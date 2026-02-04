"""
FastAPI Service for Multi-Agent Telemetry
Provides REST API and WebSocket for real-time debugging
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

from aiokafka import AIOKafkaConsumer

logger = logging.getLogger(__name__)


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


# In-memory storage for demo (use proper DB in production)
class TelemetryStore:
    def __init__(self):
        self.traces: Dict[str, Dict] = {}
        self.spans: Dict[str, Dict] = {}
        self.events: List[Dict] = []
        self.handoffs: List[Dict] = []
    
    def add_trace(self, trace: Dict):
        self.traces[trace["trace_id"]] = trace
    
    def add_span(self, span: Dict):
        self.spans[span["span_id"]] = span
    
    def add_event(self, event: Dict):
        self.events.append(event)
        # Keep only last 10000 events
        if len(self.events) > 10000:
            self.events = self.events[-10000:]
    
    def add_handoff(self, handoff: Dict):
        self.handoffs.append(handoff)
    
    def get_trace(self, trace_id: str) -> Optional[Dict]:
        return self.traces.get(trace_id)
    
    def get_spans_for_trace(self, trace_id: str) -> List[Dict]:
        return [s for s in self.spans.values() if s.get("trace_id") == trace_id]
    
    def get_events_for_trace(self, trace_id: str) -> List[Dict]:
        return [e for e in self.events if e.get("trace_id") == trace_id]
    
    def get_handoffs_for_trace(self, trace_id: str) -> List[Dict]:
        return [h for h in self.handoffs if h.get("trace_id") == trace_id]
    
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
    # Start Kafka consumer for receiving telemetry
    kafka_servers = "kafka:9092"
    topics = [
        "agent-telemetry-traces",
        "agent-telemetry-spans",
        "agent-telemetry-events",
        "agent-telemetry-handoffs"
    ]
    
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
                
                # Store the message
                if "traces" in topic:
                    store.add_trace(value)
                elif "spans" in topic:
                    store.add_span(value)
                elif "events" in topic:
                    store.add_event(value)
                    # Broadcast to connected clients
                    await manager.broadcast(value)
                elif "handoffs" in topic:
                    store.add_handoff(value)
                    await manager.broadcast({
                        "type": "handoff",
                        **value
                    })
        
        consumer_task = asyncio.create_task(consume_messages())
        logger.info("Kafka consumer started")
        
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
    
    # Create simulated spans
    agents = ["Orchestrator", "Researcher", "Writer"]
    for i, agent in enumerate(agents):
        span = {
            "span_id": str(uuid4()),
            "trace_id": trace_id,
            "agent_id": f"agent-{i}",
            "agent_name": agent,
            "span_kind": "agent",
            "status": "completed",
            "start_time": datetime.utcnow().isoformat(),
            "duration_ms": 500 + (i * 100)
        }
        store.add_span(span)
    
    return {"trace_id": trace_id, "message": "Simulated trace created"}


# Oracle Monitor endpoints for unified system state
# These endpoints are designed for Claude Code integration

@app.get("/api/v1/oracle/state")
async def get_oracle_state(format: str = Query(default="json", enum=["json", "log", "summary"])):
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
        
        if format == "log":
            return {"log": state.to_diff_log()}
        elif format == "summary":
            return state.get_summary()
        else:
            return state.to_dict()
            
    except Exception as e:
        logger.error(f"Error getting Oracle state: {e}")
        return {
            "error": str(e),
            "status": "error",
            "message": "Failed to aggregate system state"
        }


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
