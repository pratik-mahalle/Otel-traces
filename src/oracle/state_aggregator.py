"""
Oracle Monitor State Aggregator
Collects state from Kubernetes, Kafka, and LiteLLM to build unified system state
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
import subprocess

try:
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    HAS_K8S = True
except ImportError:
    HAS_K8S = False

from .state_schema import (
    OracleMonitorState, AgentState, AgentActivity, ActiveTask,
    WorkloadState, WorkloadLiveState, PodMetrics, PodStatus,
    QueueState, QueueTask, TaskPriority, PriorityLevel, TaskStatus,
    LiteLLMModel, PaymentType
)

logger = logging.getLogger(__name__)


class KubernetesCollector:
    """Collects workload state from Kubernetes"""
    
    def __init__(self, namespace: str = "telemetry"):
        self.namespace = namespace
        self._initialized = False
        
        if HAS_K8S:
            try:
                # Try in-cluster config first
                config.load_incluster_config()
                self._initialized = True
            except config.ConfigException:
                try:
                    # Fall back to kubeconfig
                    config.load_kube_config()
                    self._initialized = True
                except Exception as e:
                    logger.warning(f"Could not load Kubernetes config: {e}")
        else:
            logger.warning("kubernetes package not installed")
    
    async def get_workloads(self) -> List[WorkloadState]:
        """Get workload state for all deployments"""
        if not self._initialized:
            return self._get_mock_workloads()
        
        try:
            apps_v1 = client.AppsV1Api()
            core_v1 = client.CoreV1Api()
            
            workloads = []
            
            # Get deployments
            deployments = apps_v1.list_namespaced_deployment(self.namespace)
            
            for deployment in deployments.items:
                name = deployment.metadata.name
                
                # Get pods for this deployment
                label_selector = ",".join([
                    f"{k}={v}" 
                    for k, v in (deployment.spec.selector.match_labels or {}).items()
                ])
                
                pods = core_v1.list_namespaced_pod(
                    self.namespace, 
                    label_selector=label_selector
                )
                
                pod_metrics = []
                for pod in pods.items:
                    status = PodStatus.UNKNOWN
                    if pod.status.phase:
                        try:
                            status = PodStatus(pod.status.phase)
                        except ValueError:
                            status = PodStatus.UNKNOWN
                    
                    pod_metrics.append(PodMetrics(
                        pod_id=pod.metadata.name,
                        cpu=0,  # Would need metrics-server for real data
                        memory=0,
                        status=status,
                        updated_at=datetime.utcnow()
                    ))
                
                workloads.append(WorkloadState(
                    deployment_name=name,
                    max_pods=deployment.spec.replicas or 1,
                    live=WorkloadLiveState(
                        active_pods=len([p for p in pod_metrics if p.status == PodStatus.RUNNING]),
                        updated_at=datetime.utcnow(),
                        image=deployment.spec.template.spec.containers[0].image if deployment.spec.template.spec.containers else None
                    ),
                    pods=pod_metrics,
                    pod_max_ram=self._get_resource_limit(deployment, "memory"),
                    pod_max_cpu=self._get_resource_limit(deployment, "cpu")
                ))
            
            return workloads
            
        except Exception as e:
            logger.error(f"Error getting Kubernetes workloads: {e}")
            return self._get_mock_workloads()
    
    def _get_resource_limit(self, deployment, resource: str) -> Optional[str]:
        """Extract resource limit from deployment"""
        try:
            containers = deployment.spec.template.spec.containers
            if containers and containers[0].resources and containers[0].resources.limits:
                return containers[0].resources.limits.get(resource)
        except Exception:
            pass
        return None
    
    def _get_mock_workloads(self) -> List[WorkloadState]:
        """Return mock workload data for testing"""
        return [
            WorkloadState(
                deployment_name="telemetry-api",
                max_pods=3,
                live=WorkloadLiveState(active_pods=1, updated_at=datetime.utcnow()),
                pods=[
                    PodMetrics(
                        pod_id="telemetry-api-abc123",
                        cpu=150,
                        memory=256,
                        status=PodStatus.RUNNING
                    )
                ],
                pod_max_ram="512Mi",
                pod_max_cpu="500m"
            ),
            WorkloadState(
                deployment_name="ollama",
                max_pods=1,
                live=WorkloadLiveState(active_pods=1, updated_at=datetime.utcnow()),
                pods=[
                    PodMetrics(
                        pod_id="ollama-def456",
                        cpu=800,
                        memory=2048,
                        status=PodStatus.RUNNING
                    )
                ],
                pod_max_ram="4Gi",
                pod_max_cpu="2"
            ),
            WorkloadState(
                deployment_name="kafka",
                max_pods=1,
                live=WorkloadLiveState(active_pods=1, updated_at=datetime.utcnow()),
                pods=[
                    PodMetrics(
                        pod_id="kafka-0",
                        cpu=200,
                        memory=512,
                        status=PodStatus.RUNNING
                    )
                ],
                pod_max_ram="1Gi",
                pod_max_cpu="1"
            )
        ]


class TelemetryStoreCollector:
    """Collects agent and queue state from the telemetry store"""
    
    def __init__(self, api_url: str = "http://localhost:8080"):
        self.api_url = api_url
    
    async def get_agents(self) -> List[AgentState]:
        """Get agent state from telemetry data"""
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                # Get recent traces to infer agent state
                response = await client.get(f"{self.api_url}/api/v1/traces?limit=50")
                traces = response.json().get("traces", [])
                
                # Aggregate agent info from traces
                agents_map: Dict[str, AgentState] = {}
                
                for trace in traces:
                    # Get spans for this trace
                    span_response = await client.get(
                        f"{self.api_url}/api/v1/traces/{trace['trace_id']}/spans"
                    )
                    spans = span_response.json().get("spans", [])
                    
                    for span in spans:
                        agent_name = span.get("agent_name")
                        if not agent_name or ":" in agent_name:  # Skip sub-spans
                            continue
                        
                        if agent_name not in agents_map:
                            agents_map[agent_name] = AgentState(
                                name=agent_name,
                                deployment_name=f"agent-{agent_name.lower()}",
                                models=[span.get("model_name", "ollama/llama2")],
                                activity=AgentActivity(),
                                max_parallel_invocations=1
                            )
                        
                        # Track active tasks
                        if span.get("status") == "running":
                            agents_map[agent_name].activity.active_task_ids.append(
                                ActiveTask(
                                    id=span.get("span_id", ""),
                                    started_on=datetime.fromisoformat(span.get("start_time", datetime.utcnow().isoformat()).replace("Z", "+00:00")),
                                    status=TaskStatus.RUNNING
                                )
                            )
                
                return list(agents_map.values())
                
        except Exception as e:
            logger.warning(f"Error fetching from telemetry API: {e}")
            return self._get_mock_agents()
    
    def _get_mock_agents(self) -> List[AgentState]:
        """Return mock agent data for testing"""
        return [
            AgentState(
                name="Orchestrator",
                description="Coordinates multi-agent tasks",
                deployment_name="agent-orchestrator",
                models=["ollama/llama2"],
                activity=AgentActivity(
                    active_task_ids=[
                        ActiveTask(
                            id="task-001",
                            started_on=datetime.utcnow() - timedelta(seconds=30),
                            status=TaskStatus.RUNNING
                        )
                    ]
                ),
                max_parallel_invocations=5
            ),
            AgentState(
                name="Researcher",
                description="Researches and gathers information",
                deployment_name="agent-researcher",
                models=["ollama/llama2"],
                activity=AgentActivity(active_task_ids=[]),
                max_parallel_invocations=3
            ),
            AgentState(
                name="Writer",
                description="Writes and edits content",
                deployment_name="agent-writer",
                models=["ollama/llama2"],
                activity=AgentActivity(active_task_ids=[]),
                max_parallel_invocations=3
            ),
            AgentState(
                name="Coder",
                description="Writes and analyzes code",
                deployment_name="agent-coder",
                models=["ollama/llama2"],
                activity=AgentActivity(active_task_ids=[]),
                max_parallel_invocations=2
            )
        ]
    
    async def get_queues(self) -> List[QueueState]:
        """Get queue state (simulated from Kafka topics)"""
        # In production, this would query Kafka consumer lag
        return [
            QueueState(
                name="agent-telemetry-events",
                tasks=[],
                updated_at=datetime.utcnow()
            ),
            QueueState(
                name="agent-telemetry-spans",
                tasks=[],
                updated_at=datetime.utcnow()
            ),
            QueueState(
                name="agent-task-queue",
                tasks=[
                    QueueTask(
                        id="queue-task-001",
                        priority=TaskPriority(level=PriorityLevel.NORMAL),
                        submitted_at=datetime.utcnow() - timedelta(seconds=10),
                        invoked_by="Orchestrator",
                        prompt="Process user request"
                    )
                ],
                updated_at=datetime.utcnow()
            )
        ]


class LiteLLMCollector:
    """Collects LLM model state and usage"""
    
    def __init__(self, ollama_url: str = "http://localhost:11434"):
        self.ollama_url = ollama_url
    
    async def get_models(self) -> List[LiteLLMModel]:
        """Get LLM model configurations and current usage"""
        models = []
        
        # Try to get Ollama models
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.ollama_url}/api/tags")
                ollama_models = response.json().get("models", [])
                
                for model in ollama_models:
                    models.append(LiteLLMModel(
                        model=f"ollama/{model['name']}",
                        provider="ollama",
                        tpm=0,
                        rpm=0,
                        tpm_max=100000,  # No real limit for Ollama
                        rpm_max=1000,
                        payment_type=PaymentType.FREE,
                        input_types=["text"],
                        output_types=["text"]
                    ))
        except Exception as e:
            logger.debug(f"Could not fetch Ollama models: {e}")
        
        # Add default models if none found
        if not models:
            models = [
                LiteLLMModel(
                    model="ollama/llama2",
                    provider="ollama",
                    tpm=0,
                    rpm=0,
                    tpm_max=100000,
                    rpm_max=1000,
                    payment_type=PaymentType.FREE,
                    input_context=4096,
                    output_context=4096,
                    input_types=["text"],
                    output_types=["text"]
                ),
                LiteLLMModel(
                    model="ollama/mistral",
                    provider="ollama",
                    tpm=0,
                    rpm=0,
                    tpm_max=100000,
                    rpm_max=1000,
                    payment_type=PaymentType.FREE,
                    input_context=8192,
                    output_context=8192,
                    input_types=["text"],
                    output_types=["text"]
                )
            ]
        
        return models


class OracleMonitorAggregator:
    """
    Main aggregator that collects state from all sources
    and produces the unified Oracle Monitor state.
    """
    
    def __init__(
        self,
        namespace: str = "telemetry",
        api_url: str = "http://localhost:8080",
        ollama_url: str = "http://localhost:11434"
    ):
        self.namespace = namespace
        self.k8s_collector = KubernetesCollector(namespace)
        self.telemetry_collector = TelemetryStoreCollector(api_url)
        self.litellm_collector = LiteLLMCollector(ollama_url)
        
        self._previous_state: Optional[OracleMonitorState] = None
    
    async def get_state(self) -> OracleMonitorState:
        """
        Collect and aggregate system state from all sources.
        Returns the unified Oracle Monitor state.
        """
        # Collect state from all sources concurrently
        agents, workloads, queues, models = await asyncio.gather(
            self.telemetry_collector.get_agents(),
            self.k8s_collector.get_workloads(),
            self.telemetry_collector.get_queues(),
            self.litellm_collector.get_models(),
            return_exceptions=True
        )
        
        # Handle any exceptions
        if isinstance(agents, Exception):
            logger.error(f"Error collecting agents: {agents}")
            agents = []
        if isinstance(workloads, Exception):
            logger.error(f"Error collecting workloads: {workloads}")
            workloads = []
        if isinstance(queues, Exception):
            logger.error(f"Error collecting queues: {queues}")
            queues = []
        if isinstance(models, Exception):
            logger.error(f"Error collecting models: {models}")
            models = []
        
        state = OracleMonitorState(
            agents=agents,
            workload=workloads,
            queues=queues,
            litellm=models,
            namespace=self.namespace
        )
        
        self._previous_state = state
        return state
    
    async def get_diff_log(self) -> str:
        """Get the state as a diff log for Claude Code"""
        state = await self.get_state()
        return state.to_diff_log(self._previous_state)
    
    async def watch(self, interval_seconds: float = 5.0):
        """
        Watch system state and yield updates.
        Use this for continuous monitoring.
        """
        while True:
            try:
                state = await self.get_state()
                yield state
            except Exception as e:
                logger.error(f"Error in watch loop: {e}")
            
            await asyncio.sleep(interval_seconds)


# CLI interface
async def main():
    """CLI entry point for Oracle Monitor"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Oracle Monitor - Multi-Agent System State")
    parser.add_argument("--namespace", "-n", default="telemetry", help="Kubernetes namespace")
    parser.add_argument("--api-url", default="http://localhost:8080", help="Telemetry API URL")
    parser.add_argument("--ollama-url", default="http://localhost:11434", help="Ollama URL")
    parser.add_argument("--watch", "-w", action="store_true", help="Watch mode (continuous updates)")
    parser.add_argument("--format", "-f", choices=["json", "log", "summary"], default="log", help="Output format")
    parser.add_argument("--interval", "-i", type=float, default=5.0, help="Watch interval in seconds")
    
    args = parser.parse_args()
    
    aggregator = OracleMonitorAggregator(
        namespace=args.namespace,
        api_url=args.api_url,
        ollama_url=args.ollama_url
    )
    
    if args.watch:
        async for state in aggregator.watch(args.interval):
            if args.format == "json":
                print(state.to_json())
            elif args.format == "summary":
                print(json.dumps(state.get_summary(), indent=2))
            else:
                print("\033[2J\033[H")  # Clear screen
                print(state.to_diff_log())
    else:
        state = await aggregator.get_state()
        if args.format == "json":
            print(state.to_json())
        elif args.format == "summary":
            print(json.dumps(state.get_summary(), indent=2))
        else:
            print(state.to_diff_log())


if __name__ == "__main__":
    asyncio.run(main())
