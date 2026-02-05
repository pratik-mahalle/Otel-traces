"""
Oracle Monitor State Aggregator
Collects state from Kubernetes, Kafka, and LiteLLM to build unified system state
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
import subprocess


class DiscoveryMode(str, Enum):
    """Agent discovery modes"""
    LABELED = "labeled"      # Only agents with oracle-monitor/agent=true label
    ALL = "all"              # All deployments in namespace
    TELEMETRY = "telemetry"  # Auto-discover from telemetry data (spans/traces)
    AUTO = "auto"            # Combine: labeled K8s + telemetry-derived

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


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var with a default."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class KubernetesCollector:
    """Collects workload state from Kubernetes"""

    def __init__(
        self,
        namespace: str = "telemetry",
        allow_mocks: bool = True,
        discovery_mode: DiscoveryMode = DiscoveryMode.AUTO
    ):
        self.namespace = namespace
        self.allow_mocks = allow_mocks
        self.discovery_mode = discovery_mode
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
            if self.allow_mocks:
                return self._get_mock_workloads()
            logger.warning("Kubernetes not initialized; returning empty workload list (mocks disabled)")
            return []
        
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
            if self.allow_mocks:
                return self._get_mock_workloads()
            return []
    
    def _get_resource_limit(self, deployment, resource: str) -> Optional[str]:
        """Extract resource limit from deployment"""
        try:
            containers = deployment.spec.template.spec.containers
            if containers and containers[0].resources and containers[0].resources.limits:
                return containers[0].resources.limits.get(resource)
        except Exception:
            pass
        return None
    
    def get_discovered_agents(self) -> List[AgentState]:
        """
        Discover agents from Kubernetes deployments.

        Discovery modes:
        - LABELED: Only deployments with oracle-monitor/agent=true label
        - ALL: All deployments in the namespace
        - TELEMETRY: Skip K8s discovery (rely on telemetry data only)
        - AUTO: Same as LABELED (telemetry agents merged separately)
        """
        if not self._initialized:
            return []

        # TELEMETRY mode skips K8s discovery entirely
        if self.discovery_mode == DiscoveryMode.TELEMETRY:
            return []

        try:
            apps_v1 = client.AppsV1Api()

            # Determine which deployments to fetch
            if self.discovery_mode == DiscoveryMode.ALL:
                # Get ALL deployments in namespace
                deployments = apps_v1.list_namespaced_deployment(self.namespace)
                logger.info(f"Discovery mode ALL: found {len(deployments.items)} deployments")
            else:
                # LABELED or AUTO: only labeled deployments
                deployments = apps_v1.list_namespaced_deployment(
                    self.namespace,
                    label_selector="oracle-monitor/agent=true"
                )
                logger.info(f"Discovery mode {self.discovery_mode.value}: found {len(deployments.items)} labeled agents")

            discovered_agents = []
            for d in deployments.items:
                labels = d.metadata.labels or {}
                annotations = d.metadata.annotations or {}

                # Check if this deployment is explicitly excluded
                if annotations.get("oracle-monitor/exclude") == "true":
                    continue

                # Get agent metadata from labels/annotations (if present)
                name = (
                    labels.get("oracle-monitor/name")
                    or annotations.get("oracle-monitor/name")
                    or d.metadata.name
                )
                desc = (
                    annotations.get("oracle-monitor/description")
                    or labels.get("oracle-monitor/description")
                    or self._infer_description(d.metadata.name, labels)
                )
                model_list = (
                    annotations.get("oracle-monitor/models")
                    or labels.get("oracle-monitor/models")
                )
                if model_list:
                    models = [m.strip() for m in model_list.split(",") if m.strip()]
                else:
                    # Try to infer model from image or default to unknown
                    models = self._infer_models(d) or ["unknown"]

                discovered_agents.append(AgentState(
                    name=name,
                    description=desc,
                    deployment_name=d.metadata.name,
                    models=models,
                    activity=AgentActivity(active_task_ids=[], updated_at=datetime.utcnow()),
                    max_parallel_invocations=int(d.spec.replicas or 1)
                ))

            return discovered_agents

        except Exception as e:
            logger.warning(f"Error discovering agents from K8s: {e}")
            return []

    def _infer_description(self, name: str, labels: Dict[str, str]) -> str:
        """Infer agent description from deployment name or labels"""
        # Check common labels
        if labels.get("app.kubernetes.io/component"):
            return f"{labels['app.kubernetes.io/component']} service"
        if labels.get("app"):
            return f"{labels['app']} agent"

        # Infer from name
        name_lower = name.lower()
        if "research" in name_lower:
            return "Research and information gathering agent"
        elif "write" in name_lower:
            return "Content writing and editing agent"
        elif "code" in name_lower or "dev" in name_lower:
            return "Code analysis and development agent"
        elif "orchestrat" in name_lower:
            return "Multi-agent orchestration coordinator"
        elif "api" in name_lower:
            return "API service"
        elif "kafka" in name_lower:
            return "Message broker service"
        elif "ollama" in name_lower:
            return "Local LLM inference service"

        return f"Discovered deployment: {name}"

    def _infer_models(self, deployment) -> List[str]:
        """Try to infer LLM models from deployment configuration"""
        models = []
        try:
            containers = deployment.spec.template.spec.containers or []
            for container in containers:
                # Check environment variables for model hints
                if container.env:
                    for env_var in container.env:
                        if env_var.name and "MODEL" in env_var.name.upper() and env_var.value:
                            models.append(env_var.value)

                # Check image name for common LLM services
                image = container.image or ""
                if "ollama" in image.lower():
                    models.append("ollama/llama2")
                elif "litellm" in image.lower():
                    models.append("litellm/proxy")
        except Exception as exc:
            # Best-effort inference: log and return any models collected so far
            logger.warning("Failed to infer models from deployment %r: %s", getattr(deployment, "metadata", None), exc)

        # Deduplicate while preserving insertion order
        return list(dict.fromkeys(models))

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
    
    def __init__(self, api_url: str = "http://localhost:8080", allow_mocks: bool = True):
        self.api_url = api_url
        self.allow_mocks = allow_mocks
    
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
            if self.allow_mocks:
                return self._get_mock_agents()
            return []
    
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
        if not self.allow_mocks:
            return []
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
    
    def __init__(self, ollama_url: str = "http://localhost:11434", allow_mocks: bool = True):
        self.ollama_url = ollama_url
        self.allow_mocks = allow_mocks
    
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
            if self.allow_mocks:
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


def _get_discovery_mode() -> DiscoveryMode:
    """Get discovery mode from environment variable"""
    mode_str = os.getenv("ORACLE_DISCOVERY_MODE", "auto").lower().strip()
    try:
        return DiscoveryMode(mode_str)
    except ValueError:
        logger.warning(f"Invalid ORACLE_DISCOVERY_MODE '{mode_str}', defaulting to 'auto'")
        return DiscoveryMode.AUTO


class OracleMonitorAggregator:
    """
    Main aggregator that collects state from all sources
    and produces the unified Oracle Monitor state.

    Discovery Modes (set via ORACLE_DISCOVERY_MODE env var):
    - labeled: Only K8s deployments with oracle-monitor/agent=true label
    - all: ALL deployments in the namespace (useful for debugging)
    - telemetry: Auto-discover from telemetry data only (no K8s labels needed)
    - auto: Combine labeled K8s agents + telemetry-derived agents (default)
    """

    def __init__(
        self,
        namespace: str = "telemetry",
        api_url: str = "http://localhost:8080",
        ollama_url: str = "http://localhost:11434",
        allow_mocks: Optional[bool] = None,
        discovery_mode: Optional[DiscoveryMode] = None
    ):
        self.namespace = namespace
        if allow_mocks is None:
            env = (os.getenv("ORACLE_ENV") or os.getenv("ENVIRONMENT") or os.getenv("ENV") or "development").lower()
            default_allow_mocks = env not in {"production", "prod"}
            allow_mocks = _env_bool("ORACLE_ALLOW_MOCKS", default_allow_mocks)
        self.allow_mocks = allow_mocks

        # Set discovery mode from param or environment
        if discovery_mode is None:
            discovery_mode = _get_discovery_mode()
        self.discovery_mode = discovery_mode
        logger.info(f"Oracle Monitor using discovery mode: {self.discovery_mode.value}")

        self.k8s_collector = KubernetesCollector(
            namespace,
            allow_mocks=self.allow_mocks,
            discovery_mode=self.discovery_mode
        )
        self.telemetry_collector = TelemetryStoreCollector(api_url, allow_mocks=self.allow_mocks)
        self.litellm_collector = LiteLLMCollector(ollama_url, allow_mocks=self.allow_mocks)
        
        self._previous_state: Optional[OracleMonitorState] = None
    
    def _merge_agents(
        self,
        telemetry_agents: List[AgentState],
        discovered_agents: List[AgentState]
    ) -> List[AgentState]:
        """Merge telemetry-derived agent activity with k8s-discovered agents."""
        telemetry_by_name = {a.name.lower(): a for a in telemetry_agents if a.name}
        telemetry_by_deploy = {
            a.deployment_name.lower(): a
            for a in telemetry_agents
            if a.deployment_name
        }
        merged: List[AgentState] = []
        matched_names: set[str] = set()
        matched_deploys: set[str] = set()
        
        for k8s_agent in discovered_agents:
            name_key = (k8s_agent.name or "").lower()
            deploy_key = (k8s_agent.deployment_name or "").lower()
            match = telemetry_by_deploy.get(deploy_key) or telemetry_by_name.get(name_key)
            if match:
                merged.append(AgentState(
                    name=k8s_agent.name or match.name,
                    description=k8s_agent.description or match.description,
                    deployment_name=k8s_agent.deployment_name or match.deployment_name,
                    models=k8s_agent.models or match.models,
                    activity=match.activity or k8s_agent.activity,
                    max_parallel_invocations=k8s_agent.max_parallel_invocations
                ))
                if name_key:
                    matched_names.add(name_key)
                if deploy_key:
                    matched_deploys.add(deploy_key)
            else:
                merged.append(k8s_agent)
        
        for agent in telemetry_agents:
            name_key = (agent.name or "").lower()
            deploy_key = (agent.deployment_name or "").lower()
            if name_key in matched_names or deploy_key in matched_deploys:
                continue
            merged.append(agent)
        
        return merged
    
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
        discovered_agents = self.k8s_collector.get_discovered_agents()
        
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
        if isinstance(discovered_agents, Exception):
            logger.error(f"Error collecting discovered agents: {discovered_agents}")
            discovered_agents = []
        
        agents = self._merge_agents(agents, discovered_agents)
        
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
