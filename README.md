# Multi-Agent Telemetry System

A production-ready telemetry system for multi-agent orchestration. Oracle Monitor exposes a **strict JSON schema** that Claude Code can query to diagnose and fix agent issues automatically.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              KUBERNETES CLUSTER                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  Researcher  │  │    Writer    │  │    Coder     │  │ Orchestrator │    │
│  │    Agent     │  │    Agent     │  │    Agent     │  │    Agent     │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                 │                 │             │
│         └─────────────────┴─────────────────┴─────────────────┘             │
│                                    │                                         │
│                                    ▼                                         │
│                          ┌─────────────────┐                                │
│                          │      KAFKA      │                                │
│                          │  (Telemetry)    │                                │
│                          └────────┬────────┘                                │
│                                   │                                          │
│                                   ▼                                          │
│  ┌────────────────────────────────────────────────────────────────────┐    │
│  │                       TELEMETRY API SERVICE                         │    │
│  ├────────────────────────────────────────────────────────────────────┤    │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                 │    │
│  │  │   Oracle    │  │  Diagnostic │  │    OTLP     │                 │    │
│  │  │  Monitor    │  │  Analyzer   │  │  Exporter   │                 │    │
│  │  └─────────────┘  └─────────────┘  └─────────────┘                 │    │
│  └────────────────────────────────────────────────────────────────────┘    │
│                                   │                                          │
└───────────────────────────────────┼──────────────────────────────────────────┘
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │         CLAUDE CODE           │
                    │  ┌─────────────────────────┐  │
                    │  │ 1. Query diagnostics    │  │
                    │  │ 2. Analyze errors       │  │
                    │  │ 3. Implement fixes      │  │
                    │  │ 4. Verify resolution    │  │
                    │  └─────────────────────────┘  │
                    └───────────────────────────────┘
```

## How It Works

### 1. Agent Discovery

Oracle Monitor supports **multiple discovery modes** to find agents:

#### Discovery Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| `auto` | Labeled K8s deployments + telemetry-derived agents | **Default** - Best of both worlds |
| `labeled` | Only K8s deployments with `oracle-monitor/agent=true` | Explicit control over which deployments are agents |
| `all` | ALL deployments in namespace | Quick setup - no labels needed |
| `telemetry` | Auto-discover from telemetry data only | No K8s access required |

Set the mode via environment variable:
```bash
export ORACLE_DISCOVERY_MODE=auto  # default
export ORACLE_DISCOVERY_MODE=all   # discover all deployments
export ORACLE_DISCOVERY_MODE=telemetry  # from traces only
```

#### Option A: Labeled Discovery (Recommended for Production)

Add labels to your agent deployments:

```yaml
metadata:
  labels:
    oracle-monitor/agent: "true"           # Required - marks as discoverable
    oracle-monitor/name: "Researcher"      # Optional - agent name
  annotations:
    oracle-monitor/models: "gpt-4o,claude-3-5-sonnet"  # LLM models used
    oracle-monitor/description: "Research agent"       # Description
```

#### Option B: Discover All Deployments

No labels needed - Oracle Monitor finds ALL deployments:

```bash
export ORACLE_DISCOVERY_MODE=all
```

Oracle will auto-infer agent details from:
- Deployment name (e.g., `agent-researcher` → "Research agent")
- Container environment variables (MODEL_NAME, etc.)
- Container images (detects ollama, litellm, etc.)

To **exclude** a deployment from discovery:
```yaml
metadata:
  annotations:
    oracle-monitor/exclude: "true"  # Skip this deployment
```

#### Option C: Telemetry-Based Discovery

Agents are auto-discovered when they emit telemetry - no K8s labels needed:

```bash
export ORACLE_DISCOVERY_MODE=telemetry
```

When an agent emits spans/traces to Kafka, Oracle Monitor automatically registers it:
```
Agent emits span with agent_name="Researcher"
        ↓
Oracle Monitor sees the telemetry
        ↓
Agent appears in /api/v1/oracle/agents
```

### 2. Telemetry Collection

Agents emit telemetry (spans, traces, events) to Kafka:

```
Agent executes task
       │
       ▼
Emits spans/events ──▶ Kafka Topics ──▶ Telemetry API ──▶ In-memory Store
       │                                      │
       └── Errors with context ───────────────┘
```

### 3. Oracle Monitor Aggregation

The Oracle Monitor aggregates state from multiple sources:

| Source | Data Collected |
|--------|----------------|
| Kubernetes API | Deployments, pods, resource usage |
| Kafka Topics | Active tasks, events, errors |
| Ollama/LiteLLM | Model availability, rate limits |
| Telemetry Store | Traces, spans, performance metrics |

### 4. Claude Code Integration

Claude Code queries the strict-schema API to diagnose and fix issues:

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│ Agent Error │ ───▶ │  Telemetry  │ ───▶ │ Claude Code │ ───▶ │  Code Fix   │
│   Occurs    │      │     API     │      │  Diagnoses  │      │  Applied    │
└─────────────┘      └─────────────┘      └─────────────┘      └─────────────┘
```

---

## Quick Start

```bash
# Setup the cluster
./scripts/setup.sh

# Deploy sample agents
kubectl apply -f k8s/agents-sample.yaml

# Port forward the API
kubectl -n telemetry port-forward service/telemetry-api 8080:8080

# Verify health
curl http://localhost:8080/health
```

---

## API Reference

### Oracle Monitor Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/oracle/state` | GET | Full system state (strict JSON schema) |
| `/api/v1/oracle/validate` | GET | Validate current state against schema |
| `/api/v1/oracle/schema` | GET | Get the JSON schema definition |
| `/api/v1/oracle/agents` | GET | List all discovered agents |
| `/api/v1/oracle/workload` | GET | Kubernetes workload metrics |
| `/api/v1/oracle/llm` | GET | LLM model status and usage |
| `/api/v1/oracle/issues` | GET | Current system issues |

### Claude Code Diagnostic Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/claude/diagnose` | GET | Full diagnostic bundle |
| `/api/v1/claude/errors` | GET | Recent errors with suggestions |
| `/api/v1/claude/slow-traces` | GET | Slow traces with bottleneck analysis |
| `/api/v1/claude/rate-limits` | GET | LLM rate limit status |
| `/api/v1/claude/actions` | GET | Prioritized suggested actions |
| `/api/v1/claude/context/{trace_id}` | GET | Detailed trace context |

### Trace Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/traces` | GET | List/search traces |
| `/api/v1/traces/{trace_id}` | GET | Get trace with spans |
| `/api/v1/traces/{trace_id}/timeline` | GET | Timeline view |
| `/api/v1/traces/{trace_id}/graph` | GET | Agent interaction graph |

### Real-Time Debugging

| Endpoint | Type | Description |
|----------|------|-------------|
| `/ws/debug/{client_id}` | WebSocket | Real-time debug events |
| `/api/v1/events/stream` | SSE | Server-sent events stream |

---

## Claude Code Error Detection & Fix Workflow

### Step 1: Detect Issues

```bash
# Quick health check
curl http://localhost:8080/api/v1/oracle/issues
```

Response:
```json
{
  "status": "degraded",
  "issues": [
    "Failed task task-123 on agent Researcher",
    "Model gpt-4 at 95% TPM limit"
  ],
  "agent_count": 4,
  "active_tasks": 2
}
```

### Step 2: Get Full Diagnostics

```bash
# AI-optimized diagnostic report
curl "http://localhost:8080/api/v1/claude/diagnose?format=prompt"
```

Response:
```
=== MULTI-AGENT SYSTEM DIAGNOSTIC REPORT ===
Generated: 2024-01-15T10:30:00Z
System Status: DEGRADED

[ISSUES REQUIRING ATTENTION]
[HIGH] Rate Limit Hit: gpt-4
Category: rate_limit
Description: Model gpt-4 is at 95% of rate limit
Suggested actions:
  1. Wait ~60s for rate limit reset
  2. Consider using a different model
  3. Implement request queuing or backoff

[CRITICAL] Repeated Error: RateLimitError (12x)
Affected: Researcher, Writer
Suggested actions:
  1. Implement exponential backoff
  2. Add request queuing

[SUGGESTED ACTIONS]
1. [HIGH] Implement exponential backoff
   Rationale: Addresses rate limit errors
```

### Step 3: Get Error Details

```bash
curl http://localhost:8080/api/v1/claude/errors
```

Response:
```json
{
  "total_error_types": 2,
  "errors": [
    {
      "error_type": "RateLimitError",
      "error_category": "rate_limit",
      "count": 12,
      "affected_agents": ["Researcher", "Writer"],
      "sample_message": "Rate limit exceeded for openai/gpt-4",
      "sample_trace_id": "trace-abc-123",
      "suggestions": [
        "Implement exponential backoff",
        "Add request queuing",
        "Consider using multiple API keys"
      ]
    }
  ]
}
```

### Step 4: Claude Code Implements Fix

Based on the diagnostics, Claude Code can automatically fix the issue:

```python
# Before (problematic code)
response = await litellm.acompletion(model="gpt-4", messages=messages)

# After (Claude Code adds retry with backoff)
from tenacity import retry, wait_exponential, stop_after_attempt

@retry(
    wait=wait_exponential(multiplier=1, min=4, max=60),
    stop=stop_after_attempt(5)
)
async def call_llm_with_retry(model, messages):
    return await litellm.acompletion(model=model, messages=messages)

response = await call_llm_with_retry("gpt-4", messages)
```

### Step 5: Verify Fix

```bash
# Check if issues are resolved
curl http://localhost:8080/api/v1/oracle/validate

# Expected: {"valid": true, "error_count": 0, "errors": []}
```

---

## Oracle State Schema

The Oracle Monitor outputs a strict JSON schema for consistent parsing:

```json
{
  "id": "uuid",
  "agents": [
    {
      "name": "Researcher",
      "deployment_name": "agent-researcher",
      "models": ["gpt-4", "claude-3-5-sonnet"],
      "max_parallel_invocations": 3,
      "activity": {
        "active_task_ids": [
          {"id": "task-1", "started_on": "ISO8601", "status": "running"}
        ],
        "updated_at": "ISO8601"
      }
    }
  ],
  "workload": [
    {
      "deployment_name": "agent-researcher",
      "max_pods": 3,
      "live": {"active_pods": 2, "updated_at": "ISO8601"},
      "pods": [
        {"pod_id": "pod-abc", "cpu": 150, "memory": 256, "status": "Running"}
      ]
    }
  ],
  "queues": [
    {
      "name": "agent-task-queue",
      "tasks": [
        {
          "id": "task-1",
          "priority": {"level": "high", "waiting_since_mins": 2.5},
          "invoked_by": "Orchestrator"
        }
      ]
    }
  ],
  "litellm": [
    {
      "model": "gpt-4",
      "provider": "openai",
      "tpm": 45000,
      "tpm_max": 50000,
      "rpm": 95,
      "rpm_max": 100
    }
  ]
}
```

Validate the schema:
```bash
curl http://localhost:8080/api/v1/oracle/validate
# {"valid": true, "error_count": 0, "errors": []}
```

---

## Real-Time Debugging (WebSocket)

Connect to receive live events:

```javascript
const ws = new WebSocket('ws://localhost:8080/ws/debug/my-session');

// Subscribe to specific agents/events
ws.send(JSON.stringify({
  type: 'subscribe',
  agent_names: ['Researcher', 'Writer'],
  event_types: ['error', 'warning', 'handoff']
}));

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log(`[${data.severity}] ${data.agent_name}: ${data.message}`);
};
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ORACLE_DISCOVERY_MODE` | `auto` | Agent discovery mode: `auto`, `labeled`, `all`, `telemetry` |
| `ORACLE_STRICT_SCHEMA` | `true` | Output strict JSON schema |
| `ORACLE_ALLOW_MOCKS` | `false` (prod) | Allow mock data when K8s unavailable |
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Kafka connection |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama API URL |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | - | OTLP export endpoint (optional) |

---

## Project Structure

```
├── src/
│   ├── agents/                 # Multi-agent system
│   │   ├── multi_agent_system.py   # Base agent classes
│   │   └── emit_telemetry.py       # Sample telemetry agent
│   ├── telemetry/              # Telemetry infrastructure
│   │   ├── schemas.py              # Trace/span/event schemas
│   │   ├── collector.py            # Kafka telemetry collector
│   │   ├── claude_integration.py   # Claude Code diagnostics
│   │   ├── otlp_exporter.py        # OpenTelemetry export
│   │   └── sampling.py             # Sampling & rate limiting
│   ├── api/
│   │   └── service.py              # FastAPI REST service
│   └── oracle/                 # Oracle Monitor
│       ├── state_schema.py         # Strict JSON schema
│       ├── state_aggregator.py     # K8s/Kafka state collector
│       └── cli.py                  # CLI interface
├── k8s/
│   ├── deployment.yaml             # Core services
│   └── agents-sample.yaml          # Sample agent deployments
└── scripts/
    └── setup.sh                    # Cluster setup script
```

---

## Usage Examples

### For Claude Code

```bash
# Get diagnostic bundle (recommended for Claude Code)
curl http://localhost:8080/api/v1/claude/diagnose?format=prompt

# Get strict schema state
curl http://localhost:8080/api/v1/oracle/state

# Check specific trace
curl http://localhost:8080/api/v1/claude/context/{trace_id}
```

### Debugging Prompt for Claude Code

```
You are debugging a multi-agent system. Query these endpoints:

1. GET /api/v1/oracle/issues - Quick health check
2. GET /api/v1/claude/diagnose?format=prompt - Full diagnostics
3. GET /api/v1/claude/errors - Error details with fix suggestions

Analyze the response, identify root causes, and implement fixes.
After fixing, verify with GET /api/v1/oracle/validate.
```

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests: `pytest`
5. Submit a pull request

---

Made with ❤️ by Pratik Mahalle
