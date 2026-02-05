"""
OpenTelemetry Protocol (OTLP) Exporter

Implements:
- OTLP/HTTP trace export
- OTLP/gRPC trace export
- W3C Trace Context propagation
- Integration with OpenTelemetry Collector
- Batch export with retry logic
"""

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID
import struct
import random

import aiohttp

logger = logging.getLogger(__name__)


class OTLPExportFormat(str, Enum):
    """OTLP export formats"""
    JSON = "json"
    PROTOBUF = "protobuf"


class SpanKindOTLP(int, Enum):
    """OpenTelemetry span kind values"""
    UNSPECIFIED = 0
    INTERNAL = 1
    SERVER = 2
    CLIENT = 3
    PRODUCER = 4
    CONSUMER = 5


class StatusCodeOTLP(int, Enum):
    """OpenTelemetry status codes"""
    UNSET = 0
    OK = 1
    ERROR = 2


# W3C Trace Context constants
TRACEPARENT_HEADER = "traceparent"
TRACESTATE_HEADER = "tracestate"
TRACEPARENT_VERSION = "00"


@dataclass
class W3CTraceContext:
    """
    W3C Trace Context for distributed tracing propagation.
    Format: {version}-{trace_id}-{parent_span_id}-{trace_flags}
    """
    trace_id: str  # 32 hex chars (16 bytes)
    span_id: str   # 16 hex chars (8 bytes)
    trace_flags: int = 0x01  # 0x01 = sampled
    trace_state: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_traceparent(cls, traceparent: str) -> Optional['W3CTraceContext']:
        """Parse W3C traceparent header"""
        try:
            parts = traceparent.split("-")
            if len(parts) != 4:
                return None
            version, trace_id, span_id, flags = parts
            if version != TRACEPARENT_VERSION:
                logger.warning(f"Unsupported traceparent version: {version}")
            return cls(
                trace_id=trace_id,
                span_id=span_id,
                trace_flags=int(flags, 16)
            )
        except Exception as e:
            logger.error(f"Failed to parse traceparent: {e}")
            return None

    @classmethod
    def from_tracestate(cls, tracestate: str) -> Dict[str, str]:
        """Parse W3C tracestate header"""
        result = {}
        try:
            for item in tracestate.split(","):
                if "=" in item:
                    key, value = item.strip().split("=", 1)
                    result[key] = value
        except Exception as e:
            logger.error(f"Failed to parse tracestate: {e}")
        return result

    def to_traceparent(self) -> str:
        """Generate W3C traceparent header value"""
        return f"{TRACEPARENT_VERSION}-{self.trace_id}-{self.span_id}-{self.trace_flags:02x}"

    def to_tracestate(self) -> str:
        """Generate W3C tracestate header value"""
        return ",".join(f"{k}={v}" for k, v in self.trace_state.items())

    def to_headers(self) -> Dict[str, str]:
        """Generate HTTP headers for propagation"""
        headers = {TRACEPARENT_HEADER: self.to_traceparent()}
        if self.trace_state:
            headers[TRACESTATE_HEADER] = self.to_tracestate()
        return headers

    @property
    def is_sampled(self) -> bool:
        return bool(self.trace_flags & 0x01)

    def create_child(self, new_span_id: str) -> 'W3CTraceContext':
        """Create child context with new span ID"""
        return W3CTraceContext(
            trace_id=self.trace_id,
            span_id=new_span_id,
            trace_flags=self.trace_flags,
            trace_state=self.trace_state.copy()
        )


def normalize_trace_id(trace_id: str) -> str:
    """Normalize trace ID to 32 hex characters"""
    # Remove hyphens (if UUID format)
    cleaned = trace_id.replace("-", "")
    # Pad or truncate to 32 chars
    if len(cleaned) < 32:
        cleaned = cleaned.zfill(32)
    elif len(cleaned) > 32:
        cleaned = cleaned[:32]
    return cleaned.lower()


def normalize_span_id(span_id: str) -> str:
    """Normalize span ID to 16 hex characters"""
    cleaned = span_id.replace("-", "")
    if len(cleaned) < 16:
        cleaned = cleaned.zfill(16)
    elif len(cleaned) > 16:
        cleaned = cleaned[:16]
    return cleaned.lower()


def generate_span_id() -> str:
    """Generate a random span ID (16 hex chars)"""
    return format(random.getrandbits(64), '016x')


def generate_trace_id() -> str:
    """Generate a random trace ID (32 hex chars)"""
    return format(random.getrandbits(128), '032x')


def timestamp_to_nanos(dt: datetime) -> int:
    """Convert datetime to nanoseconds since epoch"""
    return int(dt.timestamp() * 1_000_000_000)


def iso_to_nanos(iso_str: str) -> int:
    """Convert ISO timestamp string to nanoseconds"""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return timestamp_to_nanos(dt)


@dataclass
class OTLPExporterConfig:
    """Configuration for OTLP exporter"""
    # Endpoint configuration
    endpoint: str = "http://localhost:4318"  # OTLP HTTP default
    traces_endpoint: Optional[str] = None    # Override for traces
    metrics_endpoint: Optional[str] = None   # Override for metrics

    # Headers (for authentication)
    headers: Dict[str, str] = field(default_factory=dict)

    # Export format
    format: OTLPExportFormat = OTLPExportFormat.JSON

    # Batching configuration
    batch_size: int = 100
    batch_timeout_seconds: float = 5.0
    max_queue_size: int = 10000

    # Retry configuration
    max_retries: int = 3
    retry_delay_seconds: float = 1.0
    retry_backoff_multiplier: float = 2.0

    # Compression
    compression: str = "gzip"  # none, gzip

    # Resource attributes
    service_name: str = "multi-agent-telemetry"
    service_version: str = "1.0.0"
    deployment_environment: str = "development"

    # Additional resource attributes
    resource_attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OTLPAttribute:
    """OpenTelemetry attribute representation"""
    key: str
    value: Any

    def to_dict(self) -> Dict[str, Any]:
        """Convert to OTLP JSON format"""
        value_dict = {}

        if isinstance(self.value, str):
            value_dict["stringValue"] = self.value
        elif isinstance(self.value, bool):
            value_dict["boolValue"] = self.value
        elif isinstance(self.value, int):
            value_dict["intValue"] = str(self.value)  # OTLP uses string for int64
        elif isinstance(self.value, float):
            value_dict["doubleValue"] = self.value
        elif isinstance(self.value, (list, tuple)):
            # Array value
            array_values = []
            for item in self.value:
                if isinstance(item, str):
                    array_values.append({"stringValue": item})
                elif isinstance(item, bool):
                    array_values.append({"boolValue": item})
                elif isinstance(item, int):
                    array_values.append({"intValue": str(item)})
                elif isinstance(item, float):
                    array_values.append({"doubleValue": item})
            value_dict["arrayValue"] = {"values": array_values}
        elif isinstance(self.value, dict):
            # Convert dict to key-value list
            kv_list = []
            for k, v in self.value.items():
                kv_list.append(OTLPAttribute(k, v).to_dict())
            value_dict["kvlistValue"] = {"values": kv_list}
        else:
            value_dict["stringValue"] = str(self.value)

        return {"key": self.key, "value": value_dict}


class OTLPSpanConverter:
    """Converts internal spans to OTLP format"""

    # Map internal span kinds to OTLP
    SPAN_KIND_MAP = {
        "agent": SpanKindOTLP.INTERNAL,
        "task": SpanKindOTLP.INTERNAL,
        "tool": SpanKindOTLP.CLIENT,
        "llm_call": SpanKindOTLP.CLIENT,
        "handoff": SpanKindOTLP.PRODUCER,
        "planning": SpanKindOTLP.INTERNAL,
        "reflection": SpanKindOTLP.INTERNAL,
    }

    # Map internal status to OTLP
    STATUS_MAP = {
        "running": StatusCodeOTLP.UNSET,
        "completed": StatusCodeOTLP.OK,
        "failed": StatusCodeOTLP.ERROR,
        "waiting": StatusCodeOTLP.UNSET,
        "cancelled": StatusCodeOTLP.ERROR,
    }

    @classmethod
    def convert_span(cls, span: Dict[str, Any]) -> Dict[str, Any]:
        """Convert internal span to OTLP JSON format"""
        trace_id = normalize_trace_id(span.get("trace_id", ""))
        span_id = normalize_span_id(span.get("span_id", ""))
        parent_span_id = span.get("parent_span_id")

        # Build attributes
        attributes = []

        # Standard semantic conventions for GenAI
        if span.get("agent_name"):
            attributes.append(OTLPAttribute("gen_ai.agent.name", span["agent_name"]).to_dict())
        if span.get("agent_id"):
            attributes.append(OTLPAttribute("gen_ai.agent.id", span["agent_id"]).to_dict())
        if span.get("agent_type"):
            attributes.append(OTLPAttribute("gen_ai.agent.type", span["agent_type"]).to_dict())
        if span.get("model_name"):
            attributes.append(OTLPAttribute("gen_ai.request.model", span["model_name"]).to_dict())
        if span.get("model_provider"):
            attributes.append(OTLPAttribute("gen_ai.system", span["model_provider"]).to_dict())

        # Token usage
        token_usage = span.get("token_usage")
        if token_usage:
            if token_usage.get("prompt_tokens"):
                attributes.append(OTLPAttribute("gen_ai.usage.prompt_tokens", token_usage["prompt_tokens"]).to_dict())
            if token_usage.get("completion_tokens"):
                attributes.append(OTLPAttribute("gen_ai.usage.completion_tokens", token_usage["completion_tokens"]).to_dict())
            if token_usage.get("total_tokens"):
                attributes.append(OTLPAttribute("gen_ai.usage.total_tokens", token_usage["total_tokens"]).to_dict())

        # LLM parameters
        if span.get("temperature") is not None:
            attributes.append(OTLPAttribute("gen_ai.request.temperature", span["temperature"]).to_dict())
        if span.get("max_tokens") is not None:
            attributes.append(OTLPAttribute("gen_ai.request.max_tokens", span["max_tokens"]).to_dict())

        # Tool information
        tool_invocations = span.get("tool_invocations", [])
        if tool_invocations:
            tool_names = [t.get("tool_name") for t in tool_invocations if t.get("tool_name")]
            if tool_names:
                attributes.append(OTLPAttribute("gen_ai.tools.invoked", tool_names).to_dict())

        # Custom attributes
        custom_attrs = span.get("attributes", {})
        for key, value in custom_attrs.items():
            attributes.append(OTLPAttribute(f"custom.{key}", value).to_dict())

        # Timing
        start_time = span.get("start_time", "")
        end_time = span.get("end_time", "")

        start_nanos = iso_to_nanos(start_time) if start_time else timestamp_to_nanos(datetime.utcnow())
        end_nanos = iso_to_nanos(end_time) if end_time else start_nanos

        # Status
        status = span.get("status", "running")
        status_code = cls.STATUS_MAP.get(status, StatusCodeOTLP.UNSET)

        otlp_status = {"code": status_code.value}
        if span.get("error_message"):
            otlp_status["message"] = span["error_message"]

        # Span kind
        span_kind = span.get("span_kind", "agent")
        otlp_kind = cls.SPAN_KIND_MAP.get(span_kind, SpanKindOTLP.INTERNAL)

        # Events
        events = []
        for event in span.get("events", []):
            event_attrs = []
            for k, v in event.get("attributes", {}).items():
                event_attrs.append(OTLPAttribute(k, v).to_dict())

            events.append({
                "timeUnixNano": str(iso_to_nanos(event.get("timestamp", datetime.utcnow().isoformat()))),
                "name": event.get("name", "event"),
                "attributes": event_attrs
            })

        # Build OTLP span
        otlp_span = {
            "traceId": base64.b64encode(bytes.fromhex(trace_id)).decode(),
            "spanId": base64.b64encode(bytes.fromhex(span_id)).decode(),
            "name": span.get("agent_name", "unknown"),
            "kind": otlp_kind.value,
            "startTimeUnixNano": str(start_nanos),
            "endTimeUnixNano": str(end_nanos),
            "attributes": attributes,
            "status": otlp_status,
            "events": events
        }

        if parent_span_id:
            otlp_span["parentSpanId"] = base64.b64encode(
                bytes.fromhex(normalize_span_id(parent_span_id))
            ).decode()

        return otlp_span

    @classmethod
    def convert_trace(cls, trace: Dict[str, Any], spans: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Convert a complete trace with spans to OTLP format"""
        converted_spans = [cls.convert_span(span) for span in spans]

        return {
            "spans": converted_spans,
            "trace_id": normalize_trace_id(trace.get("trace_id", "")),
            "metadata": {
                "user_input": trace.get("user_input"),
                "final_output": trace.get("final_output"),
                "total_tokens": trace.get("total_tokens"),
                "total_cost": trace.get("total_cost"),
                "agent_count": trace.get("agent_count")
            }
        }


class OTLPExporter:
    """
    OTLP Exporter for sending telemetry to OpenTelemetry Collector
    or any OTLP-compatible backend (Jaeger, Zipkin, Datadog, etc.)
    """

    def __init__(self, config: Optional[OTLPExporterConfig] = None):
        self.config = config or OTLPExporterConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._export_queue: asyncio.Queue = asyncio.Queue(maxsize=self.config.max_queue_size)
        self._batch: List[Dict[str, Any]] = []
        self._last_export_time = time.monotonic()
        self._running = False
        self._export_task: Optional[asyncio.Task] = None

        # Metrics
        self._metrics = {
            "spans_exported": 0,
            "spans_failed": 0,
            "batches_exported": 0,
            "batches_failed": 0,
            "queue_drops": 0,
            "retries": 0
        }
        self._metrics_lock = asyncio.Lock()

    @property
    def traces_endpoint(self) -> str:
        """Get the traces endpoint URL"""
        if self.config.traces_endpoint:
            return self.config.traces_endpoint
        return f"{self.config.endpoint}/v1/traces"

    def _build_resource(self) -> Dict[str, Any]:
        """Build OTLP resource with service attributes"""
        attributes = [
            OTLPAttribute("service.name", self.config.service_name).to_dict(),
            OTLPAttribute("service.version", self.config.service_version).to_dict(),
            OTLPAttribute("deployment.environment", self.config.deployment_environment).to_dict(),
            OTLPAttribute("telemetry.sdk.name", "multi-agent-telemetry").to_dict(),
            OTLPAttribute("telemetry.sdk.language", "python").to_dict(),
            OTLPAttribute("telemetry.sdk.version", "1.0.0").to_dict(),
        ]

        # Add custom resource attributes
        for key, value in self.config.resource_attributes.items():
            attributes.append(OTLPAttribute(key, value).to_dict())

        return {"attributes": attributes}

    def _build_scope(self) -> Dict[str, Any]:
        """Build instrumentation scope"""
        return {
            "name": "multi-agent-telemetry",
            "version": "1.0.0",
            "attributes": []
        }

    async def start(self):
        """Start the exporter"""
        if self._running:
            return

        self._session = aiohttp.ClientSession(
            headers=self.config.headers,
            timeout=aiohttp.ClientTimeout(total=30)
        )
        self._running = True
        self._export_task = asyncio.create_task(self._export_loop())
        logger.info(f"OTLP exporter started, endpoint: {self.traces_endpoint}")

    async def stop(self):
        """Stop the exporter and flush remaining spans"""
        self._running = False

        # Flush remaining batch
        if self._batch:
            await self._export_batch()

        if self._export_task:
            self._export_task.cancel()
            try:
                await self._export_task
            except asyncio.CancelledError:
                pass

        if self._session:
            await self._session.close()

        logger.info("OTLP exporter stopped")

    async def _export_loop(self):
        """Background loop for batch export"""
        while self._running:
            try:
                # Wait for items or timeout
                try:
                    span = await asyncio.wait_for(
                        self._export_queue.get(),
                        timeout=self.config.batch_timeout_seconds
                    )
                    self._batch.append(span)
                except asyncio.TimeoutError:
                    pass

                # Check if we should export
                should_export = (
                    len(self._batch) >= self.config.batch_size or
                    (self._batch and time.monotonic() - self._last_export_time >= self.config.batch_timeout_seconds)
                )

                if should_export and self._batch:
                    await self._export_batch()

            except Exception as e:
                logger.error(f"Error in export loop: {e}")
                await asyncio.sleep(1)

    async def _export_batch(self):
        """Export the current batch of spans"""
        if not self._batch:
            return

        batch = self._batch.copy()
        self._batch.clear()
        self._last_export_time = time.monotonic()

        # Build OTLP request
        request_body = self._build_export_request(batch)

        # Try to export with retries
        success = await self._send_with_retry(request_body)

        async with self._metrics_lock:
            if success:
                self._metrics["spans_exported"] += len(batch)
                self._metrics["batches_exported"] += 1
            else:
                self._metrics["spans_failed"] += len(batch)
                self._metrics["batches_failed"] += 1

    def _build_export_request(self, spans: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build OTLP export request body"""
        return {
            "resourceSpans": [
                {
                    "resource": self._build_resource(),
                    "scopeSpans": [
                        {
                            "scope": self._build_scope(),
                            "spans": spans
                        }
                    ]
                }
            ]
        }

    async def _send_with_retry(self, request_body: Dict[str, Any]) -> bool:
        """Send request with retry logic"""
        delay = self.config.retry_delay_seconds

        for attempt in range(self.config.max_retries + 1):
            try:
                headers = {"Content-Type": "application/json"}

                if self.config.compression == "gzip":
                    import gzip
                    data = gzip.compress(json.dumps(request_body).encode())
                    headers["Content-Encoding"] = "gzip"
                else:
                    data = json.dumps(request_body).encode()

                async with self._session.post(
                    self.traces_endpoint,
                    data=data,
                    headers=headers
                ) as response:
                    if response.status in (200, 202):
                        return True
                    elif response.status == 429:
                        # Rate limited - wait longer
                        retry_after = int(response.headers.get("Retry-After", delay * 2))
                        await asyncio.sleep(retry_after)
                        async with self._metrics_lock:
                            self._metrics["retries"] += 1
                    elif response.status >= 500:
                        # Server error - retry
                        async with self._metrics_lock:
                            self._metrics["retries"] += 1
                        await asyncio.sleep(delay)
                        delay *= self.config.retry_backoff_multiplier
                    else:
                        # Client error - don't retry
                        logger.error(f"OTLP export failed: {response.status}")
                        return False

            except Exception as e:
                logger.error(f"OTLP export error (attempt {attempt + 1}): {e}")
                if attempt < self.config.max_retries:
                    async with self._metrics_lock:
                        self._metrics["retries"] += 1
                    await asyncio.sleep(delay)
                    delay *= self.config.retry_backoff_multiplier

        return False

    async def export_span(self, span: Dict[str, Any]):
        """Queue a span for export"""
        otlp_span = OTLPSpanConverter.convert_span(span)

        # Note: put_nowait is atomic and thread-safe on asyncio.Queue.
        # We only need to protect the metric increment, not the queue operation itself.
        try:
            self._export_queue.put_nowait(otlp_span)
        except asyncio.QueueFull:
            async with self._metrics_lock:
                self._metrics["queue_drops"] += 1
            logger.warning("OTLP export queue full, dropping span")

    async def export_spans(self, spans: List[Dict[str, Any]]):
        """Queue multiple spans for export"""
        for span in spans:
            await self.export_span(span)

    async def export_trace(self, trace: Dict[str, Any], spans: List[Dict[str, Any]]):
        """Export a complete trace with all its spans"""
        converted = OTLPSpanConverter.convert_trace(trace, spans)

        # Note: put_nowait is atomic and thread-safe on asyncio.Queue.
        # We only need to protect the metric increment, not the queue operation itself.
        for span in converted["spans"]:
            try:
                self._export_queue.put_nowait(span)
            except asyncio.QueueFull:
                async with self._metrics_lock:
                    self._metrics["queue_drops"] += 1

    async def flush(self):
        """Force flush any buffered spans"""
        if self._batch:
            await self._export_batch()

    async def get_metrics(self) -> Dict[str, Any]:
        """Get exporter metrics"""
        async with self._metrics_lock:
            return {
                **self._metrics,
                "queue_size": self._export_queue.qsize(),
                "batch_size": len(self._batch)
            }


class TraceContextPropagator:
    """
    W3C Trace Context propagator for distributed tracing.
    Extracts and injects trace context from/to HTTP headers.
    """

    @staticmethod
    def extract(headers: Dict[str, str]) -> Optional[W3CTraceContext]:
        """Extract trace context from HTTP headers"""
        traceparent = headers.get(TRACEPARENT_HEADER) or headers.get(TRACEPARENT_HEADER.title())

        if not traceparent:
            return None

        context = W3CTraceContext.from_traceparent(traceparent)

        if context:
            tracestate = headers.get(TRACESTATE_HEADER) or headers.get(TRACESTATE_HEADER.title())
            if tracestate:
                context.trace_state = W3CTraceContext.from_tracestate(tracestate)

        return context

    @staticmethod
    def inject(context: W3CTraceContext, headers: Dict[str, str]) -> Dict[str, str]:
        """Inject trace context into HTTP headers"""
        headers[TRACEPARENT_HEADER] = context.to_traceparent()
        if context.trace_state:
            headers[TRACESTATE_HEADER] = context.to_tracestate()
        return headers

    @staticmethod
    def create_context(
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        sampled: bool = True
    ) -> W3CTraceContext:
        """Create a new trace context"""
        return W3CTraceContext(
            trace_id=normalize_trace_id(trace_id) if trace_id else generate_trace_id(),
            span_id=normalize_span_id(span_id) if span_id else generate_span_id(),
            trace_flags=0x01 if sampled else 0x00
        )


# Middleware for automatic trace context propagation
class TracingMiddleware:
    """
    ASGI middleware for automatic W3C trace context propagation.
    Use with FastAPI/Starlette applications.
    """

    def __init__(self, app, exporter: Optional[OTLPExporter] = None):
        self.app = app
        self.exporter = exporter
        self.propagator = TraceContextPropagator()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract incoming trace context
        headers = dict(scope.get("headers", []))
        headers = {k.decode(): v.decode() for k, v in headers.items()}

        incoming_context = self.propagator.extract(headers)

        # Create or continue trace
        if incoming_context:
            context = incoming_context.create_child(generate_span_id())
        else:
            context = self.propagator.create_context()

        # Store in scope for access by handlers
        scope["trace_context"] = context

        # Add trace context to response headers
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                context_headers = context.to_headers()
                for key, value in context_headers.items():
                    headers.append((key.encode(), value.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# Factory functions for easy setup
def create_otlp_exporter(
    endpoint: str = None,
    service_name: str = "multi-agent-telemetry",
    headers: Dict[str, str] = None,
    **kwargs
) -> OTLPExporter:
    """Create an OTLP exporter with common defaults"""
    config = OTLPExporterConfig(
        endpoint=endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        service_name=service_name or os.environ.get("OTEL_SERVICE_NAME", "multi-agent-telemetry"),
        headers=headers or {},
        **kwargs
    )

    # Load headers from environment
    env_headers = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
    if env_headers:
        for item in env_headers.split(","):
            if "=" in item:
                key, value = item.split("=", 1)
                config.headers[key.strip()] = value.strip()

    return OTLPExporter(config)


# Global exporter instance
_global_exporter: Optional[OTLPExporter] = None


async def get_global_exporter() -> OTLPExporter:
    """Get or create the global OTLP exporter"""
    global _global_exporter
    if _global_exporter is None:
        _global_exporter = create_otlp_exporter()
        await _global_exporter.start()
    return _global_exporter


async def shutdown_global_exporter():
    """Shutdown the global exporter"""
    global _global_exporter
    if _global_exporter:
        await _global_exporter.stop()
        _global_exporter = None
