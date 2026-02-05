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
    """
    Collects workload state from Kubernetes.
    Auto-detects AI agent deployments by naming patterns and labels.
    """

    # Patterns to identify AI agent deployments
    AGENT_NAME_PATTERNS = [
        "worker-",      # worker-summarizer, worker-analyzer, etc.
        "agent-",       # agent-orchestrator, agent-coder, etc.
        "map-",         # map-worker, map-aggregator, etc.
        "-agent",       # coding-agent, research-agent, etc.
        "summarizer",   # standalone summarizer
        "analyzer",     # standalone analyzer
        "reviewer",     # standalone reviewer
        "orchestrator", # orchestrator
        "coder",        # coder agent
        "researcher",   # researcher agent
    ]

    # Labels that indicate an AI agent deployment
    AGENT_LABELS = [
        "app.kubernetes.io/component=agent",
        "agent-type",
        "ai.agent/enabled=true",
        "role=agent",
    ]

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
    
    def _is_agent_deployment(self, deployment) -> bool:
        """Check if a deployment is an AI agent based on name patterns or labels"""
        name = deployment.metadata.name.lower()

        # Check name patterns
        for pattern in self.AGENT_NAME_PATTERNS:
            if pattern in name:
                return True

        # Check labels
        labels = deployment.metadata.labels or {}
        for label_check in self.AGENT_LABELS:
            if "=" in label_check:
                key, value = label_check.split("=", 1)
                if labels.get(key) == value:
                    return True
            else:
                if label_check in labels:
                    return True

        return False

    def _extract_agent_name(self, deployment_name: str) -> str:
        """Extract a human-readable agent name from deployment name"""
        name = deployment_name.lower()

        # Remove common prefixes
        for prefix in ["worker-", "agent-", "map-"]:
            if name.startswith(prefix):
                name = name[len(prefix):]
                break

        # Capitalize words
        return name.replace("-", " ").title().replace(" ", "")

    def _extract_model_from_env(self, deployment) -> List[str]:
        """Extract LLM model names from deployment environment variables"""
        models = []
        try:
            containers = deployment.spec.template.spec.containers or []
            for container in containers:
                env_vars = container.env or []
                for env in env_vars:
                    if env.name in ["MODEL", "LLM_MODEL", "OPENAI_MODEL", "ANTHROPIC_MODEL", "MODEL_NAME"]:
                        if env.value:
                            models.append(env.value)
        except Exception:
            pass

        return models if models else ["ollama/llama2"]  # Default model

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

                    # Try to get real metrics from metrics-server
                    cpu_usage = 0
                    memory_usage = 0
                    try:
                        # This requires metrics-server to be installed
                        metrics_api = client.CustomObjectsApi()
                        pod_metrics_data = metrics_api.get_namespaced_custom_object(
                            "metrics.k8s.io", "v1beta1", self.namespace,
                            "pods", pod.metadata.name
                        )
                        containers = pod_metrics_data.get("containers", [])
                        for c in containers:
                            cpu_str = c.get("usage", {}).get("cpu", "0")
                            mem_str = c.get("usage", {}).get("memory", "0")
                            # Parse CPU (e.g., "100m" -> 100 millicores)
                            if cpu_str.endswith("n"):
                                cpu_usage += int(cpu_str[:-1]) / 1000000
                            elif cpu_str.endswith("m"):
                                cpu_usage += int(cpu_str[:-1])
                            # Parse memory (e.g., "128Mi" -> 128 MB)
                            if mem_str.endswith("Ki"):
                                memory_usage += int(mem_str[:-2]) / 1024
                            elif mem_str.endswith("Mi"):
                                memory_usage += int(mem_str[:-2])
                            elif mem_str.endswith("Gi"):
                                memory_usage += int(mem_str[:-2]) * 1024
                    except Exception:
                        pass  # Metrics server not available

                    pod_metrics.append(PodMetrics(
                        pod_id=pod.metadata.name,
                        cpu=cpu_usage,
                        memory=memory_usage,
                        status=status,
                        updated_at=datetime.utcnow()
                    ))

                # Get image with tag
                image = None
                if deployment.spec.template.spec.containers:
                    image = deployment.spec.template.spec.containers[0].image
                    # Extract just the tag if present
                    if ":" in image:
                        image = f"{name}:{image.split(':')[-1]}"
                    else:
                        image = f"{name}:latest"

                workloads.append(WorkloadState(
                    deployment_name=name,
                    max_pods=deployment.spec.replicas or 1,
                    live=WorkloadLiveState(
                        active_pods=len([p for p in pod_metrics if p.status == PodStatus.RUNNING]),
                        updated_at=datetime.utcnow(),
                        image=image,
                        rolled_out_at=deployment.metadata.creation_timestamp
                    ),
                    pods=pod_metrics,
                    pod_max_ram=self._get_resource_limit(deployment, "memory"),
                    pod_max_cpu=self._get_resource_limit(deployment, "cpu")
                ))

            return workloads

        except Exception as e:
            logger.error(f"Error getting Kubernetes workloads: {e}")
            return self._get_mock_workloads()

    async def discover_agents(self) -> List[AgentState]:
        """
        Discover AI agents from Kubernetes deployments.
        Returns AgentState for each detected agent deployment.
        """
        if not self._initialized:
            return self._get_mock_agents()

        try:
            apps_v1 = client.AppsV1Api()
            core_v1 = client.CoreV1Api()

            agents = []
            deployments = apps_v1.list_namespaced_deployment(self.namespace)

            for deployment in deployments.items:
                if not self._is_agent_deployment(deployment):
                    continue

                name = deployment.metadata.name
                agent_name = self._extract_agent_name(name)
                models = self._extract_model_from_env(deployment)

                # Get description from annotations
                annotations = deployment.metadata.annotations or {}
                description = annotations.get(
                    "ai.agent/description",
                    annotations.get("description", f"AI agent: {agent_name}")
                )

                # Get max parallel from annotations or replicas
                max_parallel = int(annotations.get(
                    "ai.agent/max-parallel",
                    deployment.spec.replicas or 1
                ))

                # Get running pods to determine active tasks
                label_selector = ",".join([
                    f"{k}={v}"
                    for k, v in (deployment.spec.selector.match_labels or {}).items()
                ])

                pods = core_v1.list_namespaced_pod(
                    self.namespace,
                    label_selector=label_selector
                )

                # Create active task entries for running pods
                active_tasks = []
                for pod in pods.items:
                    if pod.status.phase == "Running":
                        # Check if pod has task annotations
                        pod_annotations = pod.metadata.annotations or {}
                        task_id = pod_annotations.get("ai.agent/task-id", pod.metadata.name)

                        active_tasks.append(ActiveTask(
                            id=task_id,
                            started_on=pod.status.start_time or datetime.utcnow(),
                            status=TaskStatus.RUNNING
                        ))

                agents.append(AgentState(
                    name=agent_name,
                    description=description,
                    deployment_name=name,
                    models=models,
                    activity=AgentActivity(
                        active_task_ids=active_tasks,
                        updated_at=datetime.utcnow()
                    ),
                    max_parallel_invocations=max_parallel
                ))

            return agents

        except Exception as e:
            logger.error(f"Error discovering agents from K8s: {e}")
            return self._get_mock_agents()

    def _get_mock_agents(self) -> List[AgentState]:
        """Return mock agent data matching the screenshot examples"""
        return [
            AgentState(
                name="Summarizer",
                description="Summarizes documents and text",
                deployment_name="worker-summarizer",
                models=["ollama/llama2"],
                activity=AgentActivity(
                    active_task_ids=[
                        ActiveTask(
                            id="task-sum-001",
                            started_on=datetime.utcnow() - timedelta(seconds=15),
                            status=TaskStatus.RUNNING
                        )
                    ]
                ),
                max_parallel_invocations=3
            ),
            AgentState(
                name="Analyzer",
                description="Analyzes data and provides insights",
                deployment_name="worker-analyzer",
                models=["ollama/mistral"],
                activity=AgentActivity(active_task_ids=[]),
                max_parallel_invocations=2
            ),
            AgentState(
                name="Reviewer",
                description="Reviews and validates outputs",
                deployment_name="worker-reviewer",
                models=["ollama/llama2"],
                activity=AgentActivity(active_task_ids=[]),
                max_parallel_invocations=2
            ),
            AgentState(
                name="Descheduler",
                description="Manages task scheduling and distribution",
                deployment_name="worker-descheduler",
                models=["ollama/llama2"],
                activity=AgentActivity(active_task_ids=[]),
                max_parallel_invocations=1
            ),
            AgentState(
                name="MapWorker",
                description="Processes map operations",
                deployment_name="map-worker",
                models=["ollama/codellama"],
                activity=AgentActivity(
                    active_task_ids=[
                        ActiveTask(
                            id="task-map-001",
                            started_on=datetime.utcnow() - timedelta(seconds=45),
                            status=TaskStatus.RUNNING
                        )
                    ]
                ),
                max_parallel_invocations=5
            ),
            AgentState(
                name="MapAggregator",
                description="Aggregates map results",
                deployment_name="map-aggregator",
                models=["ollama/llama2"],
                activity=AgentActivity(active_task_ids=[]),
                max_parallel_invocations=1
            )
        ]
    
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
        """Return mock workload data matching the screenshot examples"""
        return [
            # LiteLLM Gateway
            WorkloadState(
                deployment_name="litellm",
                max_pods=3,
                live=WorkloadLiveState(
                    active_pods=3,
                    updated_at=datetime.utcnow(),
                    image="litellm:main-latest"
                ),
                pods=[
                    PodMetrics(pod_id="litellm-0", cpu=150, memory=256, status=PodStatus.RUNNING),
                    PodMetrics(pod_id="litellm-1", cpu=120, memory=240, status=PodStatus.RUNNING),
                    PodMetrics(pod_id="litellm-2", cpu=80, memory=200, status=PodStatus.RUNNING),
                ],
                pod_max_ram="512Mi",
                pod_max_cpu="500m"
            ),
            # Worker agents
            WorkloadState(
                deployment_name="worker-summarizer",
                max_pods=1,
                live=WorkloadLiveState(
                    active_pods=1,
                    updated_at=datetime.utcnow(),
                    image="worker-summarizer:v1.2.0"
                ),
                pods=[
                    PodMetrics(pod_id="worker-summarizer-abc123", cpu=200, memory=512, status=PodStatus.RUNNING)
                ],
                pod_max_ram="1Gi",
                pod_max_cpu="1"
            ),
            WorkloadState(
                deployment_name="worker-analyzer",
                max_pods=1,
                live=WorkloadLiveState(
                    active_pods=1,
                    updated_at=datetime.utcnow(),
                    image="worker-analyzer:v1.1.0"
                ),
                pods=[
                    PodMetrics(pod_id="worker-analyzer-def456", cpu=180, memory=480, status=PodStatus.RUNNING)
                ],
                pod_max_ram="1Gi",
                pod_max_cpu="1"
            ),
            WorkloadState(
                deployment_name="worker-reviewer",
                max_pods=1,
                live=WorkloadLiveState(
                    active_pods=1,
                    updated_at=datetime.utcnow(),
                    image="worker-reviewer:v1.0.5"
                ),
                pods=[
                    PodMetrics(pod_id="worker-reviewer-ghi789", cpu=150, memory=400, status=PodStatus.RUNNING)
                ],
                pod_max_ram="1Gi",
                pod_max_cpu="1"
            ),
            WorkloadState(
                deployment_name="worker-descheduler",
                max_pods=1,
                live=WorkloadLiveState(
                    active_pods=1,
                    updated_at=datetime.utcnow(),
                    image="worker-descheduler:v1.0.0"
                ),
                pods=[
                    PodMetrics(pod_id="worker-descheduler-jkl012", cpu=100, memory=256, status=PodStatus.RUNNING)
                ],
                pod_max_ram="512Mi",
                pod_max_cpu="500m"
            ),
            # Map workers
            WorkloadState(
                deployment_name="map-worker",
                max_pods=3,
                live=WorkloadLiveState(
                    active_pods=2,
                    updated_at=datetime.utcnow(),
                    image="map-worker:v2.0.0"
                ),
                pods=[
                    PodMetrics(pod_id="map-worker-0", cpu=300, memory=768, status=PodStatus.RUNNING),
                    PodMetrics(pod_id="map-worker-1", cpu=280, memory=720, status=PodStatus.RUNNING),
                    PodMetrics(pod_id="map-worker-2", cpu=0, memory=0, status=PodStatus.PENDING),
                ],
                pod_max_ram="2Gi",
                pod_max_cpu="2"
            ),
            WorkloadState(
                deployment_name="map-aggregator",
                max_pods=1,
                live=WorkloadLiveState(
                    active_pods=1,
                    updated_at=datetime.utcnow(),
                    image="map-aggregator:v2.0.0"
                ),
                pods=[
                    PodMetrics(pod_id="map-aggregator-mno345", cpu=250, memory=640, status=PodStatus.RUNNING)
                ],
                pod_max_ram="1Gi",
                pod_max_cpu="1"
            ),
            # Infrastructure
            WorkloadState(
                deployment_name="kafka",
                max_pods=1,
                live=WorkloadLiveState(
                    active_pods=1,
                    updated_at=datetime.utcnow(),
                    image="kafka:3.5.0"
                ),
                pods=[
                    PodMetrics(pod_id="kafka-0", cpu=200, memory=512, status=PodStatus.RUNNING)
                ],
                pod_max_ram="1Gi",
                pod_max_cpu="1"
            ),
            WorkloadState(
                deployment_name="ollama",
                max_pods=1,
                live=WorkloadLiveState(
                    active_pods=1,
                    updated_at=datetime.utcnow(),
                    image="ollama:latest"
                ),
                pods=[
                    PodMetrics(pod_id="ollama-0", cpu=800, memory=4096, status=PodStatus.RUNNING)
                ],
                pod_max_ram="8Gi",
                pod_max_cpu="4"
            )
        ]


class TelemetryStoreCollector:
    """Collects queue state and augments agent data from telemetry traces"""

    def __init__(self, api_url: str = "http://localhost:8080"):
        self.api_url = api_url

    async def augment_agents_with_telemetry(
        self,
        agents: List[AgentState]
    ) -> List[AgentState]:
        """
        Augment K8s-discovered agents with telemetry data.
        Updates activity status based on recent spans.
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as http_client:
                # Get recent traces
                response = await http_client.get(f"{self.api_url}/api/v1/traces?limit=50")
                traces = response.json().get("traces", [])

                # Map agent names (lowercase) to agents
                agent_map = {a.name.lower(): a for a in agents}

                for trace in traces:
                    # Get spans for this trace
                    try:
                        span_response = await http_client.get(
                            f"{self.api_url}/api/v1/traces/{trace['trace_id']}/spans"
                        )
                        spans = span_response.json().get("spans", [])
                    except Exception:
                        continue

                    for span in spans:
                        agent_name = span.get("agent_name", "").lower()
                        if not agent_name or agent_name not in agent_map:
                            continue

                        agent = agent_map[agent_name]

                        # Update model if found in span
                        model_name = span.get("model_name")
                        if model_name and model_name not in agent.models:
                            agent.models.append(model_name)

                        # Track active tasks from running spans
                        if span.get("status") == "running":
                            task_id = span.get("span_id", "")
                            # Avoid duplicates
                            existing_ids = {t.id for t in agent.activity.active_task_ids}
                            if task_id not in existing_ids:
                                try:
                                    started = datetime.fromisoformat(
                                        span.get("start_time", datetime.utcnow().isoformat())
                                        .replace("Z", "+00:00")
                                    )
                                except Exception:
                                    started = datetime.utcnow()

                                agent.activity.active_task_ids.append(ActiveTask(
                                    id=task_id,
                                    started_on=started,
                                    status=TaskStatus.RUNNING
                                ))

                        agent.activity.updated_at = datetime.utcnow()

        except Exception as e:
            logger.debug(f"Could not augment agents with telemetry: {e}")

        return agents
    
    async def get_queues(self) -> List[QueueState]:
        """
        Get queue state.
        In production, this would query Kafka consumer lag and task queues.
        """
        # Mock queue data matching the screenshot format
        return [
            QueueState(
                name="Queue-Master",
                tasks=[
                    QueueTask(
                        id="qm-task-001",
                        priority=TaskPriority(level=PriorityLevel.HIGH),
                        submitted_at=datetime.utcnow() - timedelta(seconds=5),
                        invoked_by="Claude-Code",
                        prompt="Coordinate multi-agent task"
                    )
                ],
                updated_at=datetime.utcnow()
            ),
            QueueState(
                name="Summarizer-1",
                tasks=[
                    QueueTask(
                        id="sum-task-001",
                        priority=TaskPriority(level=PriorityLevel.NORMAL),
                        submitted_at=datetime.utcnow() - timedelta(seconds=12),
                        invoked_by="Queue-Master",
                        prompt="Summarize document batch"
                    )
                ],
                updated_at=datetime.utcnow()
            ),
            QueueState(
                name="Reviewer-1",
                tasks=[],
                updated_at=datetime.utcnow()
            ),
            QueueState(
                name="agent-task-queue",
                tasks=[
                    QueueTask(
                        id="atq-task-001",
                        priority=TaskPriority(level=PriorityLevel.NORMAL),
                        submitted_at=datetime.utcnow() - timedelta(seconds=10),
                        invoked_by="Orchestrator",
                        prompt="Process user request"
                    ),
                    QueueTask(
                        id="atq-task-002",
                        priority=TaskPriority(
                            level=PriorityLevel.CRITICAL,
                            waiting_since_mins=2.5
                        ),
                        submitted_at=datetime.utcnow() - timedelta(minutes=2, seconds=30),
                        invoked_by="MapWorker",
                        prompt="Urgent aggregation needed"
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
        
        # Add default models if none found (matching screenshot examples)
        if not models:
            models = [
                LiteLLMModel(
                    model="llama-70",
                    provider="ollama",
                    tpm=4500,
                    rpm=12,
                    tpm_max=10000,
                    rpm_max=60,
                    payment_type=PaymentType.FREE,
                    input_context=4096,
                    output_context=4096,
                    input_types=["text"],
                    output_types=["text"]
                ),
                LiteLLMModel(
                    model="mistral",
                    provider="ollama",
                    tpm=2800,
                    rpm=8,
                    tpm_max=10000,
                    rpm_max=60,
                    payment_type=PaymentType.FREE,
                    input_context=8192,
                    output_context=8192,
                    input_types=["text"],
                    output_types=["text"]
                ),
                LiteLLMModel(
                    model="embed-v2",
                    provider="ollama",
                    tpm=15000,
                    rpm=45,
                    tpm_max=50000,
                    rpm_max=200,
                    payment_type=PaymentType.FREE,
                    input_context=512,
                    output_dim=768,
                    input_types=["text"],
                    output_types=["embedding"]
                ),
                LiteLLMModel(
                    model="codellama",
                    provider="ollama",
                    tpm=1200,
                    rpm=5,
                    tpm_max=10000,
                    rpm_max=60,
                    payment_type=PaymentType.FREE,
                    input_context=16384,
                    output_context=16384,
                    input_types=["text"],
                    output_types=["text"]
                ),
                LiteLLMModel(
                    model="gpt-4",
                    provider="openai",
                    tpm=8000,
                    rpm=15,
                    tpm_max=30000,
                    rpm_max=60,
                    payment_type=PaymentType.POSTPAID,
                    credits=45.50,
                    input_context=8192,
                    output_context=4096,
                    input_types=["text", "image"],
                    output_types=["text"]
                ),
                LiteLLMModel(
                    model="claude-3-sonnet",
                    provider="anthropic",
                    tpm=5000,
                    rpm=10,
                    tpm_max=40000,
                    rpm_max=60,
                    payment_type=PaymentType.POSTPAID,
                    credits=32.75,
                    input_context=200000,
                    output_context=4096,
                    input_types=["text", "image"],
                    output_types=["text"]
                )
            ]

        return models


class OracleMonitorAggregator:
    """
    Main aggregator that collects state from all sources
    and produces the unified Oracle Monitor state.

    Prioritizes K8s-discovered agents over telemetry-inferred agents.
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

        Agent discovery priority:
        1. K8s deployments (auto-detected by naming patterns/labels)
        2. Augmented with telemetry data (active tasks, models used)
        """
        # Collect state from all sources concurrently
        # Use K8s for agent discovery (primary source)
        agents, workloads, queues, models = await asyncio.gather(
            self.k8s_collector.discover_agents(),
            self.k8s_collector.get_workloads(),
            self.telemetry_collector.get_queues(),
            self.litellm_collector.get_models(),
            return_exceptions=True
        )

        # Handle any exceptions
        if isinstance(agents, Exception):
            logger.error(f"Error discovering agents: {agents}")
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

        # Augment agents with telemetry data (active tasks, model usage)
        if agents:
            agents = await self.telemetry_collector.augment_agents_with_telemetry(agents)

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
