# Multi-Agent Telemetry System for Claude Code Debugging

A production-ready telemetry system for debugging multi-agent AI systems, built with OpenTelemetry semantic conventions, Kafka event streaming, and LiteLLM integration. Includes the **Oracle Monitor** for unified system state observation.

## 🎯 Overview

This system enables real-time debugging of multi-agent AI workflows by capturing and visualizing:

- **Agent Spans**: Individual agent execution traces with timing, I/O, and metrics
- **LLM Calls**: Token usage, costs, latency for all model interactions
- **Tool Invocations**: Function calls and tool usage within agents
- **Agent Handoffs**: Cross-agent delegation and task transitions
- **Debug Events**: Real-time events for live debugging workflows
- **Oracle Monitor**: Unified system state (agents, workloads, queues, LLM usage)

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Kind Cluster                                │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐  │
│  │  Multi-Agent    │  │                 │  │   Telemetry API     │  │
│  │    System       │──│     Kafka       │──│   (FastAPI)         │  │
│  │  (LiteLLM)      │  │  (Event Bus)    │  │   - REST API        │  │
│  └─────────────────┘  └─────────────────┘  │   - WebSocket       │  │
│                                             │   - Oracle Monitor  │  │
│  ┌─────────────────┐                        └─────────────────────┘  │
│  │    Ollama       │                        ┌─────────────────────┐  │
│  │  (Local LLM)    │                        │     Dashboard       │  │
│  └─────────────────┘                        │     (React)         │  │
│                                             └─────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### Prerequisites

- Docker Desktop (with Kubernetes enabled) or Docker Engine
- 8GB+ RAM available
- ~10GB disk space

### One-Command Setup

```bash
# Clone and run setup
cd multi-agent-telemetry
chmod +x scripts/setup.sh
./scripts/setup.sh
```

This script will:
1. Install Kind and kubectl if missing
2. Create a Kind Kubernetes cluster
3. Build and load Docker images
4. Deploy Kafka, Ollama, API, and Dashboard
5. Create Kafka topics
6. Pull the Ollama llama2 model

### Access Points

| Service | URL | Description |
|---------|-----|-------------|
| Dashboard | http://localhost:8081 | Real-time debugging UI |
| API | http://localhost:8080 | REST API endpoints |
| API Docs | http://localhost:8080/docs | Swagger documentation |
| Oracle State | http://localhost:8080/api/v1/oracle/state | Unified system state |

## 🔮 Oracle Monitor

The Oracle Monitor provides a unified view of your multi-agent system state - perfect for Claude Code debugging.

### Quick State Check

```bash
# Get system state as human-readable log
curl -s http://localhost:8080/api/v1/oracle/state?format=log | jq -r '.log'

# Check for issues
curl -s http://localhost:8080/api/v1/oracle/issues | jq

# Get full JSON state
curl -s http://localhost:8080/api/v1/oracle/state | jq
```

### CLI Tool

```bash
# Install the package
pip install -e .

# Or run directly
python oracle_monitor.py state
python oracle_monitor.py agents
python oracle_monitor.py workload
python oracle_monitor.py llm
python oracle_monitor.py watch       # Real-time monitoring
python oracle_monitor.py debug       # Interactive session
```

### State Components

The Oracle Monitor aggregates:

| Component | Description |
|-----------|-------------|
| **agents** | All registered agents, their models, and current task activity |
| **workload** | Kubernetes deployment metrics (pods, CPU, memory) |
| **queues** | Task queues with priority and wait times |
| **litellm** | LLM model usage (TPM, RPM, rate limits) |

## 🔌 SDK Integration

### Basic Usage

```python
from src.telemetry.collector import TelemetryCollector
from src.telemetry.litellm_integration import TracedLiteLLM

# Initialize
collector = TelemetryCollector(kafka_bootstrap_servers="localhost:9092")
await collector.start()

llm = TracedLiteLLM(collector, default_model="ollama/llama2")

# Create a trace
trace = collector.create_trace(
    user_input="Research AI trends",
    session_id="session-123"
)

# Create spans for agent operations
async with collector.span(
    trace_id=trace.trace_id,
    agent_id="researcher",
    agent_name="ResearchAgent",
    span_kind=SpanKind.AGENT
) as span:
    # Make LLM calls with automatic telemetry
    response = await llm.completion(
        trace_id=trace.trace_id,
        agent_id="researcher",
        agent_name="ResearchAgent",
        messages=[{"role": "user", "content": "Research AI trends"}]
    )
    span.output_data = {"result": response}

# Complete the trace
await collector.complete_trace(trace.trace_id, output=response)
```

### Recording Handoffs

```python
# When one agent delegates to another
await collector.record_handoff(
    trace_id=trace_id,
    source_agent_id="orchestrator",
    source_agent_name="Orchestrator",
    target_agent_id="researcher",
    target_agent_name="ResearchAgent",
    reason="Delegate research task",
    context={"task": "Research AI trends", "priority": "high"}
)
```

### Emitting Debug Events

```python
# For real-time debugging
await collector.emit_event(
    trace_id=trace_id,
    agent_id="researcher",
    agent_name="ResearchAgent",
    event_type=EventType.TOOL_CALL,
    event_name="search_web",
    data={"query": "latest AI trends 2024"}
)
```

## 📁 Project Structure

```
multi-agent-telemetry/
├── src/
│   ├── telemetry/
│   │   ├── schemas.py          # OpenTelemetry-compliant data models
│   │   ├── collector.py        # Kafka-based telemetry collection
│   │   └── litellm_integration.py  # LLM tracing wrapper
│   ├── agents/
│   │   └── multi_agent_system.py   # Example multi-agent framework
│   ├── api/
│   │   └── service.py          # FastAPI REST & WebSocket service
│   └── dashboard/
│       └── index.html          # React debugging dashboard
├── k8s/
│   ├── kind-config.yaml        # Kind cluster configuration
│   └── deployment.yaml         # Kubernetes deployments
├── docker/
│   ├── Dockerfile.api          # API service container
│   └── Dockerfile.demo         # Demo application container
├── scripts/
│   └── setup.sh                # Automated setup script
├── demo.py                     # Demo application
└── requirements.txt            # Python dependencies
```

## 🔍 API Reference

### REST Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/traces` | List all traces with filtering |
| GET | `/api/v1/traces/{trace_id}` | Get full trace details |
| GET | `/api/v1/traces/{trace_id}/timeline` | Get chronological timeline |
| GET | `/api/v1/traces/{trace_id}/graph` | Get agent interaction graph |
| GET | `/api/v1/metrics` | Get aggregated metrics |
| POST | `/api/v1/debug/simulate-trace` | Generate test data |

### WebSocket

Connect to `/ws/debug/{client_id}` for real-time event streaming.

```javascript
const ws = new WebSocket('ws://localhost:8080/ws/debug/my-client');
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    console.log('Event:', data);
};
```

## 🧪 Running the Demo

### Simple Demo (No LLM Required)

```bash
# In the cluster
kubectl -n telemetry delete job multi-agent-demo 2>/dev/null || true
kubectl -n telemetry create job demo-simple --image=telemetry-demo:latest \
  -- python demo.py --simple
```

### Full Demo (With Ollama)

```bash
# Ensure Ollama model is pulled
kubectl -n telemetry exec -it deployment/ollama -- ollama pull llama2

# Run demo
kubectl -n telemetry apply -f k8s/deployment.yaml -l app=multi-agent-demo
```

## 📈 Telemetry Schema

Based on OpenTelemetry semantic conventions for multi-agent systems:

### AgentSpan
```json
{
    "trace_id": "uuid",
    "span_id": "uuid",
    "parent_span_id": "uuid|null",
    "agent_id": "string",
    "agent_name": "string",
    "span_kind": "AGENT|LLM|TOOL|RETRIEVER|CHAIN",
    "status": "RUNNING|COMPLETED|FAILED",
    "start_time": "ISO8601",
    "end_time": "ISO8601|null",
    "input_data": {},
    "output_data": {},
    "llm_calls": [...],
    "tool_calls": [...],
    "token_usage": {...},
    "error": {...}
}
```

### AgentHandoff
```json
{
    "handoff_id": "uuid",
    "trace_id": "uuid",
    "source_agent_id": "string",
    "target_agent_id": "string",
    "timestamp": "ISO8601",
    "reason": "string",
    "context": {}
}
```

## 🔧 Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker address |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama API URL |
| `LITELLM_LOG` | `INFO` | LiteLLM logging level |

### Kafka Topics

| Topic | Purpose |
|-------|---------|
| `agent-telemetry-spans` | Span data |
| `agent-telemetry-traces` | Complete traces |
| `agent-telemetry-events` | Debug events |
| `agent-telemetry-handoffs` | Agent handoffs |
| `agent-telemetry-metrics` | Aggregated metrics |
| `agent-telemetry-errors` | Error data |

## 🛠️ Development

### Local Development (Without Kubernetes)

```bash
# Install dependencies
pip install -r requirements.txt

# Start Kafka (using Docker)
docker run -d --name kafka \
  -p 9092:9092 \
  -e KAFKA_CFG_NODE_ID=0 \
  -e KAFKA_CFG_PROCESS_ROLES=controller,broker \
  -e KAFKA_CFG_LISTENERS=PLAINTEXT://:9092,CONTROLLER://:9093 \
  -e KAFKA_CFG_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9092 \
  -e KAFKA_CFG_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
  -e KAFKA_CFG_CONTROLLER_QUORUM_VOTERS=0@localhost:9093 \
  -e KAFKA_CFG_CONTROLLER_LISTENER_NAMES=CONTROLLER \
  -e ALLOW_PLAINTEXT_LISTENER=yes \
  bitnami/kafka:3.6

# Run API
cd src && uvicorn api.service:app --host 0.0.0.0 --port 8080 --reload

# Run demo
python demo.py --simple
```

### Testing

```bash
# Run tests
pytest tests/ -v

# Test API
curl http://localhost:8080/api/v1/traces
curl http://localhost:8080/api/v1/metrics
```

## 🎮 Claude Code Integration

This telemetry system is designed to integrate with Claude Code for debugging multi-agent workflows:

1. **Instrument your agents** with the telemetry SDK
2. **Run your multi-agent system** with telemetry enabled
3. **Open the dashboard** at http://localhost:8081
4. **Use Claude Code** to analyze traces, identify issues, and debug

Example workflow:
```
Claude Code: "Analyze the last failed trace and identify the root cause"
→ Fetches trace data from API
→ Examines timeline and events
→ Identifies failing agent/tool
→ Suggests fix
```

## 🔒 Production Considerations

For production deployment:

1. **Storage**: Replace in-memory storage with PostgreSQL/MongoDB
2. **Authentication**: Add JWT/OAuth to API endpoints
3. **Kafka**: Configure retention policies and scale brokers
4. **Monitoring**: Add Prometheus metrics and Grafana dashboards
5. **LLM**: Use production endpoints instead of Ollama
6. **Sampling**: Implement trace sampling for high-volume systems

## 📄 License

MIT License

## 🤝 Contributing

Contributions welcome! Please read the contributing guidelines first.

---

Built with ❤️ for debugging multi-agent AI systems
