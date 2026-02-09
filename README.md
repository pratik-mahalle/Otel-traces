# Multi-Agent Telemetry System

A telemetry system for debugging multi-agent orchestration. Kafka handles **inter-agent communication** through per-agent queues, and the telemetry layer observes all traffic so Claude Code can diagnose issues automatically.

---

## Architecture

Kafka is the inter-agent communication backbone. Each agent has its own Kafka queue. Agents communicate by writing to each other's queues:

```
Orchestrator writes task  -->  agent-queue-researcher  -->  Researcher reads
Researcher writes result  -->  agent-queue-orchestrator -->  Orchestrator reads
```

Agents and the Telemetry API both write to shared JSONL files. Claude Code reads them via an MCP server.

```
                    ┌──────────────────────────────────────────────┐
                    │                 K8s CLUSTER                   │
                    │                                               │
                    │   Orchestrator    Researcher    Writer  Coder │
                    │       │               │          │       │   │
                    │       │  inter-agent  │          │       │   │
                    │       └───────────────┴──────────┴───────┘   │
                    │               │                  │            │
                    │          (Kafka queues)    (direct write)     │
                    │               │                  │            │
                    │               ▼                  ▼            │
                    │   ┌─────────────────┐  ┌────────────────┐   │
                    │   │  KAFKA           │  │ Shared Volume  │   │
                    │   │  agent-queue-*   │  │                │   │
                    │   └────────┬────────┘  │ state_*.jsonl  │   │
                    │            │            │ diff_*.jsonl   │   │
                    │            ▼            └───────┬────────┘   │
                    │   ┌─────────────────┐          │            │
                    │   │ Telemetry API   │──────────┘            │
                    │   │ (queue observer)│  writes to JSONL too  │
                    │   └─────────────────┘                       │
                    └─────────────────────────────────────────────┘
                                           │
                              MCP Server reads JSONL files
                                           │
                                           ▼
                                      Claude Code
                              (MCP tools: diagnose, errors,
                               replay, queue status, etc.)
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

### 2. Telemetry (Internal, Not in Kafka)

No telemetry data goes to Kafka. When an agent fails, errors are stored internally by the Telemetry API and persisted to disk in two JSONL files:

- **`state_time_machine.jsonl`** — Full state snapshots (written periodically)
- **`diff_time_machine.jsonl`** — Incremental changes (every error, queue message, etc.)

The API also observes agent queue traffic (read-only) to track which tasks are pending. Claude Code queries the REST API to see errors and queue state.

### 3. Claude Code Debugging (MCP Server)

Claude Code connects to the MCP server which reads the JSONL files directly. The MCP server exposes 6 tools:

| MCP Tool | What it does |
|----------|-------------|
| `get_latest_state` | Latest state snapshot (error counts, active traces, etc.) |
| `query_diffs` | Search diffs by entity type, time range, change type |
| `get_errors` | Search errors by severity, category, agent name |
| `replay_timeline` | Chronological replay of all events in a time window |
| `get_queue_status` | Per-agent queue stats (sent/received/pending) |
| `diagnose` | Full diagnostic: errors + queue bottlenecks + suggested fixes |

The REST API is also available for direct queries:

```bash
curl http://localhost:8080/api/v1/queues
curl http://localhost:8080/api/v1/errors?severity=critical
curl http://localhost:8080/api/v1/claude/diagnose
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
    collector.py             # Kafka producer + JSONL file writer
    claude_integration.py    # Claude Code diagnostic analyzer
    litellm_integration.py   # LiteLLM Gateway integration
    otlp_exporter.py         # OpenTelemetry export
    sampling.py              # Sampling & rate limiting
  mcp/
    server.py                # MCP server (6 tools for Claude Code)
  api/
    service.py               # FastAPI service (queue observer, error store)
  oracle/
    state_schema.py          # Strict JSON schema for system state
    state_aggregator.py      # Aggregates state from queues, K8s, LiteLLM
telemetry_data/              # JSONL time-machine files (gitignored)
scripts/
  setup.sh                   # Cluster + Kafka topics setup
k8s/
  deployment.yaml            # Kafka, Ollama, API, shared PVC
  agents-sample.yaml         # Sample agent pods (with shared volume)
.cursor/
  mcp.json                   # MCP server config for Cursor
```

---

## Telemetry Storage

Telemetry is persisted to two JSONL files in `./telemetry_data/`:

| File | Contents | When Written |
|------|----------|--------------|
| `state_time_machine.jsonl` | Full state snapshots (errors, queue messages, counts) | Every 10 errors or 50 queue messages |
| `diff_time_machine.jsonl` | Incremental changes (each error, message, trace, span) | Every add/update/delete |

These files enable time-travel debugging and provide a complete audit trail.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Kafka connection |
| `TELEMETRY_DATA_DIR` | `./telemetry_data` | Directory for JSONL time-machine files |
| `ORACLE_DISCOVERY_MODE` | `auto` | Agent discovery: `auto`, `labeled`, `all`, `telemetry` |
| `ORACLE_STRICT_SCHEMA` | `true` | Strict JSON schema output |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Ollama API URL |

---

Made with care by Pratik Mahalle