"""
MCP Server for Telemetry Time-Machine

Reads state_time_machine.jsonl and diff_time_machine.jsonl to provide
Claude Code with structured debugging tools via the Model Context Protocol.

Run: python -m src.mcp.server
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("TELEMETRY_DATA_DIR", "./telemetry_data"))
STATE_FILE = DATA_DIR / "state_time_machine.jsonl"
DIFF_FILE = DATA_DIR / "diff_time_machine.jsonl"

mcp = FastMCP(
    name="telemetry-time-machine",
    instructions="""
    This server provides debugging tools for a multi-agent telemetry system.
    It reads two JSONL files:
    - state_time_machine.jsonl: periodic full state snapshots
    - diff_time_machine.jsonl: incremental changes (errors, queue messages, etc.)

    Use these tools to investigate agent errors, replay timelines, and diagnose
    issues in multi-agent orchestration systems.
    """,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path, max_lines: int = 0) -> list[dict]:
    """Read a JSONL file and return parsed lines. If max_lines > 0, return last N."""
    if not path.exists():
        return []
    lines: list[dict] = []
    with open(path, "r") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    if max_lines > 0:
        return lines[-max_lines:]
    return lines


def _filter_by_time(records: list[dict], after: Optional[str], before: Optional[str]) -> list[dict]:
    """Filter records by timestamp range (ISO format strings)."""
    filtered = records
    if after:
        try:
            after_dt = datetime.fromisoformat(after)
            filtered = [r for r in filtered if datetime.fromisoformat(r.get("timestamp", "")) >= after_dt]
        except ValueError:
            pass
    if before:
        try:
            before_dt = datetime.fromisoformat(before)
            filtered = [r for r in filtered if datetime.fromisoformat(r.get("timestamp", "")) <= before_dt]
        except ValueError:
            pass
    return filtered


# ---------------------------------------------------------------------------
# Tool 1: get_latest_state
# ---------------------------------------------------------------------------


@mcp.tool
def get_latest_state() -> dict:
    """
    Read the most recent state snapshot from state_time_machine.jsonl.

    Returns the latest full state including error counts, message counts,
    active traces/spans, and service info. Returns empty dict if no snapshots.
    """
    lines = _read_jsonl(STATE_FILE, max_lines=1)
    if not lines:
        return {"error": "No state snapshots found", "file": str(STATE_FILE)}
    return lines[0]


# ---------------------------------------------------------------------------
# Tool 2: query_diffs
# ---------------------------------------------------------------------------


@mcp.tool
def query_diffs(
    entity_type: Optional[str] = None,
    change_type: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Query incremental diffs from diff_time_machine.jsonl.

    Args:
        entity_type: Filter by type (error, agent_message, trace, span, event, handoff, metric)
        change_type: Filter by change type (added, updated, deleted)
        after: Only return diffs after this ISO timestamp (e.g. "2025-01-15T10:00:00")
        before: Only return diffs before this ISO timestamp
        limit: Max results to return (default 50)

    Returns list of diff records, most recent first.
    """
    records = _read_jsonl(DIFF_FILE)

    if entity_type:
        records = [r for r in records if r.get("entity_type") == entity_type]
    if change_type:
        records = [r for r in records if r.get("change_type") == change_type]

    records = _filter_by_time(records, after, before)

    # Most recent first, limited
    return list(reversed(records[-limit:]))


# ---------------------------------------------------------------------------
# Tool 3: get_errors
# ---------------------------------------------------------------------------


@mcp.tool
def get_errors(
    severity: Optional[str] = None,
    category: Optional[str] = None,
    agent_name: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 30,
) -> list[dict]:
    """
    Search errors from diff_time_machine.jsonl.

    Args:
        severity: Filter by severity (critical, error, warning, info)
        category: Filter by category (llm_error, tool_error, agent_error, etc.)
        agent_name: Filter by agent name
        after: Only return errors after this ISO timestamp
        before: Only return errors before this ISO timestamp
        limit: Max results (default 30)

    Returns list of error records with full detail (stack trace, suggestions, etc.).
    """
    records = _read_jsonl(DIFF_FILE)

    # Only error diffs
    records = [r for r in records if r.get("entity_type") == "error"]

    records = _filter_by_time(records, after, before)

    # Filter on nested data fields
    if severity:
        records = [r for r in records if r.get("data", {}).get("severity") == severity]
    if category:
        records = [r for r in records if r.get("data", {}).get("category") == category]
    if agent_name:
        records = [r for r in records if r.get("data", {}).get("agent_name") == agent_name]

    # Most recent first
    results = list(reversed(records[-limit:]))

    # Return the inner data for readability
    return [
        {
            "timestamp": r.get("timestamp"),
            "service_name": r.get("service_name"),
            **r.get("data", {}),
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Tool 4: replay_timeline
# ---------------------------------------------------------------------------


@mcp.tool
def replay_timeline(
    after: Optional[str] = None,
    before: Optional[str] = None,
    minutes: int = 5,
    limit: int = 100,
) -> list[dict]:
    """
    Replay events in chronological order for a given time window.

    Shows what happened step by step: errors, queue messages, traces, etc.
    If neither after nor before is set, defaults to the last N minutes.

    Args:
        after: Start of window (ISO timestamp)
        before: End of window (ISO timestamp)
        minutes: If after/before not set, replay last N minutes (default 5)
        limit: Max events to return (default 100)

    Returns chronologically ordered list of all diffs in the window.
    """
    if not after and not before:
        before_dt = datetime.utcnow()
        after_dt = before_dt - timedelta(minutes=minutes)
        after = after_dt.isoformat()
        before = before_dt.isoformat()

    records = _read_jsonl(DIFF_FILE)
    records = _filter_by_time(records, after, before)

    # Chronological order, limited
    records = records[:limit]

    return [
        {
            "timestamp": r.get("timestamp"),
            "change_type": r.get("change_type"),
            "entity_type": r.get("entity_type"),
            "service_name": r.get("service_name"),
            "summary": _summarize_diff(r),
        }
        for r in records
    ]


def _summarize_diff(record: dict) -> str:
    """Create a human-readable one-line summary of a diff record."""
    entity = record.get("entity_type", "unknown")
    data = record.get("data", {})

    if entity == "error":
        sev = data.get("severity", "?")
        msg = data.get("error_message", "")[:80]
        agent = data.get("agent_name", "?")
        return f"[{sev}] {agent}: {msg}"

    if entity == "agent_message":
        src = data.get("source_agent", "?")
        tgt = data.get("target_agent", "?")
        mtype = data.get("message_type", "?")
        return f"[{mtype}] {src} -> {tgt}"

    if entity == "trace":
        tid = data.get("trace_id", "?")[:12]
        return f"trace {tid}"

    if entity == "span":
        agent = data.get("agent_name", "?")
        kind = data.get("span_kind", "?")
        return f"span {kind} by {agent}"

    return json.dumps(data)[:100]


# ---------------------------------------------------------------------------
# Tool 5: get_queue_status
# ---------------------------------------------------------------------------


@mcp.tool
def get_queue_status(agent_name: Optional[str] = None) -> dict:
    """
    Get current agent queue state from time-machine data.

    Shows messages sent/received per agent and any pending tasks.

    Args:
        agent_name: Optional filter for a specific agent

    Returns dict with per-agent queue stats.
    """
    records = _read_jsonl(DIFF_FILE)

    # Only agent messages
    messages = [r for r in records if r.get("entity_type") == "agent_message"]

    # Build per-agent stats
    queues: dict[str, dict] = {}

    for msg in messages:
        data = msg.get("data", {})
        src = data.get("source_agent", "unknown")
        tgt = data.get("target_agent", "unknown")
        mtype = data.get("message_type", "unknown")

        # Track outgoing
        if src not in queues:
            queues[src] = {"sent": 0, "received": 0, "pending_tasks": 0, "errors_sent": 0}
        queues[src]["sent"] += 1

        # Track incoming
        if tgt not in queues:
            queues[tgt] = {"sent": 0, "received": 0, "pending_tasks": 0, "errors_sent": 0}
        queues[tgt]["received"] += 1

        # Track task/result balance for pending
        if mtype == "task":
            queues[tgt]["pending_tasks"] += 1
        elif mtype == "result":
            # Result goes back to the source of the original task
            queues[tgt]["pending_tasks"] = max(0, queues[tgt].get("pending_tasks", 0) - 1)
        elif mtype == "error":
            queues[src]["errors_sent"] += 1

    if agent_name:
        return {agent_name: queues.get(agent_name, {"sent": 0, "received": 0, "pending_tasks": 0})}

    return queues


# ---------------------------------------------------------------------------
# Tool 6: diagnose
# ---------------------------------------------------------------------------


@mcp.tool
def diagnose() -> dict:
    """
    Run a full diagnostic: recent errors + queue bottlenecks + suggested fixes.

    Combines state snapshots and diffs to produce a summary for debugging.
    Returns a dict with sections: state, recent_errors, queue_status, issues.
    """
    # Latest state
    state_lines = _read_jsonl(STATE_FILE, max_lines=1)
    latest_state = state_lines[0] if state_lines else {}

    # Recent errors (last 20)
    all_diffs = _read_jsonl(DIFF_FILE)
    error_diffs = [r for r in all_diffs if r.get("entity_type") == "error"]
    recent_errors = error_diffs[-20:]

    # Queue status
    queue_status = get_queue_status()

    # Identify issues
    issues: list[str] = []

    # Check for critical errors
    critical = [
        r for r in recent_errors if r.get("data", {}).get("severity") == "critical"
    ]
    if critical:
        issues.append(f"{len(critical)} critical error(s) in recent history")

    # Check for queue imbalance (pending tasks piling up)
    for agent, stats in queue_status.items():
        if isinstance(stats, dict) and stats.get("pending_tasks", 0) > 5:
            issues.append(f"Agent '{agent}' has {stats['pending_tasks']} pending tasks (possible bottleneck)")

    # Check for error concentration on a single agent
    agent_error_counts: dict[str, int] = {}
    for r in recent_errors:
        agent = r.get("data", {}).get("agent_name", "unknown")
        agent_error_counts[agent] = agent_error_counts.get(agent, 0) + 1
    for agent, count in agent_error_counts.items():
        if count >= 5:
            issues.append(f"Agent '{agent}' has {count} recent errors (needs investigation)")

    # Suggested fixes based on error categories
    suggestions: list[str] = []
    categories = set()
    for r in recent_errors:
        cat = r.get("data", {}).get("category")
        if cat:
            categories.add(cat)
        # Include suggested_fixes from errors
        fixes = r.get("data", {}).get("suggested_fixes", [])
        suggestions.extend(fixes[:2])

    if "llm_error" in categories:
        suggestions.append("Check LLM API keys and rate limits")
    if "tool_error" in categories:
        suggestions.append("Verify tool configurations and dependencies")
    if "timeout" in categories:
        suggestions.append("Consider increasing timeout thresholds")

    # Deduplicate
    suggestions = list(dict.fromkeys(suggestions))

    return {
        "state": latest_state,
        "recent_errors_count": len(recent_errors),
        "recent_errors": [
            {
                "timestamp": r.get("timestamp"),
                "severity": r.get("data", {}).get("severity"),
                "category": r.get("data", {}).get("category"),
                "agent_name": r.get("data", {}).get("agent_name"),
                "message": r.get("data", {}).get("error_message", "")[:120],
            }
            for r in recent_errors[-10:]
        ],
        "queue_status": queue_status,
        "issues": issues if issues else ["No issues detected"],
        "suggestions": suggestions[:10] if suggestions else ["System appears healthy"],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
