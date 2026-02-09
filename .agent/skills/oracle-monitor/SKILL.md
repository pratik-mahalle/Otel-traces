---
name: Oracle Monitor Debug Skill
description: Debug multi-agent systems using JSONL time-machine files via the CLI. No MCP — local subprocess, instant, reliable.
---

# Oracle Monitor Debug Skill

Debug multi-agent systems by reading two JSONL files that the **Telemetry API observer** writes. Agents do **not** report their own errors — the observer captures data from Kafka inter-agent queues and K8s pod states, then persists to:

| File | Purpose |
|------|---------|
| `state_time_machine.jsonl` | Periodic full-state snapshots |
| `diff_time_machine.jsonl`  | Incremental changes (errors, agent messages) |

Set `TELEMETRY_DATA_DIR` to the directory containing these files (default: `./telemetry_data`).

## CLI Commands

All commands run as a local subprocess — no network calls, no MCP:

```bash
# ── Live State ──
python -m src.oracle.cli state               # Current system state
python -m src.oracle.cli state -f json        # As JSON
python -m src.oracle.cli agents               # List agents
python -m src.oracle.cli workload             # K8s workloads
python -m src.oracle.cli llm                  # LLM model usage

# ── Time-Machine (reads from JSONL files) ──
python -m src.oracle.cli errors               # Recent errors
python -m src.oracle.cli errors -s critical   # Only critical
python -m src.oracle.cli errors -a Researcher # By agent name
python -m src.oracle.cli diffs                # All recent diffs
python -m src.oracle.cli diffs -e error       # Only error diffs
python -m src.oracle.cli replay -m 10         # Replay last 10 minutes
python -m src.oracle.cli queues               # Inter-agent queue status
python -m src.oracle.cli diagnose             # Full diagnostic report
```

Add `--format json` to any command for machine-readable output.

## Debugging Workflow

1. **Start with a quick diagnostic:**
   ```bash
   python -m src.oracle.cli diagnose
   ```
   This reads both JSONL files and reports: latest state snapshot, error count, queue backlogs, and identified issues.

2. **If errors found, drill down:**
   ```bash
   python -m src.oracle.cli errors -s critical --limit 10
   ```

3. **Replay a specific window:**
   ```bash
   python -m src.oracle.cli replay --after "2026-02-08T10:00:00" --before "2026-02-08T10:05:00"
   ```

4. **Check queue health:**
   ```bash
   python -m src.oracle.cli queues
   ```
   Look for agents with high pending counts (tasks piling up).

5. **If live state is needed:**
   ```bash
   python -m src.oracle.cli state -f json
   ```

## Architecture: Why CLI, Not MCP

From the problem statement:

> MCP for internal tools is engineering malpractice. Fragmented traces are killing you — why create more?

This system uses:
- **CLI tool**: Local subprocess, zero latency, no network hops
- **Skill file**: Embeds invocation patterns so Claude Code knows exactly how to call the CLI
- **JSONL files**: Append-only, replayable, written by one observer — not by agents themselves

Agents communicate via **Kafka inter-agent queues** (pattern: Agent1 -> Agent2 Queue -> Agent2). The **Telemetry API observer** watches those queues and K8s pod states, then writes to the JSONL files. Claude Code reads them via this CLI.

## Common Issues

### Agent Not Responding
```bash
python -m src.oracle.cli diagnose            # Check overall health
python -m src.oracle.cli queues              # Is the agent's queue backed up?
python -m src.oracle.cli errors -a AgentName # Recent errors for that agent
```

### Tasks Stuck in Queue
```bash
python -m src.oracle.cli queues              # Identify backed-up queues
python -m src.oracle.cli replay -m 5         # What happened in last 5 min?
```

### High Error Rate
```bash
python -m src.oracle.cli errors --limit 50   # Get a batch of errors
python -m src.oracle.cli diffs -e error      # Error diffs over time
```
