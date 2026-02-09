"""
Multi-Agent Telemetry Package

Provides comprehensive telemetry for debugging multi-agent AI systems.
"""

from .schemas import (
    AgentSpan, AgentTrace, AgentHandoff, DebugEvent,
    TokenUsage, LLMMessage, ToolDefinition, ToolInvocation,
    DetailedError, AgentStatus, SpanKind, ErrorSeverity, ErrorCategory,
    AgentMessage, MessageType, MessagePriority,
    AGENT_QUEUE_PREFIX, DEFAULT_AGENT_NAMES,
    agent_queue_topic
)

from .collector import (
    TelemetryCollector,
    InMemoryTelemetryCollector
)

from .sampling import (
    TelemetrySampler,
    SamplingConfig,
    RateLimitConfig,
    SamplingDecision,
    SamplingReason,
    SamplingResult,
    TokenBucket,
    CardinalityLimiter,
    get_default_sampler,
    configure_sampler
)

from .otlp_exporter import (
    OTLPExporter,
    OTLPExporterConfig,
    OTLPSpanConverter,
    W3CTraceContext,
    TraceContextPropagator,
    TracingMiddleware,
    create_otlp_exporter,
    get_global_exporter,
    shutdown_global_exporter,
    generate_trace_id,
    generate_span_id
)

from .claude_integration import (
    ClaudeDiagnosticAnalyzer,
    DiagnosticBundle,
    DiagnosticIssue,
    DiagnosticSeverity,
    IssueCategory,
    PerformanceMetrics,
    RateLimitStatus,
    ErrorSummary,
    SlowTraceInfo,
    get_diagnostic_bundle
)

__all__ = [
    # Schemas
    "AgentSpan", "AgentTrace", "AgentHandoff", "DebugEvent",
    "TokenUsage", "LLMMessage", "ToolDefinition", "ToolInvocation",
    "DetailedError", "AgentStatus", "SpanKind", "ErrorSeverity", "ErrorCategory",
    "AgentMessage", "MessageType", "MessagePriority",
    "AGENT_QUEUE_PREFIX", "DEFAULT_AGENT_NAMES",
    "agent_queue_topic",

    # Collector
    "TelemetryCollector", "InMemoryTelemetryCollector",

    # Sampling
    "TelemetrySampler", "SamplingConfig", "RateLimitConfig",
    "SamplingDecision", "SamplingReason", "SamplingResult",
    "TokenBucket", "CardinalityLimiter",
    "get_default_sampler", "configure_sampler",

    # OTLP Export
    "OTLPExporter", "OTLPExporterConfig", "OTLPSpanConverter",
    "W3CTraceContext", "TraceContextPropagator", "TracingMiddleware",
    "create_otlp_exporter", "get_global_exporter", "shutdown_global_exporter",
    "generate_trace_id", "generate_span_id",

    # Claude Integration
    "ClaudeDiagnosticAnalyzer", "DiagnosticBundle", "DiagnosticIssue",
    "DiagnosticSeverity", "IssueCategory", "PerformanceMetrics",
    "RateLimitStatus", "ErrorSummary", "SlowTraceInfo",
    "get_diagnostic_bundle"
]
