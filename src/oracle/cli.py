#!/usr/bin/env python3
"""
Oracle Monitor CLI
Command-line interface for debugging multi-agent systems.
Designed for integration with Claude Code.
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.oracle.state_aggregator import OracleMonitorAggregator


class Colors:
    """ANSI color codes for terminal output"""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'
    GRAY = '\033[90m'


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def print_banner():
    """Print Oracle Monitor banner"""
    banner = f"""
{Colors.CYAN}╔═══════════════════════════════════════════════════════════════╗
║  {Colors.BOLD}Oracle Monitor{Colors.END}{Colors.CYAN}                                                  ║
║  Multi-Agent System State for Claude Code                       ║
╚═══════════════════════════════════════════════════════════════╝{Colors.END}
"""
    print(banner)


async def cmd_state(args):
    """Get current system state"""
    aggregator = OracleMonitorAggregator(
        namespace=args.namespace,
        api_url=args.api_url,
        ollama_url=args.ollama_url
    )
    
    state = await aggregator.get_state()
    
    if args.format == "json":
        strict_output = _env_bool("ORACLE_STRICT_SCHEMA", True)
        print(state.to_json(indent=2, strict=strict_output))
    elif args.format == "summary":
        summary = state.get_summary()
        print(f"\n{Colors.BOLD}System Summary{Colors.END}")
        print(f"{'='*40}")
        print(f"Status: {Colors.GREEN if summary['status'] == 'healthy' else Colors.RED}{summary['status'].upper()}{Colors.END}")
        print(f"Agents: {summary['agent_count']}")
        print(f"Active Tasks: {summary['active_tasks']}")
        print(f"Queued Tasks: {summary['queued_tasks']}")
        print(f"Running Pods: {summary['running_pods']}")
        print(f"LLM Models: {summary['llm_models']}")
        
        if summary['issues']:
            print(f"\n{Colors.YELLOW}Issues:{Colors.END}")
            for issue in summary['issues']:
                print(f"  {Colors.RED}!{Colors.END} {issue}")
    else:
        print(state.to_diff_log())


async def cmd_agents(args):
    """List agents and their status"""
    aggregator = OracleMonitorAggregator(
        namespace=args.namespace,
        api_url=args.api_url,
        ollama_url=args.ollama_url
    )
    
    state = await aggregator.get_state()
    
    if args.format == "json":
        print(json.dumps([a.to_dict() for a in state.agents], indent=2))
    else:
        print(f"\n{Colors.BOLD}Registered Agents{Colors.END}")
        print(f"{'='*60}")
        
        for agent in state.agents:
            active = len(agent.activity.active_task_ids)
            running = sum(1 for t in agent.activity.active_task_ids if t.status.value == "running")
            
            status_color = Colors.GREEN if active == 0 else Colors.YELLOW
            if any(t.status.value == "failed" for t in agent.activity.active_task_ids):
                status_color = Colors.RED
            
            print(f"\n{Colors.BOLD}{agent.name}{Colors.END}")
            print(f"  {Colors.GRAY}Deployment:{Colors.END} {agent.deployment_name}")
            print(f"  {Colors.GRAY}Models:{Colors.END} {', '.join(agent.models)}")
            print(f"  {Colors.GRAY}Max Parallel:{Colors.END} {agent.max_parallel_invocations}")
            print(f"  {Colors.GRAY}Status:{Colors.END} {status_color}{running}/{active} running{Colors.END}")
            
            if agent.activity.active_task_ids:
                print(f"  {Colors.GRAY}Active Tasks:{Colors.END}")
                for task in agent.activity.active_task_ids:
                    print(f"    - {task.id[:16]}... ({task.status.value})")


async def cmd_workload(args):
    """Show Kubernetes workload status"""
    aggregator = OracleMonitorAggregator(
        namespace=args.namespace,
        api_url=args.api_url,
        ollama_url=args.ollama_url
    )
    
    state = await aggregator.get_state()
    
    if args.format == "json":
        print(json.dumps([w.to_dict() for w in state.workload], indent=2))
    else:
        print(f"\n{Colors.BOLD}Kubernetes Workloads{Colors.END}")
        print(f"{'='*60}")
        
        for workload in state.workload:
            active = workload.live.active_pods
            max_pods = workload.max_pods
            
            # Color based on capacity
            if active == 0:
                color = Colors.RED
            elif active < max_pods:
                color = Colors.YELLOW
            else:
                color = Colors.GREEN
            
            print(f"\n{Colors.BOLD}{workload.deployment_name}{Colors.END}")
            print(f"  {Colors.GRAY}Pods:{Colors.END} {color}{active}/{max_pods}{Colors.END}")
            
            if workload.live.image:
                print(f"  {Colors.GRAY}Image:{Colors.END} {workload.live.image}")
            if workload.pod_max_ram:
                print(f"  {Colors.GRAY}Max RAM:{Colors.END} {workload.pod_max_ram}")
            if workload.pod_max_cpu:
                print(f"  {Colors.GRAY}Max CPU:{Colors.END} {workload.pod_max_cpu}")
            
            if workload.pods:
                print(f"  {Colors.GRAY}Pod Details:{Colors.END}")
                for pod in workload.pods:
                    pod_color = Colors.GREEN if pod.status.value == "Running" else Colors.RED
                    print(f"    - {pod.pod_id}: {pod_color}{pod.status.value}{Colors.END} (CPU: {pod.cpu}m, Mem: {pod.memory}MB)")


async def cmd_llm(args):
    """Show LLM model status and usage"""
    aggregator = OracleMonitorAggregator(
        namespace=args.namespace,
        api_url=args.api_url,
        ollama_url=args.ollama_url
    )
    
    state = await aggregator.get_state()
    
    if args.format == "json":
        print(json.dumps([m.to_dict() for m in state.litellm], indent=2))
    else:
        print(f"\n{Colors.BOLD}LLM Models{Colors.END}")
        print(f"{'='*60}")
        
        for model in state.litellm:
            tpm_pct = (model.tpm / model.tpm_max * 100) if model.tpm_max > 0 else 0
            rpm_pct = (model.rpm / model.rpm_max * 100) if model.rpm_max > 0 else 0
            
            # Color based on usage
            tpm_color = Colors.GREEN if tpm_pct < 70 else (Colors.YELLOW if tpm_pct < 90 else Colors.RED)
            rpm_color = Colors.GREEN if rpm_pct < 70 else (Colors.YELLOW if rpm_pct < 90 else Colors.RED)
            
            print(f"\n{Colors.BOLD}{model.model}{Colors.END}")
            print(f"  {Colors.GRAY}Provider:{Colors.END} {model.provider}")
            print(f"  {Colors.GRAY}TPM:{Colors.END} {tpm_color}{model.tpm:.0f}/{model.tpm_max:.0f} ({tpm_pct:.0f}%){Colors.END}")
            print(f"  {Colors.GRAY}RPM:{Colors.END} {rpm_color}{model.rpm:.0f}/{model.rpm_max:.0f} ({rpm_pct:.0f}%){Colors.END}")
            
            if model.payment_type:
                print(f"  {Colors.GRAY}Payment:{Colors.END} {model.payment_type.value}")
            if model.input_context:
                print(f"  {Colors.GRAY}Context:{Colors.END} {model.input_context} tokens")


async def cmd_watch(args):
    """Watch system state in real-time"""
    aggregator = OracleMonitorAggregator(
        namespace=args.namespace,
        api_url=args.api_url,
        ollama_url=args.ollama_url
    )
    
    print(f"{Colors.CYAN}Watching system state (Ctrl+C to stop)...{Colors.END}")
    
    try:
        async for state in aggregator.watch(args.interval):
            # Clear screen
            print("\033[2J\033[H", end="")
            
            if args.format == "json":
                strict_output = _env_bool("ORACLE_STRICT_SCHEMA", True)
                print(state.to_json(strict=strict_output))
            else:
                print_banner()
                print(state.to_diff_log())
            
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Watch stopped.{Colors.END}")


async def cmd_debug(args):
    """Interactive debugging session"""
    aggregator = OracleMonitorAggregator(
        namespace=args.namespace,
        api_url=args.api_url,
        ollama_url=args.ollama_url
    )
    
    print_banner()
    print(f"{Colors.CYAN}Interactive Debug Mode{Colors.END}")
    print("Commands: state, agents, workload, llm, issues, exit")
    print()
    
    while True:
        try:
            cmd = input(f"{Colors.GREEN}oracle>{Colors.END} ").strip().lower()
            
            if cmd == "exit" or cmd == "quit":
                break
            elif cmd == "state":
                state = await aggregator.get_state()
                print(state.to_diff_log())
            elif cmd == "agents":
                state = await aggregator.get_state()
                for agent in state.agents:
                    active = len(agent.activity.active_task_ids)
                    print(f"  {agent.name}: {active} active tasks")
            elif cmd == "workload":
                state = await aggregator.get_state()
                for w in state.workload:
                    print(f"  {w.deployment_name}: {w.live.active_pods}/{w.max_pods} pods")
            elif cmd == "llm":
                state = await aggregator.get_state()
                for m in state.litellm:
                    print(f"  {m.model}: {m.tpm:.0f}/{m.tpm_max:.0f} TPM")
            elif cmd == "issues":
                state = await aggregator.get_state()
                summary = state.get_summary()
                if summary['issues']:
                    for issue in summary['issues']:
                        print(f"  {Colors.RED}!{Colors.END} {issue}")
                else:
                    print(f"  {Colors.GREEN}No issues{Colors.END}")
            elif cmd == "help":
                print("Commands: state, agents, workload, llm, issues, exit")
            else:
                print(f"Unknown command: {cmd}")
                
        except KeyboardInterrupt:
            print()
            break
        except EOFError:
            break
    
    print(f"{Colors.YELLOW}Debug session ended.{Colors.END}")


# ========================================================================
# Time-Machine Commands (read JSONL files written by Telemetry API observer)
# ========================================================================

def _get_data_dir() -> Path:
    return Path(os.environ.get("TELEMETRY_DATA_DIR", "./telemetry_data"))


def _read_jsonl(path: Path, max_lines: int = 0) -> list:
    if not path.exists():
        return []
    lines = []
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


def _filter_by_time(records: list, after: str = None, before: str = None) -> list:
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


def _summarize_diff(record: dict) -> str:
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
    return json.dumps(data)[:100]


async def cmd_errors(args):
    """Search errors from diff_time_machine.jsonl"""
    data_dir = _get_data_dir()
    diff_file = data_dir / "diff_time_machine.jsonl"
    records = _read_jsonl(diff_file)
    records = [r for r in records if r.get("entity_type") == "error"]
    records = _filter_by_time(records, getattr(args, "after", None), getattr(args, "before", None))

    if hasattr(args, "severity") and args.severity:
        records = [r for r in records if r.get("data", {}).get("severity") == args.severity]
    if hasattr(args, "agent") and args.agent:
        records = [r for r in records if r.get("data", {}).get("agent_name") == args.agent]

    limit = getattr(args, "limit", 30)
    records = list(reversed(records[-limit:]))

    if args.format == "json":
        print(json.dumps(records, indent=2, default=str))
    else:
        if not records:
            print(f"{Colors.GREEN}No errors found.{Colors.END}")
            return
        print(f"\n{Colors.BOLD}Errors ({len(records)}){Colors.END}")
        print(f"{'='*60}")
        for r in records:
            data = r.get("data", {})
            ts = r.get("timestamp", "")[:19]
            sev = data.get("severity", "?")
            cat = data.get("category", "?")
            agent = data.get("agent_name", "?")
            msg = data.get("error_message", "")[:80]
            color = Colors.RED if sev in ("critical", "error") else Colors.YELLOW
            print(f"  {Colors.GRAY}{ts}{Colors.END} {color}[{sev}]{Colors.END} {cat} | {agent}: {msg}")


async def cmd_diffs(args):
    """Query diffs from diff_time_machine.jsonl"""
    data_dir = _get_data_dir()
    diff_file = data_dir / "diff_time_machine.jsonl"
    records = _read_jsonl(diff_file)

    if hasattr(args, "entity_type") and args.entity_type:
        records = [r for r in records if r.get("entity_type") == args.entity_type]
    records = _filter_by_time(records, getattr(args, "after", None), getattr(args, "before", None))

    limit = getattr(args, "limit", 50)
    records = list(reversed(records[-limit:]))

    if args.format == "json":
        print(json.dumps(records, indent=2, default=str))
    else:
        if not records:
            print(f"{Colors.GREEN}No diffs found.{Colors.END}")
            return
        print(f"\n{Colors.BOLD}Diffs ({len(records)}){Colors.END}")
        print(f"{'='*60}")
        for r in records:
            ts = r.get("timestamp", "")[:19]
            entity = r.get("entity_type", "?")
            summary = _summarize_diff(r)
            print(f"  {Colors.GRAY}{ts}{Colors.END} [{entity}] {summary}")


async def cmd_replay(args):
    """Replay timeline from diff_time_machine.jsonl"""
    data_dir = _get_data_dir()
    diff_file = data_dir / "diff_time_machine.jsonl"
    records = _read_jsonl(diff_file)

    after = getattr(args, "after", None)
    before = getattr(args, "before", None)
    if not after and not before:
        minutes = getattr(args, "minutes", 5)
        before_dt = datetime.utcnow()
        after_dt = before_dt - timedelta(minutes=minutes)
        after = after_dt.isoformat()
        before = before_dt.isoformat()

    records = _filter_by_time(records, after, before)
    limit = getattr(args, "limit", 100)
    records = records[:limit]

    if args.format == "json":
        print(json.dumps(records, indent=2, default=str))
    else:
        if not records:
            print(f"{Colors.GREEN}No events in this time window.{Colors.END}")
            return
        print(f"\n{Colors.BOLD}Timeline Replay ({len(records)} events){Colors.END}")
        print(f"{'='*60}")
        for r in records:
            ts = r.get("timestamp", "")[:19]
            entity = r.get("entity_type", "?")
            summary = _summarize_diff(r)
            print(f"  {Colors.GRAY}{ts}{Colors.END} {Colors.CYAN}[{entity}]{Colors.END} {summary}")


async def cmd_queues(args):
    """Show queue status from diff_time_machine.jsonl"""
    data_dir = _get_data_dir()
    diff_file = data_dir / "diff_time_machine.jsonl"
    records = _read_jsonl(diff_file)
    messages = [r for r in records if r.get("entity_type") == "agent_message"]

    queues = {}
    for msg in messages:
        data = msg.get("data", {})
        src = data.get("source_agent", "unknown")
        tgt = data.get("target_agent", "unknown")
        mtype = data.get("message_type", "unknown")
        for name in (src, tgt):
            if name not in queues:
                queues[name] = {"sent": 0, "received": 0, "pending": 0}
        queues[src]["sent"] += 1
        queues[tgt]["received"] += 1
        if mtype == "task":
            queues[tgt]["pending"] += 1
        elif mtype == "result":
            queues[tgt]["pending"] = max(0, queues[tgt]["pending"] - 1)

    if args.format == "json":
        print(json.dumps(queues, indent=2))
    else:
        if not queues:
            print(f"{Colors.GREEN}No queue data yet.{Colors.END}")
            return
        print(f"\n{Colors.BOLD}Agent Queue Status{Colors.END}")
        print(f"{'='*60}")
        print(f"  {'Agent':<20} {'Sent':>6} {'Recv':>6} {'Pending':>8}")
        print(f"  {'-'*20} {'-'*6} {'-'*6} {'-'*8}")
        for name, stats in sorted(queues.items()):
            pending_color = Colors.RED if stats["pending"] > 5 else Colors.GREEN
            print(f"  {name:<20} {stats['sent']:>6} {stats['received']:>6} {pending_color}{stats['pending']:>8}{Colors.END}")


async def cmd_tm_diagnose(args):
    """Full diagnostic from time-machine files"""
    data_dir = _get_data_dir()
    state_file = data_dir / "state_time_machine.jsonl"
    diff_file = data_dir / "diff_time_machine.jsonl"

    states = _read_jsonl(state_file, max_lines=1)
    latest_state = states[0] if states else {}

    all_diffs = _read_jsonl(diff_file)
    error_diffs = [r for r in all_diffs if r.get("entity_type") == "error"]
    recent_errors = error_diffs[-20:]

    # Build queue status
    messages = [r for r in all_diffs if r.get("entity_type") == "agent_message"]
    queues = {}
    for msg in messages:
        data = msg.get("data", {})
        tgt = data.get("target_agent", "unknown")
        mtype = data.get("message_type", "unknown")
        if tgt not in queues:
            queues[tgt] = {"pending": 0}
        if mtype == "task":
            queues[tgt]["pending"] += 1
        elif mtype == "result":
            queues[tgt]["pending"] = max(0, queues[tgt]["pending"] - 1)

    issues = []
    critical = [r for r in recent_errors if r.get("data", {}).get("severity") == "critical"]
    if critical:
        issues.append(f"{len(critical)} critical error(s)")
    for agent, stats in queues.items():
        if stats["pending"] > 5:
            issues.append(f"Agent '{agent}' has {stats['pending']} pending tasks")
    agent_errors = {}
    for r in recent_errors:
        a = r.get("data", {}).get("agent_name", "?")
        agent_errors[a] = agent_errors.get(a, 0) + 1
    for a, c in agent_errors.items():
        if c >= 5:
            issues.append(f"Agent '{a}' has {c} recent errors")

    if args.format == "json":
        print(json.dumps({
            "state": latest_state,
            "recent_errors": len(recent_errors),
            "issues": issues or ["No issues detected"]
        }, indent=2, default=str))
    else:
        print(f"\n{Colors.BOLD}Diagnostic Report{Colors.END}")
        print(f"{'='*60}")
        if latest_state:
            print(f"  Last snapshot: {latest_state.get('timestamp', '?')[:19]}")
            print(f"  Total errors:  {latest_state.get('total_errors_recorded', '?')}")
            print(f"  Total msgs:    {latest_state.get('total_messages_sent', '?')}")
        else:
            print(f"  {Colors.YELLOW}No state snapshots yet{Colors.END}")
        print(f"\n  Recent errors: {len(recent_errors)}")
        if issues:
            print(f"\n  {Colors.RED}Issues:{Colors.END}")
            for issue in issues:
                print(f"    ! {issue}")
        else:
            print(f"\n  {Colors.GREEN}No issues detected{Colors.END}")
        if recent_errors:
            print(f"\n  {Colors.BOLD}Last 5 errors:{Colors.END}")
            for r in recent_errors[-5:]:
                d = r.get("data", {})
                print(f"    {Colors.GRAY}{r.get('timestamp', '')[:19]}{Colors.END} "
                      f"[{d.get('severity', '?')}] {d.get('agent_name', '?')}: "
                      f"{d.get('error_message', '')[:60]}")


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="Oracle Monitor - Multi-Agent System State for Claude Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  oracle-monitor state                    # Get current system state
  oracle-monitor agents                   # List agents and status
  oracle-monitor workload                 # Show Kubernetes workloads
  oracle-monitor llm                      # Show LLM model usage
  oracle-monitor watch                    # Real-time monitoring
  oracle-monitor debug                    # Interactive debug session
  oracle-monitor state --format json      # Output as JSON
  
  # Time-Machine Commands (reads from JSONL files):
  oracle-monitor errors                   # Recent errors
  oracle-monitor errors -s critical       # Only critical errors
  oracle-monitor diffs                    # All recent diffs
  oracle-monitor replay -m 10            # Replay last 10 minutes
  oracle-monitor queues                   # Inter-agent queue status
  oracle-monitor diagnose                 # Full diagnostic report
"""
    )
    
    parser.add_argument("--namespace", "-n", default="telemetry", help="Kubernetes namespace")
    parser.add_argument("--api-url", default="http://localhost:8080", help="Telemetry API URL")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama URL")
    parser.add_argument("--format", "-f", choices=["text", "json", "summary"], default="text", help="Output format")
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # State command
    state_parser = subparsers.add_parser("state", help="Get current system state")
    state_parser.set_defaults(func=cmd_state)
    
    # Agents command
    agents_parser = subparsers.add_parser("agents", help="List agents and their status")
    agents_parser.set_defaults(func=cmd_agents)
    
    # Workload command
    workload_parser = subparsers.add_parser("workload", help="Show Kubernetes workload status")
    workload_parser.set_defaults(func=cmd_workload)
    
    # LLM command
    llm_parser = subparsers.add_parser("llm", help="Show LLM model status")
    llm_parser.set_defaults(func=cmd_llm)
    
    # Watch command
    watch_parser = subparsers.add_parser("watch", help="Watch system state in real-time")
    watch_parser.add_argument("--interval", "-i", type=float, default=5.0, help="Update interval in seconds")
    watch_parser.set_defaults(func=cmd_watch)
    
    # Debug command
    debug_parser = subparsers.add_parser("debug", help="Interactive debugging session")
    debug_parser.set_defaults(func=cmd_debug)
    
    # ── Time-Machine Commands ──────────────────────────────────
    
    # Errors
    errors_parser = subparsers.add_parser("errors", help="Search errors from time-machine")
    errors_parser.add_argument("--severity", "-s", choices=["critical", "error", "warning", "info"], help="Filter by severity")
    errors_parser.add_argument("--agent", "-a", help="Filter by agent name")
    errors_parser.add_argument("--after", help="After timestamp (ISO)")
    errors_parser.add_argument("--before", help="Before timestamp (ISO)")
    errors_parser.add_argument("--limit", "-l", type=int, default=30, help="Max results")
    errors_parser.set_defaults(func=cmd_errors)
    
    # Diffs
    diffs_parser = subparsers.add_parser("diffs", help="Query diffs from time-machine")
    diffs_parser.add_argument("--entity-type", "-e", help="Filter by entity type (error, agent_message, ...)")
    diffs_parser.add_argument("--after", help="After timestamp (ISO)")
    diffs_parser.add_argument("--before", help="Before timestamp (ISO)")
    diffs_parser.add_argument("--limit", "-l", type=int, default=50, help="Max results")
    diffs_parser.set_defaults(func=cmd_diffs)
    
    # Replay
    replay_parser = subparsers.add_parser("replay", help="Replay event timeline")
    replay_parser.add_argument("--minutes", "-m", type=int, default=5, help="Minutes to replay (default: 5)")
    replay_parser.add_argument("--after", help="After timestamp (ISO)")
    replay_parser.add_argument("--before", help="Before timestamp (ISO)")
    replay_parser.add_argument("--limit", "-l", type=int, default=100, help="Max events")
    replay_parser.set_defaults(func=cmd_replay)
    
    # Queues
    queues_parser = subparsers.add_parser("queues", help="Show inter-agent queue status")
    queues_parser.set_defaults(func=cmd_queues)
    
    # Diagnose
    diag_parser = subparsers.add_parser("diagnose", help="Full diagnostic from time-machine data")
    diag_parser.set_defaults(func=cmd_tm_diagnose)
    
    args = parser.parse_args()
    
    if not args.command:
        # Default to state command
        args.func = cmd_state
    
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
