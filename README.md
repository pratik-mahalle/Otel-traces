# Multi-Agent Telemetry System

A telemetry system for debugging multi-agent orchestration. Kafka handles **inter-agent communication** through per-agent queues. The **Telemetry API observer** watches queues and pod states, then persists everything to two JSONL files. Claude Code reads them via a **CLI tool + skill** — no MCP, no network hops.

---

## Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │                 K8s CLUSTER                   │
                    │                                               │
                    │   Orchestrator    Researcher    Writer  Coder │
                    │       │               │          │       │   │
                    │       │  inter-agent  │          │       │   │
                    │       └───────────────┴──────────┴───────┘   │
                    │                       │                       │
                    │                 (Kafka queues)                │
                    │                       │                       │
                    │                       ▼                       │
                    │          ┌─────────────────────┐             │
                    │          │  KAFKA               │             │
                    │          │  agent-queue-*       │             │
                    │          └──────────┬──────────┘             │
                    │                     │ observes                │
                    │                     ▼                         │
                    │          ┌─────────────────────┐             │
                    │          │  Telemetry API       │             │
                    │          │  (the OBSERVER)      │             │
                    │          └──────────┬──────────┘             │
                    │                     │ writes                  │
                    │                     ▼                         │
                    │          ┌─────────────────────┐             │
                    │          │  Shared Volume       │             │
                    │          │  state_*.jsonl       │             │
                    │          │  diff_*.jsonl        │             │
                    │          └─────────────────────┘             │
                    └──────────────────────────────────────────────┘
                                          │
                              CLI reads JSONL files locally
                                          │
                                          ▼
                                     Claude Code
                             (oracle-monitor CLI + skill)
```

Key point: **Agents do NOT write their own errors.** The observer captures data from Kafka queue traffic and K8s pod states. The CLI reads the resulting JSONL files as a local subprocess — instant, no network calls.

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

**Example handoff:**

```
1. Orchestrator writes TASK    -> agent-queue-researcher
2. Researcher reads from its own queue
3. Researcher processes the task
4. Researcher writes RESULT    -> agent-queue-orchestrator
5. Orchestrator reads the result from its own queue
```

### 2. Telemetry (Observer-Only, Not in Kafka)

No telemetry data goes to Kafka. The **Telemetry API observer** watches:
- Kafka queue traffic (agent messages, handoffs)
- K8s pod states (crashes, restarts, resource usage)

It writes everything to two JSONL files:

| File | Contents | When Written |
|------|----------|--------------|
| `state_time_machine.jsonl` | Full state snapshots (error counts, queue depths) | Periodically |
| `diff_time_machine.jsonl` | Incremental changes (every error, agent message) | On each event |

### 3. Claude Code Debugging (CLI + Skill)

Claude Code uses the `oracle-monitor` CLI to read the JSONL files. No MCP server — a local subprocess, zero latency:

```bash
# Quick diagnostic
python -m src.oracle.cli diagnose

# Search errors
python -m src.oracle.cli errors -s critical

# Replay a time window
python -m src.oracle.cli replay -m 10

# Check queue health
python -m src.oracle.cli queues

# All diffs
python -m src.oracle.cli diffs --format json
```

The skill file at `.agent/skills/oracle-monitor/SKILL.md` embeds all CLI invocation patterns so Claude Code knows exactly what to run.

---

## Quick Start

```bash
./scripts/setup.sh
kubectl apply -f k8s/agents-sample.yaml
kubectl -n telemetry port-forward service/telemetry-api 8080:8080
curl http://localhost:8080/health
```

---

## Project Structure

```
src/
  agents/
    multi_agent_system.py    # Agent classes with Kafka queue-based handoffs
    emit_telemetry.py        # Sample telemetry agent
  telemetry/
    schemas.py               # AgentMessage, DetailedError, spans, traces
    collector.py             # Kafka producer for inter-agent messages
    claude_integration.py    # Claude Code diagnostic analyzer
  api/
    service.py               # FastAPI (queue observer, error store, JSONL writer)
  oracle/
    cli.py                   # CLI tool for Claude Code (reads JSONL files)
    state_schema.py          # Strict JSON schema for system state
    state_aggregator.py      # Aggregates state from queues, K8s, LiteLLM
telemetry_data/              # JSONL time-machine files (gitignored)
.agent/skills/
  oracle-monitor/SKILL.md   # Skill for Claude Code CLI invocation
scripts/
  setup.sh                   # Cluster + Kafka topics setup
k8s/
  deployment.yaml            # Kafka, Ollama, API, shared PVC
  agents-sample.yaml         # Sample agent pods
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KAFKA_BOOTSTRAP_SERVERS` | `kafka:9092` | Kafka connection |
| `TELEMETRY_DATA_DIR` | `./telemetry_data` | Directory for JSONL time-machine files |
| `ORACLE_STRICT_SCHEMA` | `true` | Strict JSON schema output |

---

Made with care by Pratik Mahalle
