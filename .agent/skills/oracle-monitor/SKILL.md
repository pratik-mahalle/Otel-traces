---
name: Oracle Monitor Debug Skill
description: Use the Oracle Monitor telemetry system to debug multi-agent system issues
---

# Oracle Monitor Debug Skill

This skill enables you to debug multi-agent systems using the Oracle Monitor telemetry system. The system provides a unified view of agents, workloads, queues, and LLM usage.

## Quick Access

### Get System State
```bash
# Get current system state as log format (human-readable)
curl -s http://localhost:8080/api/v1/oracle/state?format=log | jq -r '.log'

# Get as JSON for programmatic access
curl -s http://localhost:8080/api/v1/oracle/state

# Get summary only
curl -s http://localhost:8080/api/v1/oracle/state?format=summary | jq
```

### Check for Issues
```bash
# Quick issue check
curl -s http://localhost:8080/api/v1/oracle/issues | jq
```

### Get Specific Components
```bash
# List agents and their activity
curl -s http://localhost:8080/api/v1/oracle/agents | jq

# Get Kubernetes workload metrics
curl -s http://localhost:8080/api/v1/oracle/workload | jq

# Get LLM model usage
curl -s http://localhost:8080/api/v1/oracle/llm | jq
```

## CLI Tool

The Oracle Monitor CLI can be run directly:

```bash
# From the project directory
python -m src.oracle.cli state      # Get system state
python -m src.oracle.cli agents     # List agents
python -m src.oracle.cli workload   # Show Kubernetes workloads
python -m src.oracle.cli llm        # Show LLM usage
python -m src.oracle.cli watch      # Real-time monitoring
python -m src.oracle.cli debug      # Interactive debug session
```

## Understanding the State

### Agent State
- **name**: Agent identifier
- **deployment_name**: Kubernetes deployment
- **models**: LLM models the agent can use
- **activity.active_task_ids**: Currently running tasks

### Workload State
- **deployment_name**: Kubernetes deployment
- **live.active_pods**: Number of running pods
- **max_pods**: Maximum allowed pods
- **pods**: Individual pod metrics (CPU, memory, status)

### Queue State
- **name**: Queue/topic name
- **tasks**: Pending tasks with priority levels
- **tasks[].priority.level**: low, normal, high, critical

### LLM State
- **model**: Model identifier
- **provider**: Provider name (ollama, openai, anthropic)
- **tpm/tpm_max**: Tokens per minute usage/limit
- **rpm/rpm_max**: Requests per minute usage/limit

## Debugging Workflow

1. **Start by checking issues:**
   ```bash
   curl -s http://localhost:8080/api/v1/oracle/issues | jq
   ```

2. **If issues found, get detailed state:**
   ```bash
   curl -s http://localhost:8080/api/v1/oracle/state?format=log | jq -r '.log'
   ```

3. **For trace-level debugging:**
   ```bash
   # List recent traces
   curl -s http://localhost:8080/api/v1/traces?limit=10 | jq
   
   # Get specific trace details
   curl -s http://localhost:8080/api/v1/traces/{trace_id} | jq
   ```

4. **For real-time events:**
   Connect to WebSocket at `ws://localhost:8080/ws/debug/{client_id}`

## Common Issues

### Agent Not Responding
1. Check if agent deployment is running: `/api/v1/oracle/workload`
2. Check if LLM is available: `/api/v1/oracle/llm`
3. Look at recent traces for errors: `/api/v1/traces?status=failed`

### High Latency
1. Check LLM rate limits: `/api/v1/oracle/llm`
2. Check queue depth: `/api/v1/oracle/state`
3. Check pod resource usage: `/api/v1/oracle/workload`

### Tasks Stuck in Queue
1. Check queue state: `/api/v1/oracle/state?format=summary`
2. Look for blocked tasks with high `waiting_since_mins`
3. Check for failed agents

## API Reference

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/oracle/state` | Full system state |
| `GET /api/v1/oracle/state?format=log` | Human-readable log format |
| `GET /api/v1/oracle/state?format=summary` | Just summary metrics |
| `GET /api/v1/oracle/agents` | Agent list and activity |
| `GET /api/v1/oracle/workload` | Kubernetes workload metrics |
| `GET /api/v1/oracle/llm` | LLM model usage |
| `GET /api/v1/oracle/issues` | Current system issues |
