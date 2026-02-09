"""
FastAPI Service for Multi-Agent Telemetry
Provides REST API and WebSocket for real-time debugging
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

from aiokafka import AIOKafkaConsumer
from jsonschema import Draft7Validator

# Import new telemetry modules
from ..telemetry.sampling import (
    TelemetrySampler, SamplingConfig, RateLimitConfig,
    SamplingDecision, get_default_sampler, configure_sampler
)
from ..telemetry.otlp_exporter import (
    OTLPExporter, OTLPExporterConfig, create_otlp_exporter,
    TraceContextPropagator, W3CTraceContext
)
from ..telemetry.claude_integration import (
    ClaudeDiagnosticAnalyzer, DiagnosticBundle,
    get_diagnostic_bundle
)
from ..oracle.state_schema import ORACLE_MONITOR_SCHEMA
from ..telemetry.schemas import (
    AGENT_QUEUE_PREFIX, DEFAULT_AGENT_NAMES, agent_queue_topic,
    AgentMessage
)

logger = logging.getLogger(__name__)

def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _oracle_strict_schema_default() -> bool:
    return _env_bool("ORACLE_STRICT_SCHEMA", True)

def _format_schema_error(err: Exception) -> Dict[str, Any]:
    path = "/".join(str(p) for p in getattr(err, "path", []))
    return {
        "path": path,
        "message": getattr(err, "message", str(err)),
        "validator": getattr(err, "validator", None),
        "validator_value": getattr(err, "validator_value", None)
    }

# Global instances for new features
sampler: Optional[TelemetrySampler] = None
otlp_exporter: Optional[OTLPExporter] = None
diagnostic_analyzer: Optional[ClaudeDiagnosticAnalyzer] = None


# Pydantic models for API
class TraceQuery(BaseModel):
    trace_id: Optional[str] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    status: Optional[str] = None
    limit: int = 100


class SpanQuery(BaseModel):
    trace_id: str
    span_kind: Optional[str] = None
    agent_name: Optional[str] = None
    status: Optional[str] = None


class DebugSubscription(BaseModel):
    trace_ids: List[str] = []
    agent_names: List[str] = []
    event_types: List[str] = []


# Persistent storage with time-machine JSONL files
class TelemetryStore:
    def __init__(self, data_dir: str = "./telemetry_data"):
        self.traces: Dict[str, Dict] = {}
        self.spans: Dict[str, Dict] = {}
        self.events: List[Dict] = []
        self.handoffs: List[Dict] = []
        self.errors: List[Dict] = []       # Dedicated error storage
        self.metrics: List[Dict] = []      # Dedicated metrics storage
        self.agent_messages: List[Dict] = []  # Inter-agent queue messages (observed)
        
        # Time-machine persistence
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state_time_machine.jsonl"
        self.diff_file = self.data_dir / "diff_time_machine.jsonl"
        
        # Track previous state for diffs
        self._prev_state = self._get_current_state()
    
    def _get_current_state(self) -> Dict:
        """Get current full state snapshot"""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "traces_count": len(self.traces),
            "spans_count": len(self.spans),
            "events_count": len(self.events),
            "handoffs_count": len(self.handoffs),
            "errors_count": len(self.errors),
            "metrics_count": len(self.metrics),
            "agent_messages_count": len(self.agent_messages),
            "errors": self.errors[-100:],  # Last 100 errors
            "agent_messages": self.agent_messages[-100:]  # Last 100 messages
        }
    
    def _write_state_snapshot(self):
        """Write full state snapshot to state_time_machine.jsonl"""
        state = self._get_current_state()
        with open(self.state_file, "a") as f:
            f.write(json.dumps(state) + "\n")
    
    def _write_diff(self, change_type: str, entity_type: str, data: Dict):
        """Write incremental diff to diff_time_machine.jsonl"""
        diff = {
            "timestamp": datetime.utcnow().isoformat(),
            "change_type": change_type,  # "added", "updated", "deleted"
            "entity_type": entity_type,  # "error", "agent_message", "trace", etc.
            "data": data
        }
        with open(self.diff_file, "a") as f:
            f.write(json.dumps(diff) + "\n")
    
    def add_trace(self, trace: Dict):
        self.traces[trace["trace_id"]] = trace
        self._write_diff("added", "trace", trace)
    
    def add_span(self, span: Dict):
        self.spans[span["span_id"]] = span
        self._write_diff("added", "span", span)
    
    def add_event(self, event: Dict):
        self.events.append(event)
        if len(self.events) > 10000:
            self.events = self.events[-10000:]
        self._write_diff("added", "event", event)
    
    def add_handoff(self, handoff: Dict):
        self.handoffs.append(handoff)
        self._write_diff("added", "handoff", handoff)
    
    def add_error(self, error: Dict):
        """Store a DetailedError in the internal error store + persist to disk"""
        self.errors.append(error)
        if len(self.errors) > 5000:
            self.errors = self.errors[-5000:]
        self._write_diff("added", "error", error)
        # Write state snapshot every 10 errors
        if len(self.errors) % 10 == 0:
            self._write_state_snapshot()
    
    def add_metric(self, metric: Dict):
        """Store a metric"""
        self.metrics.append(metric)
        if len(self.metrics) > 10000:
            self.metrics = self.metrics[-10000:]
        self._write_diff("added", "metric", metric)
    
    def add_agent_message(self, message: Dict):
        """Store an observed inter-agent queue message + persist to disk"""
        self.agent_messages.append(message)
        if len(self.agent_messages) > 10000:
            self.agent_messages = self.agent_messages[-10000:]
        self._write_diff("added", "agent_message", message)
        # Write state snapshot every 50 messages
        if len(self.agent_messages) % 50 == 0:
            self._write_state_snapshot()
    
    def get_agent_queue_messages(self, agent_name: str) -> List[Dict]:
        """Get all messages targeted at a specific agent's queue"""
        return [
            m for m in self.agent_messages
            if m.get("target_agent", "").lower() == agent_name.lower()
        ]
    
    def get_agent_queue_pending(self, agent_name: str) -> List[Dict]:
        """Get pending (unresolved) task messages for an agent"""
        # A task is pending if there is no result message with the same parent_message_id
        tasks = [
            m for m in self.agent_messages
            if m.get("target_agent", "").lower() == agent_name.lower()
            and m.get("message_type") == "task"
        ]
        result_parent_ids = {
            m.get("parent_message_id")
            for m in self.agent_messages
            if m.get("message_type") in ("result", "error")
            and m.get("parent_message_id")
        }
        return [t for t in tasks if t.get("message_id") not in result_parent_ids]
    
    def get_all_queue_states(self) -> List[Dict[str, Any]]:
        """Get a summary of all agent queues"""
        agents = set()
        for m in self.agent_messages:
            agents.add(m.get("source_agent", "").lower())
            agents.add(m.get("target_agent", "").lower())
        agents.discard("")
        
        queues = []
        for agent in sorted(agents):
            pending = self.get_agent_queue_pending(agent)
            total = len(self.get_agent_queue_messages(agent))
            queues.append({
                "agent_name": agent,
                "queue_topic": f"{AGENT_QUEUE_PREFIX}{agent}",
                "total_messages": total,
                "pending_tasks": len(pending),
                "pending": pending
            })
        return queues
    
    def get_trace(self, trace_id: str) -> Optional[Dict]:
        return self.traces.get(trace_id)
    
    def get_spans_for_trace(self, trace_id: str) -> List[Dict]:
        return [s for s in self.spans.values() if s.get("trace_id") == trace_id]
    
    def get_events_for_trace(self, trace_id: str) -> List[Dict]:
        return [e for e in self.events if e.get("trace_id") == trace_id]
    
    def get_handoffs_for_trace(self, trace_id: str) -> List[Dict]:
        return [h for h in self.handoffs if h.get("trace_id") == trace_id]
    
    def get_errors_for_trace(self, trace_id: str) -> List[Dict]:
        """Get all errors for a specific trace"""
        return [e for e in self.errors if e.get("trace_id") == trace_id]
    
    def get_errors_by_severity(self, severity: str) -> List[Dict]:
        """Get errors filtered by severity (critical, error, warning, info)"""
        return [e for e in self.errors if e.get("severity") == severity]
    
    def get_errors_by_category(self, category: str) -> List[Dict]:
        """Get errors filtered by category (llm_error, agent_error, etc.)"""
        return [e for e in self.errors if e.get("category") == category]
    
    def get_errors_by_agent(self, agent_name: str) -> List[Dict]:
        """Get errors filtered by agent name"""
        return [e for e in self.errors if e.get("agent_name") == agent_name]
    
    def get_recent_errors(self, limit: int = 50) -> List[Dict]:
        """Get most recent errors"""
        return self.errors[-limit:]
    
    def get_error_summary(self) -> Dict[str, Any]:
        """Get a summary of all stored errors for debugging"""
        total = len(self.errors)
        by_severity = {}
        by_category = {}
        by_agent = {}
        
        for error in self.errors:
            sev = error.get("severity", "unknown")
            cat = error.get("category", "unknown")
            agent = error.get("agent_name", "unknown")
            
            by_severity[sev] = by_severity.get(sev, 0) + 1
            by_category[cat] = by_category.get(cat, 0) + 1
            by_agent[agent] = by_agent.get(agent, 0) + 1
        
        return {
            "total_errors": total,
            "by_severity": by_severity,
            "by_category": by_category,
            "by_agent": by_agent
        }
    
    def get_metrics_for_trace(self, trace_id: str) -> List[Dict]:
        """Get all metrics for a specific trace"""
        return [m for m in self.metrics if m.get("trace_id") == trace_id]
    
    def search_traces(self, query: TraceQuery) -> List[Dict]:
        results = list(self.traces.values())
        
        if query.trace_id:
            results = [t for t in results if t["trace_id"] == query.trace_id]
        if query.session_id:
            results = [t for t in results if t.get("session_id") == query.session_id]
        if query.user_id:
            results = [t for t in results if t.get("user_id") == query.user_id]
        if query.status:
            results = [t for t in results if t.get("status") == query.status]
        
        # Sort by start_time descending
        results.sort(key=lambda x: x.get("start_time", ""), reverse=True)
        
        return results[:query.limit]


# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.subscriptions: Dict[str, DebugSubscription] = {}
    
    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        self.subscriptions[client_id] = DebugSubscription()
    
    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
        if client_id in self.subscriptions:
            del self.subscriptions[client_id]
    
    def set_subscription(self, client_id: str, subscription: DebugSubscription):
        self.subscriptions[client_id] = subscription
    
    async def broadcast(self, message: Dict):
        """Broadcast to all connected clients based on their subscriptions"""
        for client_id, websocket in list(self.active_connections.items()):
            try:
                subscription = self.subscriptions.get(client_id, DebugSubscription())
                
                # Filter by subscription
                if subscription.trace_ids and message.get("trace_id") not in subscription.trace_ids:
                    continue
                if subscription.agent_names and message.get("agent_name") not in subscription.agent_names:
                    continue
                if subscription.event_types and message.get("event_type") not in subscription.event_types:
                    continue
                
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Error sending to {client_id}: {e}")
                self.disconnect(client_id)
    
    async def send_to_client(self, client_id: str, message: Dict):
        """Send message to specific client"""
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_json(message)


# Global instances
store = TelemetryStore()
manager = ConnectionManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    global sampler, otlp_exporter, diagnostic_analyzer

    # Initialize sampler with default config
    sampler = configure_sampler(
        SamplingConfig(
            head_sample_rate=1.0,  # Sample everything by default
            tail_sample_enabled=True,
            always_sample_errors=True,
            always_sample_slow_traces=True,
            slow_trace_threshold_ms=5000.0
        ),
        RateLimitConfig(
            traces_per_second=100.0,
            spans_per_second=1000.0,
            events_per_second=500.0
        )
    )
    logger.info("Telemetry sampler initialized")

    # Initialize OTLP exporter (optional, based on env var)
    otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        otlp_exporter = create_otlp_exporter(
            endpoint=otlp_endpoint,
            service_name=os.environ.get("OTEL_SERVICE_NAME", "multi-agent-telemetry")
        )
        await otlp_exporter.start()
        logger.info(f"OTLP exporter started, endpoint: {otlp_endpoint}")

    # Initialize diagnostic analyzer
    diagnostic_analyzer = ClaudeDiagnosticAnalyzer(
        slow_trace_threshold_ms=5000.0,
        error_rate_threshold=0.1
    )
    logger.info("Claude diagnostic analyzer initialized")

    # Start Kafka consumer — observe inter-agent queues only
    # No telemetry data goes to Kafka; errors are stored internally via REST API
    kafka_servers = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    
    # Agent queue topics (inter-agent communication — observed read-only)
    topics = [agent_queue_topic(name) for name in DEFAULT_AGENT_NAMES]

    consumer_task = None

    try:
        consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=kafka_servers,
            group_id="telemetry-api-consumer",
            value_deserializer=lambda v: json.loads(v.decode('utf-8')),
            auto_offset_reset='earliest'
        )
        await consumer.start()
        
        async def consume_messages():
            async for message in consumer:
                topic = message.topic
                value = message.value
                
                # All Kafka topics are agent queues — observe for monitoring
                if topic.startswith(AGENT_QUEUE_PREFIX):
                    store.add_agent_message(value)
                    await manager.broadcast({
                        "type": "agent_queue_message",
                        "queue_topic": topic,
                        "message_type": value.get("message_type", "unknown"),
                        "source_agent": value.get("source_agent", ""),
                        "target_agent": value.get("target_agent", ""),
                        **value
                    })
        
        consumer_task = asyncio.create_task(consume_messages())
        logger.info(f"Kafka consumer started, subscribed to {len(topics)} topics")
        
    except Exception as e:
        logger.warning(f"Kafka not available: {e}. Running in standalone mode.")
    
    yield

    # Cleanup
    if consumer_task:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass

    # Cleanup OTLP exporter
    if otlp_exporter:
        await otlp_exporter.stop()
        logger.info("OTLP exporter stopped")


# Create FastAPI app
app = FastAPI(
    title="Multi-Agent Telemetry API",
    description="API for debugging multi-agent systems",
    version="1.0.0",
    lifespan=lifespan
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# REST API Endpoints

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "active_connections": len(manager.active_connections)
    }


@app.get("/api/v1/traces")
async def list_traces(
    trace_id: Optional[str] = None,
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=100, le=1000)
):
    """List traces with optional filtering"""
    query = TraceQuery(
        trace_id=trace_id,
        session_id=session_id,
        user_id=user_id,
        status=status,
        limit=limit
    )
    return {"traces": store.search_traces(query)}


@app.get("/api/v1/traces/{trace_id}")
async def get_trace(trace_id: str):
    """Get a specific trace with all its data"""
    trace = store.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    
    return {
        "trace": trace,
        "spans": store.get_spans_for_trace(trace_id),
        "events": store.get_events_for_trace(trace_id),
        "handoffs": store.get_handoffs_for_trace(trace_id)
    }


@app.get("/api/v1/traces/{trace_id}/spans")
async def get_trace_spans(trace_id: str):
    """Get all spans for a trace"""
    spans = store.get_spans_for_trace(trace_id)
    return {"spans": spans}


@app.get("/api/v1/traces/{trace_id}/timeline")
async def get_trace_timeline(trace_id: str):
    """Get a timeline view of the trace"""
    spans = store.get_spans_for_trace(trace_id)
    events = store.get_events_for_trace(trace_id)
    handoffs = store.get_handoffs_for_trace(trace_id)
    
    # Combine into timeline
    timeline = []
    
    for span in spans:
        timeline.append({
            "type": "span_start",
            "timestamp": span.get("start_time"),
            "data": {
                "span_id": span.get("span_id"),
                "agent_name": span.get("agent_name"),
                "span_kind": span.get("span_kind")
            }
        })
        if span.get("end_time"):
            timeline.append({
                "type": "span_end",
                "timestamp": span.get("end_time"),
                "data": {
                    "span_id": span.get("span_id"),
                    "agent_name": span.get("agent_name"),
                    "duration_ms": span.get("duration_ms"),
                    "status": span.get("status")
                }
            })
    
    for event in events:
        timeline.append({
            "type": "event",
            "timestamp": event.get("timestamp"),
            "data": event
        })
    
    for handoff in handoffs:
        timeline.append({
            "type": "handoff",
            "timestamp": handoff.get("timestamp"),
            "data": handoff
        })
    
    # Sort by timestamp
    timeline.sort(key=lambda x: x.get("timestamp", ""))
    
    return {"timeline": timeline}


@app.get("/api/v1/traces/{trace_id}/graph")
async def get_trace_graph(trace_id: str):
    """Get a graph representation of agent interactions"""
    spans = store.get_spans_for_trace(trace_id)
    handoffs = store.get_handoffs_for_trace(trace_id)
    
    # Build nodes (agents)
    nodes = {}
    for span in spans:
        agent_id = span.get("agent_id")
        if agent_id and agent_id not in nodes:
            nodes[agent_id] = {
                "id": agent_id,
                "name": span.get("agent_name"),
                "type": span.get("agent_type"),
                "span_count": 0,
                "total_duration_ms": 0
            }
        if agent_id:
            nodes[agent_id]["span_count"] += 1
            nodes[agent_id]["total_duration_ms"] += span.get("duration_ms", 0)
    
    # Build edges (handoffs and parent-child relationships)
    edges = []
    for handoff in handoffs:
        edges.append({
            "source": handoff.get("source_agent_id"),
            "target": handoff.get("target_agent_id"),
            "type": "handoff",
            "reason": handoff.get("reason")
        })
    
    # Add parent-child edges from spans
    for span in spans:
        if span.get("parent_span_id"):
            parent_span = store.spans.get(span.get("parent_span_id"))
            if parent_span:
                edges.append({
                    "source": parent_span.get("agent_id"),
                    "target": span.get("agent_id"),
                    "type": "parent_child"
                })
    
    return {
        "nodes": list(nodes.values()),
        "edges": edges
    }


@app.get("/api/v1/metrics")
async def get_metrics():
    """Get aggregated metrics"""
    traces = list(store.traces.values())
    spans = list(store.spans.values())
    
    # Calculate metrics
    total_traces = len(traces)
    completed_traces = len([t for t in traces if t.get("status") == "completed"])
    failed_traces = len([t for t in traces if t.get("status") == "failed"])
    
    total_tokens = sum(t.get("total_tokens", 0) for t in traces)
    total_llm_calls = sum(t.get("llm_call_count", 0) for t in traces)
    
    avg_duration = 0
    if completed_traces > 0:
        avg_duration = sum(
            t.get("total_duration_ms", 0) 
            for t in traces 
            if t.get("status") == "completed"
        ) / completed_traces
    
    return {
        "total_traces": total_traces,
        "completed_traces": completed_traces,
        "failed_traces": failed_traces,
        "success_rate": completed_traces / total_traces if total_traces > 0 else 0,
        "total_tokens": total_tokens,
        "total_llm_calls": total_llm_calls,
        "avg_duration_ms": avg_duration,
        "active_spans": len([s for s in spans if s.get("status") == "running"])
    }


# WebSocket endpoint for real-time debugging

@app.websocket("/ws/debug/{client_id}")
async def websocket_debug(websocket: WebSocket, client_id: str):
    """WebSocket endpoint for real-time debugging events"""
    await manager.connect(websocket, client_id)
    
    try:
        while True:
            # Receive subscription updates from client
            data = await websocket.receive_json()
            
            if data.get("type") == "subscribe":
                subscription = DebugSubscription(
                    trace_ids=data.get("trace_ids", []),
                    agent_names=data.get("agent_names", []),
                    event_types=data.get("event_types", [])
                )
                manager.set_subscription(client_id, subscription)
                await websocket.send_json({
                    "type": "subscription_updated",
                    "subscription": subscription.model_dump()
                })
            
            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                
    except WebSocketDisconnect:
        manager.disconnect(client_id)


# SSE endpoint for event streaming

@app.get("/api/v1/events/stream")
async def stream_events(trace_id: Optional[str] = None):
    """Server-Sent Events stream for debugging"""
    async def event_generator():
        last_index = len(store.events)
        
        while True:
            await asyncio.sleep(0.5)  # Poll interval
            
            # Get new events
            new_events = store.events[last_index:]
            last_index = len(store.events)
            
            for event in new_events:
                if trace_id and event.get("trace_id") != trace_id:
                    continue
                
                yield f"data: {json.dumps(event)}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream"
    )


# ============================================================================
# ERROR LOG ENDPOINTS - Dedicated error querying for Claude Code debugging
# ============================================================================

@app.get("/api/v1/errors")
async def list_errors(
    trace_id: Optional[str] = None,
    severity: Optional[str] = Query(default=None, enum=["critical", "error", "warning", "info"]),
    category: Optional[str] = Query(default=None, enum=[
        "llm_error", "agent_error", "tool_error", "handoff_error",
        "validation_error", "timeout_error", "resource_error",
        "network_error", "config_error", "unknown"
    ]),
    agent_name: Optional[str] = None,
    limit: int = Query(default=50, le=500)
):
    """
    List stored error logs with filtering.
    
    These errors are stored internally (not in Kafka) and contain rich
    DetailedError data including:
    - Stack traces
    - Error classification (severity + category)
    - Agent context (which agent, what operation)
    - LLM/tool context
    - Suggested fixes
    - Retry information
    
    Use this endpoint for systematic error investigation.
    """
    errors = store.errors
    
    if trace_id:
        errors = [e for e in errors if e.get("trace_id") == trace_id]
    if severity:
        errors = [e for e in errors if e.get("severity") == severity]
    if category:
        errors = [e for e in errors if e.get("category") == category]
    if agent_name:
        errors = [e for e in errors if e.get("agent_name") == agent_name]
    
    # Sort by timestamp descending (most recent first)
    errors.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    
    return {
        "total": len(errors),
        "errors": errors[:limit],
        "summary": store.get_error_summary()
    }


@app.get("/api/v1/errors/summary")
async def get_error_summary():
    """
    Get a summary of all stored errors.
    
    Returns counts grouped by severity, category, and agent.
    Designed for quick system health assessment.
    """
    return store.get_error_summary()


@app.get("/api/v1/errors/{error_id}")
async def get_error_detail(error_id: str):
    """
    Get full details for a specific error by error_id.
    
    Returns the complete DetailedError with stack trace,
    context, and suggested fixes.
    """
    for error in store.errors:
        if error.get("error_id") == error_id:
            return {"error": error}
    raise HTTPException(status_code=404, detail=f"Error {error_id} not found")


@app.get("/api/v1/traces/{trace_id}/errors")
async def get_trace_errors(trace_id: str):
    """
    Get all errors for a specific trace.
    
    Returns errors ordered by timestamp, with full DetailedError data
    for each error that occurred during the trace execution.
    """
    errors = store.get_errors_for_trace(trace_id)
    errors.sort(key=lambda x: x.get("timestamp", ""))
    
    return {
        "trace_id": trace_id,
        "error_count": len(errors),
        "errors": errors
    }


@app.get("/api/v1/errors/agent/{agent_name}")
async def get_agent_errors(
    agent_name: str,
    limit: int = Query(default=50, le=200)
):
    """
    Get all errors for a specific agent.
    
    Useful for debugging agent-specific issues like LLM failures,
    tool errors, or handoff problems for a particular agent.
    """
    errors = store.get_errors_by_agent(agent_name)
    errors.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    
    return {
        "agent_name": agent_name,
        "error_count": len(errors),
        "errors": errors[:limit]
    }


# ============================================================================
# METRICS ENDPOINTS - Performance metrics for debugging
# ============================================================================

@app.get("/api/v1/metrics/spans")
async def get_span_metrics(
    trace_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    limit: int = Query(default=100, le=1000)
):
    """
    Get span-level performance metrics from the agent-telemetry-metrics topic.
    
    Returns timing, token usage, and status for individual spans.
    """
    metrics = store.metrics
    
    if trace_id:
        metrics = [m for m in metrics if m.get("trace_id") == trace_id]
    if agent_name:
        metrics = [m for m in metrics if m.get("agent_name") == agent_name]
    
    metrics.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    
    return {
        "total": len(metrics),
        "metrics": metrics[:limit]
    }


# ============================================================================
# AGENT QUEUE ENDPOINTS - Inter-agent communication observability
# ============================================================================

@app.get("/api/v1/queues")
async def list_agent_queues():
    """
    List all observed agent queues and their current state.
    
    Each agent has a dedicated Kafka queue (agent-queue-{name}).
    This endpoint shows:
    - Total messages observed per queue
    - Pending (unresolved) tasks
    - Queue topic name
    """
    return {
        "queues": store.get_all_queue_states(),
        "total_messages": len(store.agent_messages)
    }


@app.get("/api/v1/queues/{agent_name}")
async def get_agent_queue(
    agent_name: str,
    message_type: Optional[str] = Query(default=None, enum=["task", "result", "error", "heartbeat", "cancel"]),
    limit: int = Query(default=50, le=500)
):
    """
    Get messages from a specific agent's queue.
    
    Pattern: Agent1 writes to agent-queue-{Agent2}, Agent2 reads from it.
    This endpoint shows what has been written to agent-queue-{agent_name}.
    """
    messages = store.get_agent_queue_messages(agent_name)
    
    if message_type:
        messages = [m for m in messages if m.get("message_type") == message_type]
    
    messages.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    pending = store.get_agent_queue_pending(agent_name)
    
    return {
        "agent_name": agent_name,
        "queue_topic": agent_queue_topic(agent_name),
        "total_messages": len(messages),
        "pending_tasks": len(pending),
        "messages": messages[:limit],
        "pending": pending[:limit]
    }


@app.get("/api/v1/queues/{agent_name}/pending")
async def get_agent_pending_tasks(agent_name: str):
    """
    Get only the pending (unresolved) tasks in an agent's queue.
    
    A task is pending if no result or error response has been observed
    with a matching parent_message_id.
    """
    pending = store.get_agent_queue_pending(agent_name)
    return {
        "agent_name": agent_name,
        "pending_count": len(pending),
        "tasks": pending
    }


# CLI debugging endpoints

@app.post("/api/v1/debug/inject-event")
async def inject_debug_event(event: Dict[str, Any]):
    """Inject a debug event (for testing)"""
    event["timestamp"] = datetime.utcnow().isoformat()
    event["event_id"] = event.get("event_id", str(datetime.utcnow().timestamp()))
    
    store.add_event(event)
    await manager.broadcast(event)
    
    return {"status": "injected", "event_id": event["event_id"]}


@app.post("/api/v1/debug/simulate-trace")
async def simulate_trace():
    """Simulate a trace for testing the dashboard"""
    from uuid import uuid4
    import random
    
    trace_id = str(uuid4())
    
    # Create simulated trace
    trace = {
        "trace_id": trace_id,
        "session_id": "test-session",
        "user_id": "test-user",
        "root_agent_name": "Orchestrator",
        "status": "completed",
        "start_time": datetime.utcnow().isoformat(),
        "end_time": datetime.utcnow().isoformat(),
        "total_duration_ms": 1500,
        "total_tokens": 1200,
        "agent_count": 3,
        "llm_call_count": 5,
        "user_input": "Test query",
        "final_output": "Test response"
    }
    store.add_trace(trace)
    
    # Create simulated spans and events
    agents = ["Orchestrator", "Researcher", "Writer"]
    event_types = ["agent_start", "llm_request", "llm_response", "tool_call", "agent_complete"]
    messages = [
        "Agent started processing request",
        "Calling LLM model ollama/llama2",
        "LLM response received (245 tokens)",
        "Executing tool: search_web",
        "Agent completed successfully"
    ]
    
    for i, agent in enumerate(agents):
        span_id = str(uuid4())
        span = {
            "span_id": span_id,
            "trace_id": trace_id,
            "agent_id": f"agent-{i}",
            "agent_name": agent,
            "span_kind": "agent",
            "status": "completed",
            "start_time": datetime.utcnow().isoformat(),
            "duration_ms": 500 + (i * 100),
            "token_usage": {
                "prompt_tokens": random.randint(100, 300),
                "completion_tokens": random.randint(50, 200),
                "total_tokens": random.randint(200, 500)
            }
        }
        store.add_span(span)
        
        # Create and broadcast events for this agent
        for j, (event_type, message) in enumerate(zip(event_types, messages)):
            event = {
                "event_id": str(uuid4()),
                "trace_id": trace_id,
                "span_id": span_id,
                "agent_id": f"agent-{i}",
                "agent_name": agent,
                "event_type": event_type,
                "message": f"{agent}: {message}",
                "timestamp": datetime.utcnow().isoformat(),
                "severity": "info",
                "data": {"step": j + 1, "agent_index": i}
            }
            store.add_event(event)
            # Broadcast to WebSocket clients
            await manager.broadcast(event)
            # Small delay between events for visual effect
            await asyncio.sleep(0.1)
    
    # Create handoffs
    handoffs = [
        ("Orchestrator", "Researcher", "Delegate research task"),
        ("Researcher", "Writer", "Pass research results for writing")
    ]
    for source, target, reason in handoffs:
        handoff = {
            "handoff_id": str(uuid4()),
            "trace_id": trace_id,
            "source_agent_id": f"agent-{agents.index(source)}",
            "source_agent_name": source,
            "target_agent_id": f"agent-{agents.index(target)}",
            "target_agent_name": target,
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat()
        }
        store.add_handoff(handoff)
        await manager.broadcast({
            "event_type": "handoff",
            "message": f"Handoff: {source} → {target} ({reason})",
            "agent_name": source,
            "timestamp": datetime.utcnow().isoformat(),
            **handoff
        })
    
    return {"trace_id": trace_id, "message": "Simulated trace created with events"}


# Oracle Monitor endpoints for unified system state
# These endpoints are designed for Claude Code integration

@app.get("/api/v1/oracle/state")
async def get_oracle_state(
    format: str = Query(default="json", enum=["json", "log", "summary"]),
    strict: Optional[bool] = Query(default=None)
):
    """
    Get unified Oracle Monitor system state.
    This endpoint provides a comprehensive view of:
    - All registered agents and their activity
    - Kubernetes workload metrics
    - Task queues and pending work
    - LLM model usage and limits
    
    Designed for Claude Code debugging integration.
    """
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator
        
        api_url = f"http://localhost:8080"  # Self-reference
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        
        aggregator = OracleMonitorAggregator(
            namespace="telemetry",
            api_url=api_url,
            ollama_url=ollama_url
        )
        
        state = await aggregator.get_state()
        
        strict_output = _oracle_strict_schema_default() if strict is None else strict
        if format == "log":
            return {"log": state.to_diff_log()}
        elif format == "summary":
            return state.get_summary()
        else:
            return state.to_dict(strict=strict_output)
            
    except Exception as e:
        logger.error(f"Error getting Oracle state: {e}")
        return {
            "error": str(e),
            "status": "error",
            "message": "Failed to aggregate system state"
        }

@app.get("/api/v1/oracle/schema")
async def get_oracle_schema():
    """Return the Oracle Monitor JSON schema."""
    return ORACLE_MONITOR_SCHEMA

@app.get("/api/v1/oracle/validate")
async def validate_oracle_state():
    """Validate current Oracle state against the JSON schema."""
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator

        api_url = "http://localhost:8080"
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")

        aggregator = OracleMonitorAggregator(
            namespace="telemetry",
            api_url=api_url,
            ollama_url=ollama_url
        )

        state = await aggregator.get_state()
        state_dict = state.to_dict(strict=True)

        validator = Draft7Validator(ORACLE_MONITOR_SCHEMA)
        errors = sorted(validator.iter_errors(state_dict), key=lambda e: list(e.path))
        if errors:
            return {
                "valid": False,
                "error_count": len(errors),
                "errors": [_format_schema_error(e) for e in errors]
            }

        return {"valid": True, "error_count": 0, "errors": []}
    except Exception as e:
        logger.error(f"Error validating Oracle state: {e}")
        return {"valid": False, "error": str(e), "error_count": None, "errors": []}

@app.get("/api/v1/oracle/agents")
async def get_oracle_agents():
    """Get all registered agents and their current activity"""
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator
        
        aggregator = OracleMonitorAggregator(
            api_url="http://localhost:8080",
            ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        )
        
        state = await aggregator.get_state()
        return {"agents": [a.to_dict() for a in state.agents]}
        
    except Exception as e:
        logger.error(f"Error getting agents: {e}")
        return {"error": str(e), "agents": []}


@app.get("/api/v1/oracle/workload")
async def get_oracle_workload():
    """Get Kubernetes workload metrics for all deployments"""
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator
        
        aggregator = OracleMonitorAggregator(
            api_url="http://localhost:8080",
            ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        )
        
        state = await aggregator.get_state()
        return {"workload": [w.to_dict() for w in state.workload]}
        
    except Exception as e:
        logger.error(f"Error getting workload: {e}")
        return {"error": str(e), "workload": []}


@app.get("/api/v1/oracle/llm")
async def get_oracle_llm():
    """Get LLM model configurations and usage metrics"""
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator
        
        aggregator = OracleMonitorAggregator(
            api_url="http://localhost:8080",
            ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        )
        
        state = await aggregator.get_state()
        return {"litellm": [m.to_dict() for m in state.litellm]}
        
    except Exception as e:
        logger.error(f"Error getting LLM models: {e}")
        return {"error": str(e), "litellm": []}


@app.get("/api/v1/oracle/issues")
async def get_oracle_issues():
    """Get current system issues for quick debugging"""
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator
        
        aggregator = OracleMonitorAggregator(
            api_url="http://localhost:8080",
            ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        )
        
        state = await aggregator.get_state()
        summary = state.get_summary()
        
        return {
            "status": summary["status"],
            "issues": summary["issues"],
            "agent_count": summary["agent_count"],
            "active_tasks": summary["active_tasks"],
            "queued_tasks": summary["queued_tasks"]
        }
        
    except Exception as e:
        logger.error(f"Error getting issues: {e}")
        return {"error": str(e), "status": "error", "issues": [str(e)]}


# ============================================================================
# SAMPLING AND RATE LIMITING ENDPOINTS
# ============================================================================

@app.get("/api/v1/sampling/metrics")
async def get_sampling_metrics():
    """Get current sampling metrics and statistics"""
    if not sampler:
        return {"error": "Sampler not initialized", "metrics": {}}

    return {
        "status": "active",
        "metrics": sampler.get_metrics(),
        "config": {
            "head_sample_rate": sampler.sampling_config.head_sample_rate,
            "tail_sample_enabled": sampler.sampling_config.tail_sample_enabled,
            "slow_trace_threshold_ms": sampler.sampling_config.slow_trace_threshold_ms,
            "always_sample_errors": sampler.sampling_config.always_sample_errors
        }
    }


@app.post("/api/v1/sampling/configure")
async def configure_sampling(config: Dict[str, Any]):
    """
    Update sampling configuration dynamically.

    Example:
    {
        "head_sample_rate": 0.5,
        "slow_trace_threshold_ms": 3000,
        "debug_trace_ids": ["trace-123", "trace-456"]
    }
    """
    global sampler

    if not sampler:
        return {"error": "Sampler not initialized"}

    # Update config
    if "head_sample_rate" in config:
        sampler.sampling_config.head_sample_rate = float(config["head_sample_rate"])

    if "slow_trace_threshold_ms" in config:
        sampler.sampling_config.slow_trace_threshold_ms = float(config["slow_trace_threshold_ms"])

    if "debug_trace_ids" in config:
        sampler.sampling_config.debug_trace_ids = set(config["debug_trace_ids"])

    if "debug_session_ids" in config:
        sampler.sampling_config.debug_session_ids = set(config["debug_session_ids"])

    if "agent_sample_rates" in config:
        sampler.sampling_config.agent_sample_rates = config["agent_sample_rates"]

    return {
        "status": "updated",
        "new_config": {
            "head_sample_rate": sampler.sampling_config.head_sample_rate,
            "tail_sample_enabled": sampler.sampling_config.tail_sample_enabled,
            "slow_trace_threshold_ms": sampler.sampling_config.slow_trace_threshold_ms,
            "debug_trace_ids": list(sampler.sampling_config.debug_trace_ids),
            "agent_sample_rates": sampler.sampling_config.agent_sample_rates
        }
    }


@app.post("/api/v1/sampling/reset-metrics")
async def reset_sampling_metrics():
    """Reset sampling metrics counters"""
    if sampler:
        sampler.reset_metrics()
        return {"status": "reset", "message": "Sampling metrics have been reset"}
    return {"error": "Sampler not initialized"}


# ============================================================================
# OTLP EXPORT ENDPOINTS
# ============================================================================

@app.get("/api/v1/otlp/status")
async def get_otlp_status():
    """Get OTLP exporter status and metrics"""
    if not otlp_exporter:
        return {
            "status": "disabled",
            "message": "OTLP exporter not configured. Set OTEL_EXPORTER_OTLP_ENDPOINT to enable."
        }

    return {
        "status": "active",
        "endpoint": otlp_exporter.traces_endpoint,
        "metrics": await otlp_exporter.get_metrics(),
        "config": {
            "batch_size": otlp_exporter.config.batch_size,
            "batch_timeout_seconds": otlp_exporter.config.batch_timeout_seconds,
            "compression": otlp_exporter.config.compression,
            "service_name": otlp_exporter.config.service_name
        }
    }


@app.post("/api/v1/otlp/flush")
async def flush_otlp():
    """Force flush any buffered spans to OTLP endpoint"""
    if not otlp_exporter:
        return {"error": "OTLP exporter not configured"}

    await otlp_exporter.flush()
    return {"status": "flushed", "message": "Buffered spans have been exported"}


@app.post("/api/v1/otlp/export-trace/{trace_id}")
async def export_trace_to_otlp(trace_id: str):
    """Export a specific trace to OTLP endpoint"""
    if not otlp_exporter:
        return {"error": "OTLP exporter not configured"}

    trace = store.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    spans = store.get_spans_for_trace(trace_id)

    await otlp_exporter.export_trace(trace, spans)
    await otlp_exporter.flush()

    return {
        "status": "exported",
        "trace_id": trace_id,
        "span_count": len(spans),
        "endpoint": otlp_exporter.traces_endpoint
    }


# ============================================================================
# CLAUDE CODE INTEGRATION ENDPOINTS
# ============================================================================

@app.get("/api/v1/claude/diagnose")
async def claude_diagnose(
    time_window_minutes: int = Query(default=15, le=60),
    format: str = Query(default="json", enum=["json", "prompt", "summary"])
):
    """
    Generate a diagnostic bundle for Claude Code.

    This is the primary endpoint for Claude Code to query system state.

    Parameters:
    - time_window_minutes: Look back period (default 15, max 60)
    - format: Response format
        - json: Full structured data
        - prompt: AI-optimized text format
        - summary: Brief overview only

    Response includes:
    - System status (healthy/degraded/critical)
    - Recent errors with analysis
    - Performance metrics
    - Rate limit status
    - Slow traces
    - Suggested actions
    """
    if not diagnostic_analyzer:
        return {"error": "Diagnostic analyzer not initialized"}

    # Gather data
    traces = list(store.traces.values())
    spans = list(store.spans.values())
    events = store.events
    stored_errors = store.errors  # NEW: Include stored DetailedErrors

    # Get Oracle state if available
    oracle_state = None
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator
        api_url = "http://localhost:8080"
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")

        aggregator = OracleMonitorAggregator(
            namespace="telemetry",
            api_url=api_url,
            ollama_url=ollama_url
        )
        state = await aggregator.get_state()
        oracle_state = state.to_dict(strict=_oracle_strict_schema_default())
    except Exception as e:
        logger.warning(f"Could not get Oracle state: {e}")

    # Generate diagnostic bundle - now with stored errors from Kafka
    bundle = await diagnostic_analyzer.analyze(
        traces=traces,
        spans=spans,
        events=events,
        oracle_state=oracle_state,
        time_window_minutes=time_window_minutes,
        stored_errors=stored_errors
    )

    if format == "prompt":
        return {
            "format": "prompt",
            "content": bundle.to_claude_prompt()
        }
    elif format == "summary":
        return {
            "format": "summary",
            "system_status": bundle.system_status,
            "system_summary": bundle.system_summary,
            "issue_counts": {
                "critical": bundle.critical_count,
                "high": bundle.high_count,
                "medium": bundle.medium_count
            },
            "error_rate": bundle.error_rate,
            "any_rate_limited": bundle.any_rate_limited,
            "suggested_actions": bundle.suggested_actions[:3]
        }
    else:
        return bundle.to_dict()


@app.get("/api/v1/claude/errors")
async def claude_get_errors(
    limit: int = Query(default=10, le=50),
    include_suggestions: bool = True,
    include_raw_errors: bool = Query(
        default=False,
        description="Include raw DetailedError records from Kafka error store"
    )
):
    """
    Get recent errors with Claude-friendly analysis.

    Returns errors grouped by type with:
    - Error counts
    - Affected agents
    - Sample messages
    - Contextual fix suggestions
    - Stack traces (from stored DetailedErrors)
    
    When include_raw_errors=True, also returns the raw DetailedError
    records from the internal error store.
    """
    if not diagnostic_analyzer:
        return {"error": "Diagnostic analyzer not initialized"}

    # Use stored errors from Kafka if available, fall back to span/event analysis
    stored_errors = store.errors
    if stored_errors:
        errors = diagnostic_analyzer._analyze_stored_errors(stored_errors)
    else:
        traces = list(store.traces.values())
        spans = list(store.spans.values())
        events = store.events
        errors = diagnostic_analyzer._analyze_errors(traces, spans, events)

    result = []
    for error in errors[:limit]:
        error_dict = error.to_dict()
        if include_suggestions:
            error_dict["suggestions"] = diagnostic_analyzer._get_error_suggestions(
                error.error_category
            )
        result.append(error_dict)

    response = {
        "total_error_types": len(errors),
        "errors": result,
        "error_store_summary": store.get_error_summary(),
        "source": "kafka_error_store" if stored_errors else "span_event_inference"
    }
    
    if include_raw_errors:
        response["raw_errors"] = store.get_recent_errors(limit)
    
    return response


@app.get("/api/v1/claude/slow-traces")
async def claude_get_slow_traces(
    threshold_ms: float = Query(default=5000.0),
    limit: int = Query(default=10, le=50)
):
    """
    Get slow traces with bottleneck analysis.

    Returns traces exceeding the threshold with:
    - Duration breakdown
    - Slowest span identification
    - Bottleneck analysis
    """
    if not diagnostic_analyzer:
        return {"error": "Diagnostic analyzer not initialized"}

    # Temporarily override threshold
    original_threshold = diagnostic_analyzer.slow_trace_threshold_ms
    diagnostic_analyzer.slow_trace_threshold_ms = threshold_ms

    traces = list(store.traces.values())
    spans = list(store.spans.values())

    slow_traces = diagnostic_analyzer._find_slow_traces(traces, spans)

    # Restore threshold
    diagnostic_analyzer.slow_trace_threshold_ms = original_threshold

    return {
        "threshold_ms": threshold_ms,
        "count": len(slow_traces),
        "traces": [s.to_dict() for s in slow_traces[:limit]]
    }


@app.get("/api/v1/claude/rate-limits")
async def claude_get_rate_limits():
    """
    Get current rate limit status for all LLM models.

    Returns:
    - Per-model TPM/RPM usage
    - Warning/limited status
    - Estimated reset times
    """
    oracle_state = None
    try:
        from ..oracle.state_aggregator import OracleMonitorAggregator

        aggregator = OracleMonitorAggregator(
            api_url="http://localhost:8080",
            ollama_url=os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        )
        state = await aggregator.get_state()
        oracle_state = state.to_dict(strict=_oracle_strict_schema_default())
    except Exception as e:
        return {"error": f"Could not get Oracle state: {e}"}

    if not oracle_state or not diagnostic_analyzer:
        return {"error": "Required components not available"}

    rate_limits = diagnostic_analyzer._analyze_rate_limits(oracle_state)

    any_limited = any(r.is_limited for r in rate_limits)
    any_warning = any(r.tpm_percentage > 80 or r.rpm_percentage > 80 for r in rate_limits)

    return {
        "status": "limited" if any_limited else "warning" if any_warning else "ok",
        "any_limited": any_limited,
        "models": [r.to_dict() for r in rate_limits]
    }


@app.get("/api/v1/claude/actions")
async def claude_get_suggested_actions(
    limit: int = Query(default=5, le=20)
):
    """
    Get prioritized suggested actions for system improvement.

    Returns actions sorted by priority with:
    - Action description
    - Rationale
    - Related issue references
    """
    if not diagnostic_analyzer:
        return {"error": "Diagnostic analyzer not initialized"}

    # Generate full diagnostic to get actions
    traces = list(store.traces.values())
    spans = list(store.spans.values())
    events = store.events
    stored_errors = store.errors

    bundle = await diagnostic_analyzer.analyze(
        traces=traces,
        spans=spans,
        events=events,
        time_window_minutes=15,
        stored_errors=stored_errors
    )

    return {
        "system_status": bundle.system_status,
        "total_actions": len(bundle.suggested_actions),
        "actions": bundle.suggested_actions[:limit]
    }


@app.get("/api/v1/claude/context/{trace_id}")
async def claude_get_trace_context(trace_id: str):
    """
    Get detailed context for a specific trace for debugging.

    Returns comprehensive trace information including:
    - Full trace timeline
    - All spans with details
    - Error context if any
    - Performance analysis
    """
    trace = store.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")

    spans = store.get_spans_for_trace(trace_id)
    events = store.get_events_for_trace(trace_id)
    handoffs = store.get_handoffs_for_trace(trace_id)
    trace_errors = store.get_errors_for_trace(trace_id)  # NEW: Get errors from store

    # Analyze the trace
    duration = trace.get("total_duration_ms", 0)
    is_slow = duration > 5000
    has_errors = any(s.get("status") == "failed" for s in spans) or len(trace_errors) > 0

    # Find bottleneck if slow
    bottleneck = None
    if spans:
        slowest = max(spans, key=lambda x: x.get("duration_ms", 0))
        if slowest:
            bottleneck = {
                "span_id": slowest.get("span_id"),
                "agent_name": slowest.get("agent_name"),
                "duration_ms": slowest.get("duration_ms"),
                "span_kind": slowest.get("span_kind")
            }

    # Build timeline
    timeline = []
    for span in sorted(spans, key=lambda x: x.get("start_time", "")):
        timeline.append({
            "type": "span",
            "timestamp": span.get("start_time"),
            "agent": span.get("agent_name"),
            "kind": span.get("span_kind"),
            "duration_ms": span.get("duration_ms"),
            "status": span.get("status"),
            "error": span.get("error_message")
        })

    for event in sorted(events, key=lambda x: x.get("timestamp", "")):
        timeline.append({
            "type": "event",
            "timestamp": event.get("timestamp"),
            "event_type": event.get("event_type"),
            "message": event.get("message"),
            "severity": event.get("severity")
        })

    timeline.sort(key=lambda x: x.get("timestamp", ""))

    return {
        "trace_id": trace_id,
        "status": trace.get("status"),
        "duration_ms": duration,
        "is_slow": is_slow,
        "has_errors": has_errors,
        "summary": {
            "agent_count": trace.get("agent_count", 0),
            "llm_calls": trace.get("llm_call_count", 0),
            "tool_calls": trace.get("tool_call_count", 0),
            "total_tokens": trace.get("total_tokens", 0),
            "error_count": len(trace_errors)  # NEW: Error count in summary
        },
        "bottleneck": bottleneck,
        "timeline": timeline,
        "handoffs": handoffs,
        "errors": trace_errors,  # NEW: Full DetailedError records for this trace
        "user_input": trace.get("user_input"),
        "final_output": trace.get("final_output")
    }


# ============================================================================
# W3C TRACE CONTEXT PROPAGATION
# ============================================================================

@app.get("/api/v1/trace-context/create")
async def create_trace_context(sampled: bool = True):
    """
    Create a new W3C trace context for distributed tracing.

    Returns traceparent and tracestate headers to propagate.
    """
    context = TraceContextPropagator.create_context(sampled=sampled)

    return {
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        "sampled": context.is_sampled,
        "headers": context.to_headers()
    }


@app.post("/api/v1/trace-context/parse")
async def parse_trace_context(headers: Dict[str, str]):
    """
    Parse W3C trace context from HTTP headers.

    Input: {"traceparent": "00-...", "tracestate": "..."}
    """
    context = TraceContextPropagator.extract(headers)

    if not context:
        return {"error": "Invalid or missing traceparent header"}

    return {
        "trace_id": context.trace_id,
        "span_id": context.span_id,
        "sampled": context.is_sampled,
        "trace_state": context.trace_state
    }


def main():
    """Run the API server"""
    uvicorn.run(
        "src.api.service:app",
        host="0.0.0.0",
        port=8080,
        reload=True
    )


if __name__ == "__main__":
    main()
