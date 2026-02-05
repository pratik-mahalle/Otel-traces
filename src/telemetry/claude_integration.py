"""
Claude Code Integration Contract

Provides a formal interface for Claude Code to query and debug multi-agent systems.
Includes:
- Structured diagnostic endpoints
- Optimized diagnostic bundles
- AI-friendly error analysis
- Debugging suggestions and prompts
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
import json
import statistics

logger = logging.getLogger(__name__)


class DiagnosticSeverity(str, Enum):
    """Severity levels for diagnostic findings"""
    CRITICAL = "critical"   # Immediate action required
    HIGH = "high"           # Should be addressed soon
    MEDIUM = "medium"       # Should be investigated
    LOW = "low"             # Informational
    INFO = "info"           # Context only


class IssueCategory(str, Enum):
    """Categories for diagnostic issues"""
    ERROR = "error"
    PERFORMANCE = "performance"
    RATE_LIMIT = "rate_limit"
    RESOURCE = "resource"
    CONFIGURATION = "configuration"
    CONNECTIVITY = "connectivity"
    TIMEOUT = "timeout"
    COST = "cost"


@dataclass
class DiagnosticIssue:
    """A single diagnostic issue found in the system"""
    id: str
    category: IssueCategory
    severity: DiagnosticSeverity
    title: str
    description: str
    affected_components: List[str] = field(default_factory=list)
    suggested_actions: List[str] = field(default_factory=list)
    related_trace_ids: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    detected_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category.value,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "affected_components": self.affected_components,
            "suggested_actions": self.suggested_actions,
            "related_trace_ids": self.related_trace_ids,
            "metadata": self.metadata,
            "detected_at": self.detected_at.isoformat()
        }

    def to_claude_format(self) -> str:
        """Format for Claude Code consumption"""
        lines = [
            f"[{self.severity.value.upper()}] {self.title}",
            f"Category: {self.category.value}",
            f"Description: {self.description}",
        ]
        if self.affected_components:
            lines.append(f"Affected: {', '.join(self.affected_components)}")
        if self.suggested_actions:
            lines.append("Suggested actions:")
            for i, action in enumerate(self.suggested_actions, 1):
                lines.append(f"  {i}. {action}")
        return "\n".join(lines)


@dataclass
class PerformanceMetrics:
    """Performance metrics for a time window"""
    time_window_minutes: int
    total_traces: int
    successful_traces: int
    failed_traces: int
    success_rate: float

    avg_duration_ms: float
    p50_duration_ms: float
    p95_duration_ms: float
    p99_duration_ms: float
    max_duration_ms: float

    total_tokens: int
    avg_tokens_per_trace: float

    total_llm_calls: int
    avg_llm_calls_per_trace: float

    total_tool_calls: int
    agent_count: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time_window_minutes": self.time_window_minutes,
            "total_traces": self.total_traces,
            "successful_traces": self.successful_traces,
            "failed_traces": self.failed_traces,
            "success_rate": round(self.success_rate, 4),
            "latency": {
                "avg_ms": round(self.avg_duration_ms, 2),
                "p50_ms": round(self.p50_duration_ms, 2),
                "p95_ms": round(self.p95_duration_ms, 2),
                "p99_ms": round(self.p99_duration_ms, 2),
                "max_ms": round(self.max_duration_ms, 2)
            },
            "tokens": {
                "total": self.total_tokens,
                "avg_per_trace": round(self.avg_tokens_per_trace, 2)
            },
            "llm_calls": {
                "total": self.total_llm_calls,
                "avg_per_trace": round(self.avg_llm_calls_per_trace, 2)
            },
            "tool_calls": self.total_tool_calls,
            "unique_agents": self.agent_count
        }


@dataclass
class RateLimitStatus:
    """Current rate limit status across all models"""
    model: str
    provider: str
    tpm_used: float
    tpm_limit: float
    tpm_percentage: float
    rpm_used: float
    rpm_limit: float
    rpm_percentage: float
    is_limited: bool
    estimated_reset_seconds: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "tpm": {
                "used": self.tpm_used,
                "limit": self.tpm_limit,
                "percentage": round(self.tpm_percentage, 2)
            },
            "rpm": {
                "used": self.rpm_used,
                "limit": self.rpm_limit,
                "percentage": round(self.rpm_percentage, 2)
            },
            "is_limited": self.is_limited,
            "estimated_reset_seconds": self.estimated_reset_seconds
        }


@dataclass
class ErrorSummary:
    """Summary of errors in a time window"""
    error_type: str
    error_category: str
    count: int
    first_seen: datetime
    last_seen: datetime
    affected_agents: List[str]
    sample_message: str
    sample_trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_type": self.error_type,
            "error_category": self.error_category,
            "count": self.count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "affected_agents": self.affected_agents,
            "sample_message": self.sample_message,
            "sample_trace_id": self.sample_trace_id
        }


@dataclass
class SlowTraceInfo:
    """Information about a slow trace"""
    trace_id: str
    duration_ms: float
    agent_count: int
    llm_call_count: int
    slowest_span_name: str
    slowest_span_duration_ms: float
    bottleneck_analysis: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "duration_ms": round(self.duration_ms, 2),
            "agent_count": self.agent_count,
            "llm_call_count": self.llm_call_count,
            "slowest_span": {
                "name": self.slowest_span_name,
                "duration_ms": round(self.slowest_span_duration_ms, 2)
            },
            "bottleneck_analysis": self.bottleneck_analysis
        }


@dataclass
class DiagnosticBundle:
    """
    Complete diagnostic bundle for Claude Code.
    This is the primary interface for debugging assistance.
    """
    # Metadata
    generated_at: datetime = field(default_factory=datetime.utcnow)
    time_window_minutes: int = 15
    system_status: str = "healthy"  # healthy, degraded, critical

    # Issues
    issues: List[DiagnosticIssue] = field(default_factory=list)
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0

    # Performance
    performance: Optional[PerformanceMetrics] = None

    # Errors
    recent_errors: List[ErrorSummary] = field(default_factory=list)
    error_rate: float = 0.0

    # Rate limits
    rate_limit_status: List[RateLimitStatus] = field(default_factory=list)
    any_rate_limited: bool = False

    # Slow traces
    slow_traces: List[SlowTraceInfo] = field(default_factory=list)

    # Suggested actions (prioritized)
    suggested_actions: List[Dict[str, Any]] = field(default_factory=list)

    # Context for Claude
    debugging_context: str = ""
    system_summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": {
                "generated_at": self.generated_at.isoformat(),
                "time_window_minutes": self.time_window_minutes,
                "system_status": self.system_status
            },
            "issues": {
                "items": [i.to_dict() for i in self.issues],
                "counts": {
                    "critical": self.critical_count,
                    "high": self.high_count,
                    "medium": self.medium_count,
                    "total": len(self.issues)
                }
            },
            "performance": self.performance.to_dict() if self.performance else None,
            "errors": {
                "recent": [e.to_dict() for e in self.recent_errors],
                "error_rate": round(self.error_rate, 4)
            },
            "rate_limits": {
                "status": [r.to_dict() for r in self.rate_limit_status],
                "any_limited": self.any_rate_limited
            },
            "slow_traces": [s.to_dict() for s in self.slow_traces],
            "suggested_actions": self.suggested_actions,
            "debugging_context": self.debugging_context,
            "system_summary": self.system_summary
        }

    def to_claude_prompt(self) -> str:
        """
        Generate a structured prompt for Claude Code to analyze.
        This format is optimized for LLM consumption.
        """
        lines = [
            "=== MULTI-AGENT SYSTEM DIAGNOSTIC REPORT ===",
            f"Generated: {self.generated_at.isoformat()}",
            f"Time Window: Last {self.time_window_minutes} minutes",
            f"System Status: {self.system_status.upper()}",
            ""
        ]

        # Summary
        lines.append("[SUMMARY]")
        lines.append(self.system_summary if self.system_summary else "No significant issues detected.")
        lines.append("")

        # Critical issues first
        if self.issues:
            lines.append("[ISSUES REQUIRING ATTENTION]")
            for issue in sorted(self.issues, key=lambda x: ["critical", "high", "medium", "low", "info"].index(x.severity.value)):
                lines.append(issue.to_claude_format())
                lines.append("")

        # Performance metrics
        if self.performance:
            lines.append("[PERFORMANCE METRICS]")
            p = self.performance
            lines.append(f"Traces: {p.total_traces} total, {p.successful_traces} successful, {p.failed_traces} failed")
            lines.append(f"Success Rate: {p.success_rate * 100:.1f}%")
            lines.append(f"Latency: avg={p.avg_duration_ms:.0f}ms, p50={p.p50_duration_ms:.0f}ms, p95={p.p95_duration_ms:.0f}ms, p99={p.p99_duration_ms:.0f}ms")
            lines.append(f"Tokens: {p.total_tokens} total, {p.avg_tokens_per_trace:.0f} avg/trace")
            lines.append(f"LLM Calls: {p.total_llm_calls} total, {p.avg_llm_calls_per_trace:.1f} avg/trace")
            lines.append("")

        # Errors
        if self.recent_errors:
            lines.append("[RECENT ERRORS]")
            for error in self.recent_errors[:5]:  # Top 5
                lines.append(f"- {error.error_type} ({error.count}x): {error.sample_message[:100]}")
                lines.append(f"  Agents affected: {', '.join(error.affected_agents)}")
            lines.append("")

        # Rate limits
        if self.any_rate_limited:
            lines.append("[RATE LIMIT WARNINGS]")
            for rl in self.rate_limit_status:
                if rl.is_limited or rl.tpm_percentage > 80 or rl.rpm_percentage > 80:
                    lines.append(f"- {rl.model}: TPM {rl.tpm_percentage:.0f}%, RPM {rl.rpm_percentage:.0f}%")
            lines.append("")

        # Slow traces
        if self.slow_traces:
            lines.append("[SLOW TRACES]")
            for st in self.slow_traces[:3]:  # Top 3
                lines.append(f"- Trace {st.trace_id[:8]}...: {st.duration_ms:.0f}ms")
                lines.append(f"  Bottleneck: {st.bottleneck_analysis}")
            lines.append("")

        # Suggested actions
        if self.suggested_actions:
            lines.append("[SUGGESTED ACTIONS]")
            for i, action in enumerate(self.suggested_actions[:5], 1):
                lines.append(f"{i}. [{action.get('priority', 'medium').upper()}] {action.get('action', 'N/A')}")
                if action.get("rationale"):
                    lines.append(f"   Rationale: {action['rationale']}")
            lines.append("")

        # Debugging context
        if self.debugging_context:
            lines.append("[DEBUGGING CONTEXT]")
            lines.append(self.debugging_context)
            lines.append("")

        lines.append("=== END DIAGNOSTIC REPORT ===")

        return "\n".join(lines)


class ClaudeDiagnosticAnalyzer:
    """
    Analyzes telemetry data and generates diagnostic bundles for Claude Code.
    """

    def __init__(
        self,
        slow_trace_threshold_ms: float = 5000.0,
        error_rate_threshold: float = 0.1,
        rate_limit_warning_threshold: float = 0.8
    ):
        self.slow_trace_threshold_ms = slow_trace_threshold_ms
        self.error_rate_threshold = error_rate_threshold
        self.rate_limit_warning_threshold = rate_limit_warning_threshold

    async def analyze(
        self,
        traces: List[Dict[str, Any]],
        spans: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        oracle_state: Optional[Dict[str, Any]] = None,
        time_window_minutes: int = 15
    ) -> DiagnosticBundle:
        """
        Analyze telemetry data and generate a diagnostic bundle.
        """
        bundle = DiagnosticBundle(time_window_minutes=time_window_minutes)

        # Analyze performance
        bundle.performance = self._analyze_performance(traces, spans, time_window_minutes)

        # Analyze errors
        bundle.recent_errors = self._analyze_errors(traces, spans, events)
        if bundle.performance:
            bundle.error_rate = 1 - bundle.performance.success_rate

        # Analyze rate limits
        if oracle_state:
            bundle.rate_limit_status = self._analyze_rate_limits(oracle_state)
            bundle.any_rate_limited = any(r.is_limited for r in bundle.rate_limit_status)

        # Find slow traces
        bundle.slow_traces = self._find_slow_traces(traces, spans)

        # Identify issues
        bundle.issues = self._identify_issues(bundle, oracle_state)

        # Count issues by severity
        for issue in bundle.issues:
            if issue.severity == DiagnosticSeverity.CRITICAL:
                bundle.critical_count += 1
            elif issue.severity == DiagnosticSeverity.HIGH:
                bundle.high_count += 1
            elif issue.severity == DiagnosticSeverity.MEDIUM:
                bundle.medium_count += 1

        # Determine system status
        if bundle.critical_count > 0:
            bundle.system_status = "critical"
        elif bundle.high_count > 0 or bundle.error_rate > self.error_rate_threshold:
            bundle.system_status = "degraded"
        else:
            bundle.system_status = "healthy"

        # Generate suggested actions
        bundle.suggested_actions = self._generate_suggested_actions(bundle)

        # Generate summaries
        bundle.system_summary = self._generate_system_summary(bundle)
        bundle.debugging_context = self._generate_debugging_context(bundle, oracle_state)

        return bundle

    def _analyze_performance(
        self,
        traces: List[Dict[str, Any]],
        spans: List[Dict[str, Any]],
        time_window_minutes: int
    ) -> Optional[PerformanceMetrics]:
        """Analyze performance metrics from traces"""
        if not traces:
            return PerformanceMetrics(
                time_window_minutes=time_window_minutes,
                total_traces=0,
                successful_traces=0,
                failed_traces=0,
                success_rate=1.0,
                avg_duration_ms=0,
                p50_duration_ms=0,
                p95_duration_ms=0,
                p99_duration_ms=0,
                max_duration_ms=0,
                total_tokens=0,
                avg_tokens_per_trace=0,
                total_llm_calls=0,
                avg_llm_calls_per_trace=0,
                total_tool_calls=0,
                agent_count=0
            )

        total = len(traces)
        successful = len([t for t in traces if t.get("status") == "completed"])
        failed = len([t for t in traces if t.get("status") == "failed"])

        durations = [t.get("total_duration_ms", 0) for t in traces if t.get("total_duration_ms")]
        if not durations:
            durations = [0]

        sorted_durations = sorted(durations)
        p50_idx = int(len(sorted_durations) * 0.5)
        p95_idx = int(len(sorted_durations) * 0.95)
        p99_idx = int(len(sorted_durations) * 0.99)

        total_tokens = sum(t.get("total_tokens", 0) for t in traces)
        total_llm_calls = sum(t.get("llm_call_count", 0) for t in traces)
        total_tool_calls = sum(t.get("tool_call_count", 0) for t in traces)

        # Count unique agents
        agents = set()
        for span in spans:
            if span.get("agent_id"):
                agents.add(span["agent_id"])

        return PerformanceMetrics(
            time_window_minutes=time_window_minutes,
            total_traces=total,
            successful_traces=successful,
            failed_traces=failed,
            success_rate=successful / max(total, 1),
            avg_duration_ms=statistics.mean(durations),
            p50_duration_ms=sorted_durations[p50_idx] if sorted_durations else 0,
            p95_duration_ms=sorted_durations[min(p95_idx, len(sorted_durations) - 1)] if sorted_durations else 0,
            p99_duration_ms=sorted_durations[min(p99_idx, len(sorted_durations) - 1)] if sorted_durations else 0,
            max_duration_ms=max(durations) if durations else 0,
            total_tokens=total_tokens,
            avg_tokens_per_trace=total_tokens / max(total, 1),
            total_llm_calls=total_llm_calls,
            avg_llm_calls_per_trace=total_llm_calls / max(total, 1),
            total_tool_calls=total_tool_calls,
            agent_count=len(agents)
        )

    def _analyze_errors(
        self,
        traces: List[Dict[str, Any]],
        spans: List[Dict[str, Any]],
        events: List[Dict[str, Any]]
    ) -> List[ErrorSummary]:
        """Analyze and summarize errors"""
        error_map: Dict[str, Dict] = {}

        # Collect errors from spans
        for span in spans:
            if span.get("status") == "failed" or span.get("error_message"):
                error_type = span.get("error_type", "UnknownError")
                key = f"{error_type}"

                if key not in error_map:
                    error_map[key] = {
                        "error_type": error_type,
                        "error_category": self._categorize_error(error_type),
                        "count": 0,
                        "first_seen": datetime.utcnow(),
                        "last_seen": datetime.utcnow(),
                        "affected_agents": set(),
                        "sample_message": span.get("error_message", "No message"),
                        "sample_trace_id": span.get("trace_id")
                    }

                error_map[key]["count"] += 1
                if span.get("agent_name"):
                    error_map[key]["affected_agents"].add(span["agent_name"])

        # Collect errors from events
        for event in events:
            if event.get("severity") == "error":
                error_type = event.get("event_type", "error")
                key = f"event_{error_type}"

                if key not in error_map:
                    error_map[key] = {
                        "error_type": error_type,
                        "error_category": "event_error",
                        "count": 0,
                        "first_seen": datetime.utcnow(),
                        "last_seen": datetime.utcnow(),
                        "affected_agents": set(),
                        "sample_message": event.get("message", "No message"),
                        "sample_trace_id": event.get("trace_id")
                    }

                error_map[key]["count"] += 1
                if event.get("agent_name"):
                    error_map[key]["affected_agents"].add(event["agent_name"])

        # Convert to ErrorSummary objects
        summaries = []
        for data in error_map.values():
            summaries.append(ErrorSummary(
                error_type=data["error_type"],
                error_category=data["error_category"],
                count=data["count"],
                first_seen=data["first_seen"],
                last_seen=data["last_seen"],
                affected_agents=list(data["affected_agents"]),
                sample_message=data["sample_message"],
                sample_trace_id=data["sample_trace_id"]
            ))

        # Sort by count descending
        return sorted(summaries, key=lambda x: x.count, reverse=True)

    def _categorize_error(self, error_type: str) -> str:
        """Categorize error by type"""
        error_type_lower = error_type.lower()

        if any(x in error_type_lower for x in ["ratelimit", "quota", "429"]):
            return "rate_limit"
        elif any(x in error_type_lower for x in ["timeout", "timedout"]):
            return "timeout"
        elif any(x in error_type_lower for x in ["connection", "network", "http"]):
            return "connectivity"
        elif any(x in error_type_lower for x in ["validation", "invalid", "schema"]):
            return "validation"
        elif any(x in error_type_lower for x in ["memory", "resource", "oom"]):
            return "resource"
        elif any(x in error_type_lower for x in ["auth", "permission", "forbidden"]):
            return "authentication"
        else:
            return "unknown"

    def _analyze_rate_limits(self, oracle_state: Dict[str, Any]) -> List[RateLimitStatus]:
        """Analyze rate limit status from Oracle state"""
        statuses = []

        litellm = oracle_state.get("litellm", [])
        for model in litellm:
            tpm = model.get("tpm", 0)
            tpm_max = model.get("tpm_max", 1)
            rpm = model.get("rpm", 0)
            rpm_max = model.get("rpm_max", 1)

            tpm_pct = (tpm / tpm_max * 100) if tpm_max > 0 else 0
            rpm_pct = (rpm / rpm_max * 100) if rpm_max > 0 else 0

            statuses.append(RateLimitStatus(
                model=model.get("model", "unknown"),
                provider=model.get("provider", "unknown"),
                tpm_used=tpm,
                tpm_limit=tpm_max,
                tpm_percentage=tpm_pct,
                rpm_used=rpm,
                rpm_limit=rpm_max,
                rpm_percentage=rpm_pct,
                is_limited=tpm_pct >= 95 or rpm_pct >= 95,
                estimated_reset_seconds=60 if tpm_pct >= 95 else None
            ))

        return statuses

    def _find_slow_traces(
        self,
        traces: List[Dict[str, Any]],
        spans: List[Dict[str, Any]]
    ) -> List[SlowTraceInfo]:
        """Find and analyze slow traces"""
        slow_traces = []

        for trace in traces:
            duration = trace.get("total_duration_ms", 0)
            if duration < self.slow_trace_threshold_ms:
                continue

            trace_id = trace.get("trace_id", "")
            trace_spans = [s for s in spans if s.get("trace_id") == trace_id]

            # Find slowest span
            slowest_span = max(trace_spans, key=lambda x: x.get("duration_ms", 0), default={})
            slowest_duration = slowest_span.get("duration_ms", 0)
            slowest_name = slowest_span.get("agent_name", "unknown")

            # Analyze bottleneck
            bottleneck = self._analyze_bottleneck(trace, trace_spans, slowest_span)

            slow_traces.append(SlowTraceInfo(
                trace_id=trace_id,
                duration_ms=duration,
                agent_count=trace.get("agent_count", 0),
                llm_call_count=trace.get("llm_call_count", 0),
                slowest_span_name=slowest_name,
                slowest_span_duration_ms=slowest_duration,
                bottleneck_analysis=bottleneck
            ))

        return sorted(slow_traces, key=lambda x: x.duration_ms, reverse=True)[:10]

    def _analyze_bottleneck(
        self,
        trace: Dict[str, Any],
        spans: List[Dict[str, Any]],
        slowest_span: Dict[str, Any]
    ) -> str:
        """Analyze what's causing the bottleneck"""
        if not slowest_span:
            return "Unable to determine bottleneck"

        span_kind = slowest_span.get("span_kind", "unknown")
        agent_name = slowest_span.get("agent_name", "unknown")
        duration = slowest_span.get("duration_ms", 0)

        if span_kind == "llm_call":
            tokens = slowest_span.get("token_usage", {}).get("total_tokens", 0)
            model = slowest_span.get("model_name", "unknown")
            return f"LLM call to {model} with {tokens} tokens took {duration:.0f}ms"

        elif span_kind == "tool":
            tool_name = slowest_span.get("tool_invocations", [{}])[0].get("tool_name", "unknown") if slowest_span.get("tool_invocations") else "unknown"
            return f"Tool execution '{tool_name}' took {duration:.0f}ms"

        elif span_kind == "agent":
            llm_calls = len([s for s in spans if s.get("parent_span_id") == slowest_span.get("span_id") and s.get("span_kind") == "llm_call"])
            return f"Agent '{agent_name}' made {llm_calls} LLM calls, taking {duration:.0f}ms total"

        return f"Span '{agent_name}' ({span_kind}) took {duration:.0f}ms"

    def _identify_issues(
        self,
        bundle: DiagnosticBundle,
        oracle_state: Optional[Dict[str, Any]]
    ) -> List[DiagnosticIssue]:
        """Identify issues from the analysis"""
        issues = []
        issue_id = 0

        # High error rate
        if bundle.error_rate > self.error_rate_threshold:
            issue_id += 1
            issues.append(DiagnosticIssue(
                id=f"issue_{issue_id}",
                category=IssueCategory.ERROR,
                severity=DiagnosticSeverity.HIGH if bundle.error_rate > 0.2 else DiagnosticSeverity.MEDIUM,
                title=f"High Error Rate: {bundle.error_rate * 100:.1f}%",
                description=f"Error rate exceeds threshold of {self.error_rate_threshold * 100:.0f}%",
                affected_components=[e.affected_agents[0] for e in bundle.recent_errors if e.affected_agents][:3],
                suggested_actions=[
                    "Review recent error messages for patterns",
                    "Check agent configurations",
                    "Verify external service connectivity"
                ]
            ))

        # Rate limiting
        for rl in bundle.rate_limit_status:
            if rl.is_limited:
                issue_id += 1
                issues.append(DiagnosticIssue(
                    id=f"issue_{issue_id}",
                    category=IssueCategory.RATE_LIMIT,
                    severity=DiagnosticSeverity.HIGH,
                    title=f"Rate Limit Hit: {rl.model}",
                    description=f"Model {rl.model} is at {max(rl.tpm_percentage, rl.rpm_percentage):.0f}% of rate limit",
                    affected_components=[rl.model],
                    suggested_actions=[
                        f"Wait ~{rl.estimated_reset_seconds or 60}s for rate limit reset",
                        "Consider using a different model",
                        "Implement request queuing or backoff"
                    ],
                    metadata={"tpm_pct": rl.tpm_percentage, "rpm_pct": rl.rpm_percentage}
                ))
            elif rl.tpm_percentage > 80 or rl.rpm_percentage > 80:
                issue_id += 1
                issues.append(DiagnosticIssue(
                    id=f"issue_{issue_id}",
                    category=IssueCategory.RATE_LIMIT,
                    severity=DiagnosticSeverity.MEDIUM,
                    title=f"Approaching Rate Limit: {rl.model}",
                    description=f"Model {rl.model} is at {max(rl.tpm_percentage, rl.rpm_percentage):.0f}% of rate limit",
                    affected_components=[rl.model],
                    suggested_actions=[
                        "Monitor rate limit closely",
                        "Consider load balancing across models"
                    ]
                ))

        # Slow performance
        if bundle.performance and bundle.performance.p95_duration_ms > self.slow_trace_threshold_ms:
            issue_id += 1
            issues.append(DiagnosticIssue(
                id=f"issue_{issue_id}",
                category=IssueCategory.PERFORMANCE,
                severity=DiagnosticSeverity.MEDIUM,
                title=f"High Latency: p95 = {bundle.performance.p95_duration_ms:.0f}ms",
                description="95th percentile latency exceeds threshold",
                suggested_actions=[
                    "Review slow traces for optimization opportunities",
                    "Consider caching frequently used data",
                    "Optimize agent handoff patterns"
                ]
            ))

        # Specific error patterns
        for error in bundle.recent_errors:
            if error.count >= 5:  # Multiple occurrences
                issue_id += 1
                severity = DiagnosticSeverity.HIGH if error.count >= 10 else DiagnosticSeverity.MEDIUM

                suggestions = self._get_error_suggestions(error.error_category)

                issues.append(DiagnosticIssue(
                    id=f"issue_{issue_id}",
                    category=IssueCategory.ERROR,
                    severity=severity,
                    title=f"Repeated Error: {error.error_type} ({error.count}x)",
                    description=error.sample_message[:200],
                    affected_components=error.affected_agents,
                    suggested_actions=suggestions,
                    related_trace_ids=[error.sample_trace_id] if error.sample_trace_id else []
                ))

        # Resource issues from Oracle state
        if oracle_state:
            workloads = oracle_state.get("workload", [])
            for workload in workloads:
                pods = workload.get("pods", [])
                failed_pods = [p for p in pods if p.get("status") == "Failed"]
                if failed_pods:
                    issue_id += 1
                    issues.append(DiagnosticIssue(
                        id=f"issue_{issue_id}",
                        category=IssueCategory.RESOURCE,
                        severity=DiagnosticSeverity.HIGH,
                        title=f"Failed Pods: {workload.get('deployment_name')}",
                        description=f"{len(failed_pods)} pods in failed state",
                        affected_components=[p.get("pod_id") for p in failed_pods],
                        suggested_actions=[
                            "Check pod logs for error details",
                            "Verify resource limits",
                            "Check for OOM kills"
                        ]
                    ))

        return issues

    def _get_error_suggestions(self, error_category: str) -> List[str]:
        """Get contextual suggestions for error categories"""
        suggestions = {
            "rate_limit": [
                "Implement exponential backoff",
                "Add request queuing",
                "Consider using multiple API keys"
            ],
            "timeout": [
                "Increase timeout configuration",
                "Check network latency",
                "Reduce request complexity"
            ],
            "connectivity": [
                "Verify service URLs",
                "Check network configuration",
                "Ensure required services are running"
            ],
            "validation": [
                "Review input data format",
                "Check schema requirements",
                "Validate data before sending"
            ],
            "resource": [
                "Increase memory limits",
                "Reduce batch sizes",
                "Scale infrastructure"
            ],
            "authentication": [
                "Verify API keys",
                "Check token expiration",
                "Review permissions"
            ]
        }
        return suggestions.get(error_category, [
            "Review error details",
            "Check system logs",
            "Contact support if persists"
        ])

    def _generate_suggested_actions(self, bundle: DiagnosticBundle) -> List[Dict[str, Any]]:
        """Generate prioritized suggested actions"""
        actions = []

        # Collect all suggestions from issues
        for issue in bundle.issues:
            for action in issue.suggested_actions[:2]:  # Top 2 per issue
                priority = "high" if issue.severity in (DiagnosticSeverity.CRITICAL, DiagnosticSeverity.HIGH) else "medium"
                actions.append({
                    "priority": priority,
                    "action": action,
                    "rationale": f"Addresses: {issue.title}",
                    "related_issue_id": issue.id
                })

        # Deduplicate and sort
        seen = set()
        unique_actions = []
        for action in actions:
            if action["action"] not in seen:
                seen.add(action["action"])
                unique_actions.append(action)

        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2}
        return sorted(unique_actions, key=lambda x: priority_order.get(x["priority"], 2))

    def _generate_system_summary(self, bundle: DiagnosticBundle) -> str:
        """Generate a human-readable system summary"""
        parts = []

        if bundle.system_status == "healthy":
            parts.append("System is operating normally.")
        elif bundle.system_status == "degraded":
            parts.append("System is experiencing some issues.")
        else:
            parts.append("CRITICAL: System requires immediate attention.")

        if bundle.performance:
            parts.append(f"Processed {bundle.performance.total_traces} traces with {bundle.performance.success_rate * 100:.1f}% success rate.")

        if bundle.critical_count > 0:
            parts.append(f"{bundle.critical_count} critical issue(s) detected.")

        if bundle.any_rate_limited:
            limited = [r.model for r in bundle.rate_limit_status if r.is_limited]
            parts.append(f"Rate limits hit on: {', '.join(limited)}.")

        if bundle.slow_traces:
            parts.append(f"{len(bundle.slow_traces)} slow trace(s) detected (>{self.slow_trace_threshold_ms}ms).")

        return " ".join(parts)

    def _generate_debugging_context(
        self,
        bundle: DiagnosticBundle,
        oracle_state: Optional[Dict[str, Any]]
    ) -> str:
        """Generate context helpful for debugging"""
        context_parts = []

        # Agent topology
        if oracle_state:
            agents = oracle_state.get("agents", [])
            if agents:
                agent_names = [a.get("name") for a in agents]
                context_parts.append(f"Active agents: {', '.join(agent_names)}")

        # Error patterns
        if bundle.recent_errors:
            top_errors = bundle.recent_errors[:3]
            error_summary = "; ".join([f"{e.error_type}({e.count}x)" for e in top_errors])
            context_parts.append(f"Top errors: {error_summary}")

        # Performance baseline
        if bundle.performance:
            context_parts.append(
                f"Performance baseline: avg={bundle.performance.avg_duration_ms:.0f}ms, "
                f"p95={bundle.performance.p95_duration_ms:.0f}ms"
            )

        return " | ".join(context_parts)


# Convenience function for quick diagnostics
async def get_diagnostic_bundle(
    traces: List[Dict[str, Any]],
    spans: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    oracle_state: Optional[Dict[str, Any]] = None,
    time_window_minutes: int = 15
) -> DiagnosticBundle:
    """
    Quick access to diagnostic bundle generation.
    Use this in API endpoints.
    """
    analyzer = ClaudeDiagnosticAnalyzer()
    return await analyzer.analyze(
        traces=traces,
        spans=spans,
        events=events,
        oracle_state=oracle_state,
        time_window_minutes=time_window_minutes
    )
