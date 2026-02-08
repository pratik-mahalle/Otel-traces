# Multi-Agent Telemetry System

A telemetry system for debugging multi-agent orchestration. Kafka handles **inter-agent communication** through per-agent queues, and the telemetry layer observes all traffic so Claude Code can diagnose issues automatically.

---

## Architecture

Kafka is the inter-agent communication backbone. Each agent has its own Kafka queue. Agents communicate by writing to each other's queues:

```
Orchestrator writes task  -->  agent-queue-researcher  -->  Researcher reads
Researcher writes result  -->  agent-queue-orchestrator -->  Orchestrator reads
```

The telemetry service observes queue traffic (read-only) and captures **only errors** to a dedicated error store for Claude Code debugging.

```
                    ┌──────────────────────────────────────────────┐
                    │                 EKS CLUSTER                   │
                    │                                               │
                    │   Orchestrator    Researcher    Writer  Coder │
                    │       │               │          │       │   │
                    │       └───────────────┴──────────┴───────┘   │
                    │                       │                       │
                    │                       ▼                       │
                    │   ┌───────────────────────────────────────┐  │
                    │   │     KAFKA - Per-Agent Queues           │  │
                    │   │                                        │  │
                    │   │  agent-queue-orchestrator               │  │
                    │   │  agent-queue-researcher                 │  │
                    │   │  agent-queue-writer                     │  │
                    │   │  agent-queue-coder                      │  │
                    │   │  agent-queue-reviewer                   │  │
                    │   │                                        │  │
                    │   │  + agent-telemetry-errors (error logs) │  │
                    │   └──────────────────┬────────────────────┘  │
                    │                      │                        │
                    │                      ▼                        │
                    │   ┌───────────────────────────────────────┐  │
                    │   │     Telemetry API (Observer)           │  │
                    │   │                                        │  │
                    │   │  Queue Messages  │  Error Store        │  │
                    │   └──────────────────┬────────────────────┘  │
                    │                      │                        │
                    └──────────────────────┼────────────────────────┘
                                           │
                                           ▼
                                      Claude Code
                             (queries REST API to debug)
```

---

## How It Works

### 1. Inter-Agent Communication (Kafka Queues)

Each agent has its own Kafka topic. Agents talk to each other by writing to the target agent's queue:

| Kafka Topic | Owner | Writers | Reader |
|-------------|-------|---------|--------|
| `agent-queue-orchestrator` | Orchestrator | Sub-agents send results | Orchestrator |
| `agent-queue-researcher` | Researcher | Orchestrator sends tasks | Researcher |
| `agent-queue-writer` | Writer | Orchestrator sends tasks | Writer |
| `agent-queue-coder` | Coder | Orchestrator sends tasks | Coder |
| `agent-queue-reviewer` | Reviewer | Orchestrator sends tasks | Reviewer |

**Example handoff:**

```
1. Orchestrator writes TASK    -> agent-queue-researcher
2. Researcher reads from its own queue
3. Researcher processes the task
4. Researcher writes RESULT    -> agent-queue-orchestrator
5. Orchestrator reads the result from its own queue
```

### 2. Error Capture (Only Errors Sent to Kafka)

Only **errors** are sent to Kafka as telemetry. When an agent fails (LLM error, tool error, timeout, etc.), a `DetailedError` is published to `agent-telemetry-errors`. The Telemetry API consumes this topic and stores errors in a dedicated Error Store.

The Telemetry API also observes agent queue traffic (read-only) to track which tasks are pending.

### 3. Claude Code Debugging

Claude Code queries the REST API to find and fix issues:

```bash
# Check agent queue states
curl http://localhost:8080/api/v1/queues

# Check errors
curl http://localhost:8080/api/v1/errors?severity=critical

# Full diagnostics
curl http://localhost:8080/api/v1/claude/diagnose

# Verify fix
curl http://localhost:8080/api/v1/oracle/validate
```

---

## Quick Start

```bash
./scripts/setup.sh
kubectl apply -f k8s/agents-sample.yaml
kubectl -n telemetry port-forward service/telemetry-api 8080:8080
curl http://localhost:8080/health
```

**If `ollama` or `telemetry-dashboard` stay in ContainerCreating:** they pull `ollama/ollama:latest` (~2–4GB) and `nginx:alpine` from the network. The first time can take 5–15+ minutes. Re-run `./scripts/setup.sh` to pre-pull and load these images into Kind so future runs start quickly.

---

## API Endpoints

### Agent Queues

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/queues` | All agent queues with pending task counts |
| `GET /api/v1/queues/{agent}` | Messages in an agent's queue |
| `GET /api/v1/queues/{agent}/pending` | Unresolved tasks for an agent |

### Errors

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/errors` | List/filter errors (severity, category, agent, trace) |
| `GET /api/v1/errors/summary` | Error count breakdown |
| `GET /api/v1/errors/{error_id}` | Full error detail with stack trace |

### Diagnostics

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/claude/diagnose` | Full diagnostic bundle |
| `GET /api/v1/claude/errors` | Recent errors with fix suggestions |
| `GET /api/v1/oracle/state` | Full system state (strict JSON schema) |
| `GET /api/v1/oracle/validate` | Validate state against schema |

### Real-Time

| Endpoint | Description |
|----------|-------------|
| `WS /ws/debug/{client_id}` | Real-time error alerts + queue activity |

---

## Project Structure

```
src/
  agents/
    multi_agent_system.py    # Agent classes with Kafka queue-based handoffs
    emit_telemetry.py        # Sample telemetry agent
  telemetry/
    schemas.py               # AgentMessage, DetailedError, spans, traces
    collector.py             # Kafka producer (send_to_agent, record_error)
    claude_integration.py    # Claude Code diagnostic analyzer
    litellm_integration.py   # LiteLLM Gateway integration
    otlp_exporter.py         # OpenTelemetry export
    sampling.py              # Sampling & rate limiting
  api/
    service.py               # FastAPI service (queue observer, error store)
  oracle/
    state_schema.py          # Strict JSON schema for system state
    state_aggregator.py      # Aggregates state from queues, K8s, LiteLLM
scripts/
  setup.sh                   # Cluster + Kafka topics setup
k8s/
  deployment.yaml            # Kafka, Ollama, API deployments
  agents-sample.yaml         # Sample agent pods
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Kafka connection |
| `ORACLE_DISCOVERY_MODE` | `auto` | Agent discovery: `auto`, `labeled`, `all`, `telemetry` |
| `ORACLE_STRICT_SCHEMA` | `true` | Strict JSON schema output |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama API URL |

---

Made with care by Pratik Mahalle
