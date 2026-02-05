# Multi-Agent Telemetry (Claude Code Ready)

This system emits and aggregates telemetry for multi-agent debugging. Oracle Monitor exposes a strict JSON schema that Claude Code can query.

**Quick Start**

```bash
./scripts/setup.sh
kubectl apply -f k8s/agents-sample.yaml
```

**Access**

```bash
# API
kubectl -n telemetry port-forward service/telemetry-api 8080:8080

# Dashboard
kubectl -n telemetry port-forward service/telemetry-dashboard 8081:80
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

**Agent Discovery (Kubernetes)**

Oracle Monitor discovers agents from deployments labeled with:

- `oracle-monitor/agent=true`
- `oracle-monitor/name` (optional)
- `oracle-monitor/models` (optional, comma-separated)
- `oracle-monitor/description` (optional, annotation or label)

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

- Mocks are disabled in production.
- Oracle state defaults to strict schema output.
