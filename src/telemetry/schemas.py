"""
Multi-Agent Telemetry Schemas
Based on OpenTelemetry GenAI semantic conventions for multi-agent systems
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4
import json


class AgentStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"
    CANCELLED = "cancelled"


class SpanKind(str, Enum):
    AGENT = "agent"
    TASK = "task"
    TOOL = "tool"
    LLM_CALL = "llm_call"
    HANDOFF = "handoff"
    PLANNING = "planning"
    REFLECTION = "reflection"


class ErrorSeverity(str, Enum):
    """Error severity levels for debugging prioritization"""
    CRITICAL = "critical"  # System-wide failure, immediate attention needed
    ERROR = "error"        # Operation failed, requires investigation
    WARNING = "warning"    # Potential issue, may need attention
    INFO = "info"          # Informational, no action needed


class ErrorCategory(str, Enum):
    """Error categories for quick classification"""
    LLM_ERROR = "llm_error"              # LLM API errors (rate limits, timeouts, etc.)
    AGENT_ERROR = "agent_error"          # Agent execution errors
    TOOL_ERROR = "tool_error"            # Tool invocation failures
    HANDOFF_ERROR = "handoff_error"      # Agent handoff failures
    VALIDATION_ERROR = "validation_error" # Input/output validation errors
    TIMEOUT_ERROR = "timeout_error"       # Operation timeouts
    RESOURCE_ERROR = "resource_error"     # Resource exhaustion (memory, CPU)
    NETWORK_ERROR = "network_error"       # Network connectivity issues
    CONFIG_ERROR = "config_error"         # Configuration/setup errors
    UNKNOWN = "unknown"                   # Unclassified errors


@dataclass
class DetailedError:
    """
    Comprehensive error information for Claude Code debugging.
    Includes context, stack traces, and actionable suggestions.
    """
    # Unique identifier
    error_id: str = field(default_factory=lambda: str(uuid4()))
    
    # Classification
    severity: ErrorSeverity = ErrorSeverity.ERROR
    category: ErrorCategory = ErrorCategory.UNKNOWN
    
    # Error details
    error_type: str = ""           # Exception class name
    error_message: str = ""        # Human-readable message
    error_code: Optional[str] = None  # Error code if available
    
    # Stack trace
    stack_trace: Optional[str] = None
    
    # Context - what was happening when error occurred
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    agent_name: Optional[str] = None
    agent_id: Optional[str] = None
    operation: Optional[str] = None  # What operation was being performed
    
    # Input/Output at time of error
    input_data: Dict[str, Any] = field(default_factory=dict)
    partial_output: Optional[Any] = None
    
    # LLM-specific context
    model_name: Optional[str] = None
    prompt_preview: Optional[str] = None  # First 500 chars of prompt
    token_count: Optional[int] = None
    
    # Tool-specific context
    tool_name: Optional[str] = None
    tool_parameters: Dict[str, Any] = field(default_factory=dict)
    
    # Timing
    timestamp: datetime = field(default_factory=datetime.utcnow)
    duration_before_error_ms: Optional[float] = None
    
    # Recovery information
    is_retryable: bool = False
    retry_count: int = 0
    max_retries: int = 3
    
    # Debugging suggestions
    suggested_fixes: List[str] = field(default_factory=list)
    related_docs: List[str] = field(default_factory=list)
    
    # Environment context
    environment: str = "development"
    service_name: str = "multi-agent-system"
    host: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_id": self.error_id,
            "severity": self.severity.value,
            "category": self.category.value,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "error_code": self.error_code,
            "stack_trace": self.stack_trace,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "agent_name": self.agent_name,
            "agent_id": self.agent_id,
            "operation": self.operation,
            "input_data": self.input_data,
            "partial_output": self.partial_output,
            "model_name": self.model_name,
            "prompt_preview": self.prompt_preview,
            "token_count": self.token_count,
            "tool_name": self.tool_name,
            "tool_parameters": self.tool_parameters,
            "timestamp": self.timestamp.isoformat(),
            "duration_before_error_ms": self.duration_before_error_ms,
            "is_retryable": self.is_retryable,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "suggested_fixes": self.suggested_fixes,
            "related_docs": self.related_docs,
            "environment": self.environment,
            "service_name": self.service_name,
            "host": self.host
        }
    
    def to_claude_format(self) -> str:
        """
        Format error for Claude Code to easily parse and understand.
        Returns a structured text format optimized for LLM consumption.
        """
        lines = [
            f"=== ERROR REPORT [{self.severity.value.upper()}] ===",
            f"ID: {self.error_id}",
            f"Time: {self.timestamp.isoformat()}",
            f"Category: {self.category.value}",
            "",
            f"[ERROR] {self.error_type}: {self.error_message}",
            ""
        ]
        
        if self.agent_name:
            lines.append(f"Agent: {self.agent_name} ({self.agent_id})")
        if self.operation:
            lines.append(f"Operation: {self.operation}")
        if self.trace_id:
            lines.append(f"Trace: {self.trace_id}")
        if self.span_id:
            lines.append(f"Span: {self.span_id}")
        
        if self.model_name:
            lines.extend(["", "[LLM CONTEXT]"])
            lines.append(f"Model: {self.model_name}")
            if self.token_count:
                lines.append(f"Tokens: {self.token_count}")
            if self.prompt_preview:
                lines.append(f"Prompt preview: {self.prompt_preview[:200]}...")
        
        if self.tool_name:
            lines.extend(["", "[TOOL CONTEXT]"])
            lines.append(f"Tool: {self.tool_name}")
            if self.tool_parameters:
                lines.append(f"Parameters: {json.dumps(self.tool_parameters, default=str)[:200]}")
        
        if self.stack_trace:
            lines.extend(["", "[STACK TRACE]", self.stack_trace])
        
        if self.suggested_fixes:
            lines.extend(["", "[SUGGESTED FIXES]"])
            for i, fix in enumerate(self.suggested_fixes, 1):
                lines.append(f"  {i}. {fix}")
        
        if self.related_docs:
            lines.extend(["", "[RELATED DOCUMENTATION]"])
            for doc in self.related_docs:
                lines.append(f"  - {doc}")
        
        lines.extend([
            "",
            f"Retryable: {self.is_retryable} (attempts: {self.retry_count}/{self.max_retries})",
            "=== END ERROR REPORT ==="
        ])
        
        return "\n".join(lines)
    
    @classmethod
    def from_exception(
        cls,
        exception: Exception,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        operation: Optional[str] = None,
        **kwargs
    ) -> 'DetailedError':
        """Create a DetailedError from an exception with automatic classification"""
        import traceback
        
        error_type = type(exception).__name__
        error_message = str(exception)
        stack_trace = traceback.format_exc()
        
        # Auto-classify the error
        category = ErrorCategory.UNKNOWN
        severity = ErrorSeverity.ERROR
        suggested_fixes = []
        is_retryable = False
        
        # LLM-related errors
        if any(x in error_type.lower() for x in ['ratelimit', 'quota', 'token']):
            category = ErrorCategory.LLM_ERROR
            is_retryable = True
            suggested_fixes = [
                "Wait and retry - rate limit may reset",
                "Check API quota and billing status",
                "Consider using a different model or provider"
            ]
        elif any(x in error_type.lower() for x in ['timeout', 'timedout']):
            category = ErrorCategory.TIMEOUT_ERROR
            is_retryable = True
            suggested_fixes = [
                "Increase timeout configuration",
                "Check network connectivity",
                "Reduce request complexity"
            ]
        elif any(x in error_type.lower() for x in ['connection', 'network', 'http']):
            category = ErrorCategory.NETWORK_ERROR
            is_retryable = True
            suggested_fixes = [
                "Check network connectivity",
                "Verify service URLs are correct",
                "Check if required services are running"
            ]
        elif any(x in error_type.lower() for x in ['validation', 'invalid', 'schema']):
            category = ErrorCategory.VALIDATION_ERROR
            suggested_fixes = [
                "Check input data format",
                "Verify required fields are present",
                "Review data type requirements"
            ]
        elif any(x in error_type.lower() for x in ['memory', 'resource', 'oom']):
            category = ErrorCategory.RESOURCE_ERROR
            severity = ErrorSeverity.CRITICAL
            suggested_fixes = [
                "Reduce batch size or input size",
                "Free up system resources",
                "Scale up infrastructure"
            ]
        
        return cls(
            error_type=error_type,
            error_message=error_message,
            stack_trace=stack_trace,
            category=category,
            severity=severity,
            suggested_fixes=suggested_fixes,
            is_retryable=is_retryable,
            trace_id=trace_id,
            span_id=span_id,
            agent_name=agent_name,
            operation=operation,
            **kwargs
        )


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class TokenUsage:
    """Token usage metrics for LLM calls"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens
        }


@dataclass
class LLMMessage:
    """Individual message in LLM conversation"""
    role: MessageRole
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "role": self.role.value,
            "content": self.content
        }
        if self.name:
            result["name"] = self.name
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            result["tool_calls"] = self.tool_calls
        return result


@dataclass
class ToolDefinition:
    """Definition of a tool available to an agent"""
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


@dataclass
class ToolInvocation:
    """Record of a tool being invoked"""
    tool_name: str
    tool_id: str = field(default_factory=lambda: str(uuid4()))
    input_parameters: Dict[str, Any] = field(default_factory=dict)
    output: Optional[Any] = None
    error: Optional[str] = None
    start_time: datetime = field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "tool_name": self.tool_name,
            "input_parameters": self.input_parameters,
            "output": self.output,
            "error": self.error,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms
        }


@dataclass
class AgentSpan:
    """
    Represents a span of agent execution.
    Based on OpenTelemetry semantic conventions for GenAI agents.
    """
    # Identifiers
    span_id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    parent_span_id: Optional[str] = None
    
    # Agent information
    agent_id: str = ""
    agent_name: str = ""
    agent_type: str = ""
    agent_version: str = "1.0.0"
    
    # Span metadata
    span_kind: SpanKind = SpanKind.AGENT
    status: AgentStatus = AgentStatus.RUNNING
    
    # Timing
    start_time: datetime = field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    duration_ms: Optional[float] = None
    
    # Input/Output
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)
    
    # LLM specific
    model_name: Optional[str] = None
    model_provider: Optional[str] = None
    messages: List[LLMMessage] = field(default_factory=list)
    response: Optional[str] = None
    token_usage: Optional[TokenUsage] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    
    # Tool usage
    tools_available: List[ToolDefinition] = field(default_factory=list)
    tool_invocations: List[ToolInvocation] = field(default_factory=list)
    
    # Error handling
    error_message: Optional[str] = None
    error_type: Optional[str] = None
    error_stack: Optional[str] = None
    
    # Custom attributes
    attributes: Dict[str, Any] = field(default_factory=dict)
    
    # Events within the span
    events: List[Dict[str, Any]] = field(default_factory=list)
    
    def add_event(self, name: str, attributes: Dict[str, Any] = None):
        """Add an event to this span"""
        self.events.append({
            "name": name,
            "timestamp": datetime.utcnow().isoformat(),
            "attributes": attributes or {}
        })
    
    def set_error(self, error: Exception):
        """Set error information on the span"""
        self.status = AgentStatus.FAILED
        self.error_message = str(error)
        self.error_type = type(error).__name__
        import traceback
        self.error_stack = traceback.format_exc()
    
    def complete(self, output: Dict[str, Any] = None):
        """Mark the span as completed"""
        self.end_time = datetime.utcnow()
        self.duration_ms = (self.end_time - self.start_time).total_seconds() * 1000
        self.status = AgentStatus.COMPLETED
        if output:
            self.output_data = output
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "agent_type": self.agent_type,
            "agent_version": self.agent_version,
            "span_kind": self.span_kind.value,
            "status": self.status.value,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_ms": self.duration_ms,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "model_name": self.model_name,
            "model_provider": self.model_provider,
            "messages": [m.to_dict() for m in self.messages],
            "response": self.response,
            "token_usage": self.token_usage.to_dict() if self.token_usage else None,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "tools_available": [t.to_dict() for t in self.tools_available],
            "tool_invocations": [t.to_dict() for t in self.tool_invocations],
            "error_message": self.error_message,
            "error_type": self.error_type,
            "error_stack": self.error_stack,
            "attributes": self.attributes,
            "events": self.events
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


@dataclass
class AgentHandoff:
    """Represents a handoff between agents"""
    handoff_id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    
    source_agent_id: str = ""
    source_agent_name: str = ""
    target_agent_id: str = ""
    target_agent_name: str = ""
    
    reason: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "trace_id": self.trace_id,
            "source_agent_id": self.source_agent_id,
            "source_agent_name": self.source_agent_name,
            "target_agent_id": self.target_agent_id,
            "target_agent_name": self.target_agent_name,
            "reason": self.reason,
            "context": self.context,
            "timestamp": self.timestamp.isoformat()
        }


@dataclass
class AgentTrace:
    """
    Complete trace of a multi-agent interaction.
    Contains all spans and handoffs for debugging.
    """
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    
    # Root span info
    root_agent_id: str = ""
    root_agent_name: str = ""
    
    # All spans in the trace
    spans: List[AgentSpan] = field(default_factory=list)
    
    # Agent handoffs
    handoffs: List[AgentHandoff] = field(default_factory=list)
    
    # Timing
    start_time: datetime = field(default_factory=datetime.utcnow)
    end_time: Optional[datetime] = None
    total_duration_ms: Optional[float] = None
    
    # Aggregated metrics
    total_tokens: int = 0
    total_cost: float = 0.0
    agent_count: int = 0
    tool_call_count: int = 0
    llm_call_count: int = 0
    
    # Status
    status: AgentStatus = AgentStatus.RUNNING
    error_message: Optional[str] = None
    
    # Input/Output
    user_input: str = ""
    final_output: str = ""
    
    # Metadata
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def add_span(self, span: AgentSpan):
        """Add a span to the trace"""
        span.trace_id = self.trace_id
        self.spans.append(span)
        
        # Update metrics
        if span.token_usage:
            self.total_tokens += span.token_usage.total_tokens
        if span.span_kind == SpanKind.LLM_CALL:
            self.llm_call_count += 1
        if span.tool_invocations:
            self.tool_call_count += len(span.tool_invocations)
    
    def add_handoff(self, handoff: AgentHandoff):
        """Add a handoff to the trace"""
        handoff.trace_id = self.trace_id
        self.handoffs.append(handoff)
    
    def complete(self, output: str = ""):
        """Mark the trace as completed"""
        self.end_time = datetime.utcnow()
        self.total_duration_ms = (self.end_time - self.start_time).total_seconds() * 1000
        self.status = AgentStatus.COMPLETED
        self.final_output = output
        
        # Count unique agents
        unique_agents = set(span.agent_id for span in self.spans if span.agent_id)
        self.agent_count = len(unique_agents)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "root_agent_id": self.root_agent_id,
            "root_agent_name": self.root_agent_name,
            "spans": [s.to_dict() for s in self.spans],
            "handoffs": [h.to_dict() for h in self.handoffs],
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "total_duration_ms": self.total_duration_ms,
            "total_tokens": self.total_tokens,
            "total_cost": self.total_cost,
            "agent_count": self.agent_count,
            "tool_call_count": self.tool_call_count,
            "llm_call_count": self.llm_call_count,
            "status": self.status.value,
            "error_message": self.error_message,
            "user_input": self.user_input,
            "final_output": self.final_output,
            "metadata": self.metadata
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


@dataclass
class DebugEvent:
    """
    Debug event for real-time debugging in Claude Code.
    These events are streamed via Kafka for live debugging.
    """
    event_id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str = ""
    span_id: Optional[str] = None
    
    event_type: str = ""  # e.g., "agent_start", "llm_call", "tool_invoke", "error", "handoff"
    severity: str = "info"  # info, warning, error, debug
    
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp.isoformat()
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# ============================================================================
# Inter-Agent Communication Schemas
# ============================================================================

class MessageType(str, Enum):
    """Types of messages exchanged between agents via Kafka queues"""
    TASK = "task"             # Task assignment from one agent to another
    RESULT = "result"         # Result returned to the requesting agent
    ERROR = "error"           # Error notification to the requesting agent
    HEARTBEAT = "heartbeat"   # Agent liveness signal
    CANCEL = "cancel"         # Task cancellation request


class MessagePriority(str, Enum):
    """Priority levels for inter-agent messages"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# Prefix for per-agent Kafka queue topics
AGENT_QUEUE_PREFIX = "agent-queue-"

# Default agent names whose queues are pre-created
DEFAULT_AGENT_NAMES = ["orchestrator", "researcher", "writer", "coder", "reviewer"]


def agent_queue_topic(agent_name: str) -> str:
    """
    Generate a Kafka topic name for an agent's personal queue.
    
    Pattern: agent-queue-{agent_name_lowercase}
    Example: agent_queue_topic("Researcher") -> "agent-queue-researcher"
    """
    return f"{AGENT_QUEUE_PREFIX}{agent_name.lower().replace(' ', '-')}"


@dataclass
class AgentMessage:
    """
    A message exchanged between agents via their Kafka queues.
    
    This is the unit of inter-agent communication.  The routing pattern is:
        Agent1 writes -> agent-queue-{Agent2}   (Agent2's queue)
        Agent2 reads  <- agent-queue-{Agent2}   (its own queue)
    
    When Agent2 finishes, it writes the result back:
        Agent2 writes -> agent-queue-{Agent1}   (Agent1's queue)
        Agent1 reads  <- agent-queue-{Agent1}   (its own queue)
    """
    # Identifiers
    message_id: str = field(default_factory=lambda: str(uuid4()))
    
    # Routing
    source_agent: str = ""          # Name of the sending agent
    target_agent: str = ""          # Name of the receiving agent
    
    # Message classification
    message_type: MessageType = MessageType.TASK
    priority: MessagePriority = MessagePriority.NORMAL
    
    # Payload
    payload: Dict[str, Any] = field(default_factory=dict)
    
    # Tracing context - links queue messages to telemetry traces
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_message_id: Optional[str] = None  # For result → links back to original task
    
    # Metadata
    reason: str = ""                # Human-readable reason for the message
    timestamp: datetime = field(default_factory=datetime.utcnow)
    ttl_seconds: Optional[int] = None  # Time-to-live; None = no expiry
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "source_agent": self.source_agent,
            "target_agent": self.target_agent,
            "message_type": self.message_type.value,
            "priority": self.priority.value,
            "payload": self.payload,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_message_id": self.parent_message_id,
            "reason": self.reason,
            "timestamp": self.timestamp.isoformat(),
            "ttl_seconds": self.ttl_seconds
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentMessage':
        """Reconstruct an AgentMessage from a Kafka consumer dict"""
        return cls(
            message_id=data.get("message_id", str(uuid4())),
            source_agent=data.get("source_agent", ""),
            target_agent=data.get("target_agent", ""),
            message_type=MessageType(data.get("message_type", "task")),
            priority=MessagePriority(data.get("priority", "normal")),
            payload=data.get("payload", {}),
            trace_id=data.get("trace_id"),
            span_id=data.get("span_id"),
            parent_message_id=data.get("parent_message_id"),
            reason=data.get("reason", ""),
            timestamp=datetime.fromisoformat(data["timestamp"]) if "timestamp" in data else datetime.utcnow(),
            ttl_seconds=data.get("ttl_seconds")
        )
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# ============================================================================
# Kafka message schemas
# ============================================================================

@dataclass
class KafkaMessage:
    """Wrapper for Kafka messages"""
    topic: str
    key: str
    value: Dict[str, Any]
    headers: Dict[str, str] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "key": self.key,
            "value": self.value,
            "headers": self.headers,
            "timestamp": self.timestamp.isoformat()
        }


# Telemetry topic — only errors are sent to Kafka.
# Inter-agent communication uses agent-queue-{name} topics (see AGENT_QUEUE_PREFIX).
KAFKA_TOPICS = {
    "errors": "agent-telemetry-errors"
}
