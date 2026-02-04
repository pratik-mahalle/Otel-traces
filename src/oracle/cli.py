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
from datetime import datetime
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
        print(state.to_json(indent=2))
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
                print(state.to_json())
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
    
    args = parser.parse_args()
    
    if not args.command:
        # Default to state command
        args.func = cmd_state
    
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
