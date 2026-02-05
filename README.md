# Multi-Agent Telemetry System

This system emits and aggregates telemetry for multi-agent debugging. Oracle Monitor exposes a strict JSON schema that Claude Code can query for troubleshooting.

**Quick Start**

```bash
./scripts/setup.sh
kubectl apply -f k8s/agents-sample.yaml
```

**Access (Recommended)**

```bash
# API
kubectl -n telemetry port-forward service/telemetry-api 8080:8080

# Dashboard
kubectl -n telemetry port-forward service/telemetry-dashboard 8081:80
```

**Verify Health**

```bash
curl http://localhost:8080/health
```

**Validate Schema**

```bash
curl http://localhost:8080/api/v1/oracle/validate
```

Expected:

```json
{"valid":true,"error_count":0,"errors":[]}
```

**Oracle State (Claude Code)**

```bash
curl http://localhost:8080/api/v1/oracle/state
```

**Trace Check**

```bash
curl http://localhost:8080/api/v1/traces?limit=5
```

**Claude Code Integration**

Claude Code can consume the Oracle state directly. Use these endpoints as the data source:

```bash
# Strict schema state
curl http://localhost:8080/api/v1/oracle/state

# Schema validation (pass/fail for the current state)
curl http://localhost:8080/api/v1/oracle/validate
```

Recommended prompt for Claude Code:

```
You are debugging a multi-agent system. Use the Oracle state JSON from /api/v1/oracle/state.
Identify errors, bottlenecks, and missing correlations. Propose fixes.
```

**Agent Discovery (Kubernetes)**

Oracle Monitor discovers agents from deployments labeled with:

- `oracle-monitor/agent=true`
- `oracle-monitor/name` (optional)
- `oracle-monitor/models` (optional, comma-separated; use annotation if you need commas)
- `oracle-monitor/description` (optional; label or annotation)

Example:

```yaml
metadata:
  labels:
    oracle-monitor/agent: "true"
    oracle-monitor/name: "Researcher"
  annotations:
    oracle-monitor/models: "gpt-4o-mini,claude-3-5-sonnet"
    oracle-monitor/description: "Research agent"
```

**Notes**

- Mocks are disabled in production (`ORACLE_ALLOW_MOCKS=false`).
- Oracle state defaults to strict schema output (`ORACLE_STRICT_SCHEMA=true`).
- If `localhost:30080` is unreachable, use port-forward as shown above.


Made with ❤️ by Pratik Mahalle
