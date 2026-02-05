"""
Telemetry Sampling and Rate Limiting Strategy

Implements:
- Head-based sampling (decision at trace start)
- Tail-based sampling (decision after trace completes)
- Dynamic sampling based on errors
- Rate limiting per trace/agent
- Cardinality limits on custom attributes
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set
from functools import wraps
import hashlib
import random

logger = logging.getLogger(__name__)


class SamplingDecision(str, Enum):
    """Sampling decision outcomes"""
    SAMPLE = "sample"           # Include in telemetry
    DROP = "drop"               # Exclude from telemetry
    DEFER = "defer"             # Defer decision (for tail-based)
    PRIORITY_SAMPLE = "priority_sample"  # Always sample (errors, slow, etc.)


class SamplingReason(str, Enum):
    """Reasons for sampling decisions"""
    HEAD_BASED = "head_based"
    TAIL_BASED = "tail_based"
    ERROR_DETECTED = "error_detected"
    SLOW_TRACE = "slow_trace"
    RATE_LIMITED = "rate_limited"
    PARENT_SAMPLED = "parent_sampled"
    DEBUG_FLAG = "debug_flag"
    CARDINALITY_LIMIT = "cardinality_limit"
    RANDOM = "random"


@dataclass
class SamplingResult:
    """Result of a sampling decision"""
    decision: SamplingDecision
    reason: SamplingReason
    probability: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason.value,
            "probability": self.probability,
            "metadata": self.metadata
        }


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting"""
    # Per-second limits
    traces_per_second: float = 100.0
    spans_per_second: float = 1000.0
    events_per_second: float = 500.0

    # Per-agent limits
    traces_per_agent_per_minute: int = 60

    # Burst allowance
    burst_multiplier: float = 2.0

    # Cardinality limits
    max_unique_trace_ids: int = 10000
    max_unique_agent_ids: int = 100
    max_attribute_keys: int = 50
    max_attribute_value_length: int = 1000


@dataclass
class SamplingConfig:
    """Configuration for sampling strategies"""
    # Head-based sampling
    head_sample_rate: float = 1.0  # 1.0 = 100%, 0.1 = 10%

    # Tail-based sampling
    tail_sample_enabled: bool = True
    tail_buffer_size: int = 1000  # Max traces to buffer for tail sampling
    tail_buffer_timeout_seconds: float = 30.0

    # Dynamic sampling rules
    always_sample_errors: bool = True
    always_sample_slow_traces: bool = True
    slow_trace_threshold_ms: float = 5000.0  # 5 seconds

    # Agent-specific sampling
    agent_sample_rates: Dict[str, float] = field(default_factory=dict)

    # Debug sampling
    debug_trace_ids: Set[str] = field(default_factory=set)
    debug_session_ids: Set[str] = field(default_factory=set)

    # Priority sampling for specific event types
    priority_event_types: Set[str] = field(default_factory=lambda: {
        "error", "handoff", "tool_error", "llm_error", "timeout"
    })


class TokenBucket:
    """Token bucket rate limiter with burst support"""

    def __init__(self, rate: float, burst_multiplier: float = 2.0):
        self.rate = rate  # tokens per second
        self.max_tokens = rate * burst_multiplier
        self.tokens = self.max_tokens
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: int = 1) -> bool:
        """Try to acquire tokens. Returns True if successful."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def acquire_sync(self, tokens: int = 1) -> bool:
        """Synchronous version of acquire"""
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
        self.last_update = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    @property
    def available_tokens(self) -> float:
        """Get current available tokens"""
        now = time.monotonic()
        elapsed = now - self.last_update
        return min(self.max_tokens, self.tokens + elapsed * self.rate)


class CardinalityLimiter:
    """Limits cardinality of tracked values to prevent memory issues"""

    def __init__(self, max_items: int = 10000, ttl_seconds: float = 3600.0):
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._items: Dict[str, float] = {}  # value -> timestamp
        self._lock = asyncio.Lock()

    async def check_and_add(self, value: str) -> bool:
        """
        Check if value can be added. Returns True if within limits.
        Also performs cleanup of expired items.
        """
        async with self._lock:
            now = time.time()

            # Cleanup expired items
            expired = [k for k, v in self._items.items() if now - v > self.ttl_seconds]
            for k in expired:
                del self._items[k]

            # Check if already tracked
            if value in self._items:
                self._items[value] = now  # Update timestamp
                return True

            # Check cardinality limit
            if len(self._items) >= self.max_items:
                return False

            self._items[value] = now
            return True

    def check_and_add_sync(self, value: str) -> bool:
        """Synchronous version"""
        now = time.time()

        # Cleanup expired items (less aggressive in sync mode)
        if len(self._items) >= self.max_items:
            expired = [k for k, v in self._items.items() if now - v > self.ttl_seconds]
            for k in expired[:100]:  # Limit cleanup batch
                del self._items[k]

        if value in self._items:
            self._items[value] = now
            return True

        if len(self._items) >= self.max_items:
            return False

        self._items[value] = now
        return True

    @property
    def current_count(self) -> int:
        return len(self._items)


@dataclass
class TailSamplingBuffer:
    """Buffer for tail-based sampling decisions"""
    trace_id: str
    spans: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    handoffs: List[Dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)
    has_error: bool = False
    is_slow: bool = False
    max_duration_ms: float = 0.0
    agent_ids: Set[str] = field(default_factory=set)

    def add_span(self, span: Dict[str, Any]):
        self.spans.append(span)
        if span.get("status") == "failed" or span.get("error_message"):
            self.has_error = True
        if span.get("duration_ms"):
            self.max_duration_ms = max(self.max_duration_ms, span["duration_ms"])
        if span.get("agent_id"):
            self.agent_ids.add(span["agent_id"])

    def add_event(self, event: Dict[str, Any]):
        self.events.append(event)
        if event.get("severity") == "error":
            self.has_error = True

    def add_handoff(self, handoff: Dict[str, Any]):
        self.handoffs.append(handoff)


class TelemetrySampler:
    """
    Main sampler implementing head-based, tail-based, and dynamic sampling.
    Integrates with the TelemetryCollector for sampling decisions.
    """

    def __init__(
        self,
        sampling_config: Optional[SamplingConfig] = None,
        rate_limit_config: Optional[RateLimitConfig] = None
    ):
        self.sampling_config = sampling_config or SamplingConfig()
        self.rate_limit_config = rate_limit_config or RateLimitConfig()

        # Rate limiters
        self._trace_limiter = TokenBucket(
            self.rate_limit_config.traces_per_second,
            self.rate_limit_config.burst_multiplier
        )
        self._span_limiter = TokenBucket(
            self.rate_limit_config.spans_per_second,
            self.rate_limit_config.burst_multiplier
        )
        self._event_limiter = TokenBucket(
            self.rate_limit_config.events_per_second,
            self.rate_limit_config.burst_multiplier
        )

        # Per-agent rate limiters
        self._agent_limiters: Dict[str, TokenBucket] = {}

        # Cardinality limiters
        self._trace_cardinality = CardinalityLimiter(
            self.rate_limit_config.max_unique_trace_ids
        )
        self._agent_cardinality = CardinalityLimiter(
            self.rate_limit_config.max_unique_agent_ids
        )

        # Tail sampling buffer
        self._tail_buffer: Dict[str, TailSamplingBuffer] = {}
        self._tail_buffer_lock = asyncio.Lock()

        # Sampling decisions cache (for consistent child span decisions)
        self._sampling_decisions: Dict[str, SamplingResult] = {}

        # Metrics
        self._metrics = {
            "total_traces": 0,
            "sampled_traces": 0,
            "dropped_traces": 0,
            "rate_limited_traces": 0,
            "error_sampled_traces": 0,
            "slow_sampled_traces": 0,
            "total_spans": 0,
            "sampled_spans": 0,
            "dropped_spans": 0,
        }

    def _get_agent_limiter(self, agent_id: str) -> TokenBucket:
        """Get or create rate limiter for an agent"""
        if agent_id not in self._agent_limiters:
            # Per-minute rate converted to per-second
            rate = self.rate_limit_config.traces_per_agent_per_minute / 60.0
            self._agent_limiters[agent_id] = TokenBucket(rate)
        return self._agent_limiters[agent_id]

    def _compute_trace_hash(self, trace_id: str) -> float:
        """Compute deterministic hash for consistent sampling"""
        hash_bytes = hashlib.md5(trace_id.encode()).digest()
        hash_value = int.from_bytes(hash_bytes[:8], byteorder='big')
        return hash_value / (2**64)  # Normalize to [0, 1)

    async def should_sample_trace(
        self,
        trace_id: str,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        is_debug: bool = False,
        parent_sampled: Optional[bool] = None
    ) -> SamplingResult:
        """
        Head-based sampling decision for a new trace.
        Called when a trace is created.
        """
        self._metrics["total_traces"] += 1

        # Check if already decided (for child spans)
        if trace_id in self._sampling_decisions:
            return self._sampling_decisions[trace_id]

        # Debug flag always samples
        if is_debug or trace_id in self.sampling_config.debug_trace_ids:
            result = SamplingResult(
                decision=SamplingDecision.PRIORITY_SAMPLE,
                reason=SamplingReason.DEBUG_FLAG
            )
            self._sampling_decisions[trace_id] = result
            self._metrics["sampled_traces"] += 1
            return result

        # Debug session always samples
        if session_id and session_id in self.sampling_config.debug_session_ids:
            result = SamplingResult(
                decision=SamplingDecision.PRIORITY_SAMPLE,
                reason=SamplingReason.DEBUG_FLAG,
                metadata={"session_id": session_id}
            )
            self._sampling_decisions[trace_id] = result
            self._metrics["sampled_traces"] += 1
            return result

        # Parent sampling propagation
        if parent_sampled is not None:
            if parent_sampled:
                result = SamplingResult(
                    decision=SamplingDecision.SAMPLE,
                    reason=SamplingReason.PARENT_SAMPLED
                )
            else:
                result = SamplingResult(
                    decision=SamplingDecision.DROP,
                    reason=SamplingReason.PARENT_SAMPLED
                )
            self._sampling_decisions[trace_id] = result
            return result

        # Cardinality check
        if not await self._trace_cardinality.check_and_add(trace_id):
            result = SamplingResult(
                decision=SamplingDecision.DROP,
                reason=SamplingReason.CARDINALITY_LIMIT,
                metadata={"limit": self.rate_limit_config.max_unique_trace_ids}
            )
            self._metrics["dropped_traces"] += 1
            return result

        # Global rate limit check
        if not await self._trace_limiter.acquire():
            result = SamplingResult(
                decision=SamplingDecision.DROP,
                reason=SamplingReason.RATE_LIMITED,
                metadata={"type": "global"}
            )
            self._metrics["rate_limited_traces"] += 1
            self._metrics["dropped_traces"] += 1
            return result

        # Agent rate limit check
        if agent_id:
            agent_limiter = self._get_agent_limiter(agent_id)
            if not await agent_limiter.acquire():
                result = SamplingResult(
                    decision=SamplingDecision.DROP,
                    reason=SamplingReason.RATE_LIMITED,
                    metadata={"type": "agent", "agent_id": agent_id}
                )
                self._metrics["rate_limited_traces"] += 1
                self._metrics["dropped_traces"] += 1
                return result

        # Probabilistic head-based sampling
        sample_rate = self.sampling_config.head_sample_rate

        # Agent-specific sample rate override
        if agent_id and agent_id in self.sampling_config.agent_sample_rates:
            sample_rate = self.sampling_config.agent_sample_rates[agent_id]

        # Use deterministic hash for consistent sampling
        trace_hash = self._compute_trace_hash(trace_id)

        if trace_hash < sample_rate:
            # If tail sampling is enabled, defer final decision
            if self.sampling_config.tail_sample_enabled:
                result = SamplingResult(
                    decision=SamplingDecision.DEFER,
                    reason=SamplingReason.HEAD_BASED,
                    probability=sample_rate
                )
                # Initialize tail buffer
                async with self._tail_buffer_lock:
                    self._tail_buffer[trace_id] = TailSamplingBuffer(trace_id=trace_id)
            else:
                result = SamplingResult(
                    decision=SamplingDecision.SAMPLE,
                    reason=SamplingReason.HEAD_BASED,
                    probability=sample_rate
                )
                self._metrics["sampled_traces"] += 1
        else:
            result = SamplingResult(
                decision=SamplingDecision.DROP,
                reason=SamplingReason.RANDOM,
                probability=sample_rate
            )
            self._metrics["dropped_traces"] += 1

        self._sampling_decisions[trace_id] = result
        return result

    async def should_sample_span(
        self,
        trace_id: str,
        span_id: str,
        agent_id: Optional[str] = None,
        span_kind: Optional[str] = None,
        has_error: bool = False,
        duration_ms: Optional[float] = None
    ) -> SamplingResult:
        """
        Sampling decision for a span.
        Respects parent trace decision and applies span-specific rules.
        """
        self._metrics["total_spans"] += 1

        # Check parent trace decision
        trace_decision = self._sampling_decisions.get(trace_id)

        if trace_decision:
            if trace_decision.decision == SamplingDecision.DROP:
                self._metrics["dropped_spans"] += 1
                return SamplingResult(
                    decision=SamplingDecision.DROP,
                    reason=SamplingReason.PARENT_SAMPLED
                )
            elif trace_decision.decision == SamplingDecision.PRIORITY_SAMPLE:
                self._metrics["sampled_spans"] += 1
                return SamplingResult(
                    decision=SamplingDecision.SAMPLE,
                    reason=SamplingReason.PARENT_SAMPLED
                )

        # Rate limit check for spans
        if not await self._span_limiter.acquire():
            self._metrics["dropped_spans"] += 1
            return SamplingResult(
                decision=SamplingDecision.DROP,
                reason=SamplingReason.RATE_LIMITED
            )

        # Error detection - always sample
        if has_error and self.sampling_config.always_sample_errors:
            # Upgrade trace to priority sample
            if trace_id in self._sampling_decisions:
                self._sampling_decisions[trace_id] = SamplingResult(
                    decision=SamplingDecision.PRIORITY_SAMPLE,
                    reason=SamplingReason.ERROR_DETECTED
                )
            self._metrics["sampled_spans"] += 1
            self._metrics["error_sampled_traces"] += 1
            return SamplingResult(
                decision=SamplingDecision.PRIORITY_SAMPLE,
                reason=SamplingReason.ERROR_DETECTED
            )

        # Slow trace detection
        if duration_ms and duration_ms > self.sampling_config.slow_trace_threshold_ms:
            if self.sampling_config.always_sample_slow_traces:
                if trace_id in self._sampling_decisions:
                    self._sampling_decisions[trace_id] = SamplingResult(
                        decision=SamplingDecision.PRIORITY_SAMPLE,
                        reason=SamplingReason.SLOW_TRACE
                    )
                self._metrics["sampled_spans"] += 1
                self._metrics["slow_sampled_traces"] += 1
                return SamplingResult(
                    decision=SamplingDecision.PRIORITY_SAMPLE,
                    reason=SamplingReason.SLOW_TRACE,
                    metadata={"duration_ms": duration_ms}
                )

        # Deferred decision - add to tail buffer
        if trace_decision and trace_decision.decision == SamplingDecision.DEFER:
            self._metrics["sampled_spans"] += 1
            return SamplingResult(
                decision=SamplingDecision.DEFER,
                reason=SamplingReason.TAIL_BASED
            )

        self._metrics["sampled_spans"] += 1
        return SamplingResult(
            decision=SamplingDecision.SAMPLE,
            reason=SamplingReason.HEAD_BASED
        )

    async def should_sample_event(
        self,
        trace_id: str,
        event_type: str,
        severity: str = "info"
    ) -> SamplingResult:
        """Sampling decision for debug events"""
        # Priority events always sampled
        if event_type in self.sampling_config.priority_event_types:
            return SamplingResult(
                decision=SamplingDecision.PRIORITY_SAMPLE,
                reason=SamplingReason.ERROR_DETECTED if severity == "error" else SamplingReason.HEAD_BASED
            )

        # Error severity always sampled
        if severity == "error":
            return SamplingResult(
                decision=SamplingDecision.PRIORITY_SAMPLE,
                reason=SamplingReason.ERROR_DETECTED
            )

        # Check parent trace decision
        trace_decision = self._sampling_decisions.get(trace_id)
        if trace_decision and trace_decision.decision == SamplingDecision.DROP:
            return SamplingResult(
                decision=SamplingDecision.DROP,
                reason=SamplingReason.PARENT_SAMPLED
            )

        # Rate limit check
        if not await self._event_limiter.acquire():
            return SamplingResult(
                decision=SamplingDecision.DROP,
                reason=SamplingReason.RATE_LIMITED
            )

        return SamplingResult(
            decision=SamplingDecision.SAMPLE,
            reason=SamplingReason.HEAD_BASED
        )

    async def add_to_tail_buffer(
        self,
        trace_id: str,
        item_type: str,
        item: Dict[str, Any]
    ):
        """Add span/event/handoff to tail sampling buffer"""
        async with self._tail_buffer_lock:
            if trace_id not in self._tail_buffer:
                return

            buffer = self._tail_buffer[trace_id]

            if item_type == "span":
                buffer.add_span(item)
            elif item_type == "event":
                buffer.add_event(item)
            elif item_type == "handoff":
                buffer.add_handoff(item)

    async def finalize_tail_sampling(self, trace_id: str) -> SamplingResult:
        """
        Finalize tail-based sampling decision when trace completes.
        Returns decision and removes from buffer.
        """
        async with self._tail_buffer_lock:
            if trace_id not in self._tail_buffer:
                # Not in tail buffer, use cached decision
                return self._sampling_decisions.get(trace_id, SamplingResult(
                    decision=SamplingDecision.SAMPLE,
                    reason=SamplingReason.HEAD_BASED
                ))

            buffer = self._tail_buffer.pop(trace_id)

        # Error-based sampling
        if buffer.has_error and self.sampling_config.always_sample_errors:
            result = SamplingResult(
                decision=SamplingDecision.PRIORITY_SAMPLE,
                reason=SamplingReason.ERROR_DETECTED,
                metadata={"span_count": len(buffer.spans)}
            )
            self._sampling_decisions[trace_id] = result
            self._metrics["sampled_traces"] += 1
            self._metrics["error_sampled_traces"] += 1
            return result

        # Slow trace sampling
        if buffer.max_duration_ms > self.sampling_config.slow_trace_threshold_ms:
            if self.sampling_config.always_sample_slow_traces:
                result = SamplingResult(
                    decision=SamplingDecision.PRIORITY_SAMPLE,
                    reason=SamplingReason.SLOW_TRACE,
                    metadata={"duration_ms": buffer.max_duration_ms}
                )
                self._sampling_decisions[trace_id] = result
                self._metrics["sampled_traces"] += 1
                self._metrics["slow_sampled_traces"] += 1
                return result

        # Default: sample based on head decision
        result = SamplingResult(
            decision=SamplingDecision.SAMPLE,
            reason=SamplingReason.TAIL_BASED,
            metadata={
                "span_count": len(buffer.spans),
                "agent_count": len(buffer.agent_ids)
            }
        )
        self._sampling_decisions[trace_id] = result
        self._metrics["sampled_traces"] += 1
        return result

    async def get_buffered_items(self, trace_id: str) -> Optional[TailSamplingBuffer]:
        """Get buffered items for a trace (for export after tail sampling decision)"""
        async with self._tail_buffer_lock:
            return self._tail_buffer.get(trace_id)

    async def cleanup_expired_buffers(self):
        """Cleanup expired tail sampling buffers"""
        now = datetime.utcnow()
        timeout = timedelta(seconds=self.sampling_config.tail_buffer_timeout_seconds)

        async with self._tail_buffer_lock:
            expired = [
                trace_id for trace_id, buffer in self._tail_buffer.items()
                if now - buffer.created_at > timeout
            ]

            for trace_id in expired:
                buffer = self._tail_buffer.pop(trace_id)
                # Force sample expired buffers that have errors
                if buffer.has_error:
                    self._sampling_decisions[trace_id] = SamplingResult(
                        decision=SamplingDecision.SAMPLE,
                        reason=SamplingReason.ERROR_DETECTED
                    )
                else:
                    self._sampling_decisions[trace_id] = SamplingResult(
                        decision=SamplingDecision.DROP,
                        reason=SamplingReason.TAIL_BASED,
                        metadata={"reason": "timeout"}
                    )

        return len(expired)

    def truncate_attribute_value(self, value: Any) -> Any:
        """Truncate attribute values to prevent memory issues"""
        max_len = self.rate_limit_config.max_attribute_value_length

        if isinstance(value, str) and len(value) > max_len:
            return value[:max_len] + "...[truncated]"
        elif isinstance(value, (list, tuple)):
            return value[:100]  # Limit list items
        elif isinstance(value, dict):
            # Limit dict keys
            if len(value) > self.rate_limit_config.max_attribute_keys:
                truncated = dict(list(value.items())[:self.rate_limit_config.max_attribute_keys])
                truncated["_truncated"] = True
                return truncated
        return value

    def sanitize_attributes(self, attributes: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize and limit attributes"""
        if not attributes:
            return {}

        # Limit number of keys
        if len(attributes) > self.rate_limit_config.max_attribute_keys:
            attributes = dict(list(attributes.items())[:self.rate_limit_config.max_attribute_keys])
            attributes["_truncated"] = True

        # Truncate values
        return {k: self.truncate_attribute_value(v) for k, v in attributes.items()}

    def get_metrics(self) -> Dict[str, Any]:
        """Get sampling metrics"""
        return {
            **self._metrics,
            "trace_cardinality": self._trace_cardinality.current_count,
            "agent_cardinality": self._agent_cardinality.current_count,
            "tail_buffer_size": len(self._tail_buffer),
            "trace_rate_available": self._trace_limiter.available_tokens,
            "span_rate_available": self._span_limiter.available_tokens,
            "event_rate_available": self._event_limiter.available_tokens,
            "sample_rate": (
                self._metrics["sampled_traces"] / max(1, self._metrics["total_traces"])
            )
        }

    def reset_metrics(self):
        """Reset sampling metrics"""
        for key in self._metrics:
            self._metrics[key] = 0

    # Convenience decorators
    def sampled_trace(self, agent_id: Optional[str] = None):
        """Decorator for automatically sampling traces"""
        def decorator(func):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                trace_id = kwargs.get("trace_id") or str(random.getrandbits(128))

                result = await self.should_sample_trace(
                    trace_id=trace_id,
                    agent_id=agent_id
                )

                if result.decision in (SamplingDecision.DROP,):
                    return None

                kwargs["_sampling_result"] = result
                return await func(*args, **kwargs)
            return wrapper
        return decorator


# Singleton sampler instance
_default_sampler: Optional[TelemetrySampler] = None


def get_default_sampler() -> TelemetrySampler:
    """Get or create the default sampler instance"""
    global _default_sampler
    if _default_sampler is None:
        _default_sampler = TelemetrySampler()
    return _default_sampler


def configure_sampler(
    sampling_config: Optional[SamplingConfig] = None,
    rate_limit_config: Optional[RateLimitConfig] = None
) -> TelemetrySampler:
    """Configure the default sampler"""
    global _default_sampler
    _default_sampler = TelemetrySampler(sampling_config, rate_limit_config)
    return _default_sampler
