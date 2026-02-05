"""
Oracle Monitor System State Schema
Based on the comprehensive state representation for multi-agent orchestration monitoring
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4
import json


class TaskStatus(str, Enum):
    """Task execution status"""
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class PriorityLevel(str, Enum):
    """Task priority levels"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class PodStatus(str, Enum):
    """Kubernetes pod status"""
    RUNNING = "Running"
    PENDING = "Pending"
    FAILED = "Failed"
    SUCCEEDED = "Succeeded"
    UNKNOWN = "Unknown"


class PaymentType(str, Enum):
    """LLM payment types"""
    PREPAID = "prepaid"
    POSTPAID = "postpaid"
    FREE = "free"


@dataclass
class ActiveTask:
    """Active task running on an agent"""
    id: str
    started_on: datetime
    status: TaskStatus
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "started_on": self.started_on.isoformat(),
            "status": self.status.value
        }


@dataclass
class AgentActivity:
    """Current activity state of an agent"""
    active_task_ids: List[ActiveTask] = field(default_factory=list)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_task_ids": [t.to_dict() for t in self.active_task_ids],
            "updated_at": self.updated_at.isoformat()
        }


@dataclass
class AgentState:
    """State of a registered agent in the system"""
    name: str
    deployment_name: str
    models: List[str]
    activity: AgentActivity
    description: Optional[str] = None
    max_parallel_invocations: int = 1
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "name": self.name,
            "deployment_name": self.deployment_name,
            "models": self.models,
            "activity": self.activity.to_dict(),
            "max_parallel_invocations": self.max_parallel_invocations
        }
        if self.description:
            result["description"] = self.description
        return result


@dataclass
class PodMetrics:
    """Metrics for a single pod"""
    pod_id: str
    cpu: float  # CPU usage in millicores
    memory: float  # Memory usage in MB
    status: PodStatus
    network_in: Optional[float] = None  # Network ingress bytes
    network_out: Optional[float] = None  # Network egress bytes
    updated_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "pod_id": self.pod_id,
            "cpu": self.cpu,
            "memory": self.memory,
            "status": self.status.value
        }
        if self.network_in is not None:
            result["network_in"] = self.network_in
        if self.network_out is not None:
            result["network_out"] = self.network_out
        if self.updated_at:
            result["updated_at"] = self.updated_at.isoformat()
        return result


@dataclass
class WorkloadLiveState:
    """Live state of a workload"""
    active_pods: int
    updated_at: datetime = field(default_factory=datetime.utcnow)
    image: Optional[str] = None
    rolled_out_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "active_pods": self.active_pods,
            "updated_at": self.updated_at.isoformat()
        }
        if self.image:
            result["image"] = self.image
        if self.rolled_out_at:
            result["rolled_out_at"] = self.rolled_out_at.isoformat()
        return result


@dataclass
class WorkloadState:
    """Kubernetes workload metrics per deployment"""
    deployment_name: str
    max_pods: int
    live: WorkloadLiveState
    pods: List[PodMetrics] = field(default_factory=list)
    pod_max_ram: Optional[str] = None  # e.g., "2Gi"
    pod_max_cpu: Optional[str] = None  # e.g., "1000m"
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "deployment_name": self.deployment_name,
            "max_pods": self.max_pods,
            "live": self.live.to_dict(),
            "pods": [p.to_dict() for p in self.pods]
        }
        if self.pod_max_ram:
            result["pod_max_ram"] = self.pod_max_ram
        if self.pod_max_cpu:
            result["pod_max_cpu"] = self.pod_max_cpu
        return result


@dataclass 
class TaskPriority:
    """Task priority information"""
    level: PriorityLevel
    blocked_task: Optional[str] = None  # Task ID this task is blocked by
    waiting_since_mins: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        result = {"level": self.level.value}
        if self.blocked_task:
            result["blocked_task"] = self.blocked_task
        if self.waiting_since_mins is not None:
            result["waiting_since_mins"] = self.waiting_since_mins
        return result


@dataclass
class QueueTask:
    """Task in a queue"""
    id: str
    priority: TaskPriority
    submitted_at: datetime = field(default_factory=datetime.utcnow)
    invoked_by: Optional[str] = None
    prompt: Optional[str] = None
    args: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "id": self.id,
            "priority": self.priority.to_dict(),
            "submitted_at": self.submitted_at.isoformat()
        }
        if self.invoked_by:
            result["invoked_by"] = self.invoked_by
        if self.prompt:
            result["prompt"] = self.prompt
        if self.args:
            result["args"] = self.args
        return result


@dataclass
class QueueState:
    """Task queue state"""
    name: str
    tasks: List[QueueTask] = field(default_factory=list)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "tasks": [t.to_dict() for t in self.tasks],
            "updated_at": self.updated_at.isoformat()
        }


@dataclass
class LiteLLMModel:
    """LLM model configuration and usage"""
    model: str
    provider: str
    tpm_max: float  # Maximum tokens per minute limit
    rpm_max: float  # Maximum requests per minute limit
    tpm: float = 0  # Current tokens per minute usage
    rpm: float = 0  # Current requests per minute usage
    credits: Optional[float] = None
    payment_type: Optional[PaymentType] = None
    input_context: Optional[int] = None
    output_context: Optional[int] = None
    output_dim: Optional[int] = None  # Embedding output dimensions
    input_types: List[str] = field(default_factory=list)
    output_types: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "model": self.model,
            "provider": self.provider,
            "tpm": self.tpm,
            "rpm": self.rpm,
            "tpm_max": self.tpm_max,
            "rpm_max": self.rpm_max
        }
        if self.credits is not None:
            result["credits"] = self.credits
        if self.payment_type:
            result["payment_type"] = self.payment_type.value
        if self.input_context:
            result["input_context"] = self.input_context
        if self.output_context:
            result["output_context"] = self.output_context
        if self.output_dim:
            result["output_dim"] = self.output_dim
        if self.input_types:
            result["input_types"] = self.input_types
        if self.output_types:
            result["output_types"] = self.output_types
        return result


@dataclass
class OracleMonitorState:
    """
    Comprehensive system state representation for multi-agent orchestration monitoring.
    This is the unified view that Claude Code uses for debugging.
    """
    id: str = field(default_factory=lambda: str(uuid4()))
    agents: List[AgentState] = field(default_factory=list)
    workload: List[WorkloadState] = field(default_factory=list)
    queues: List[QueueState] = field(default_factory=list)
    litellm: List[LiteLLMModel] = field(default_factory=list)
    
    # Metadata
    generated_at: datetime = field(default_factory=datetime.utcnow)
    cluster_name: str = "telemetry-cluster"
    namespace: str = "telemetry"
    
    def to_dict(self, strict: bool = False) -> Dict[str, Any]:
        """
        Convert to dict.
        When strict is True, emit only fields defined in the public JSON schema.
        """
        result = {
            "id": self.id,
            "agents": [a.to_dict() for a in self.agents],
            "workload": [w.to_dict() for w in self.workload],
            "queues": [q.to_dict() for q in self.queues],
            "litellm": [l.to_dict() for l in self.litellm]
        }
        if not strict:
            result["generated_at"] = self.generated_at.isoformat()
            result["cluster_name"] = self.cluster_name
            result["namespace"] = self.namespace
        return result
    
    def to_json(self, indent: int = 2, strict: bool = False) -> str:
        """Convert to JSON string"""
        return json.dumps(self.to_dict(strict=strict), indent=indent, default=str)
    
    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of the system state for quick debugging"""
        total_active_tasks = sum(
            len(a.activity.active_task_ids) for a in self.agents
        )
        total_queued_tasks = sum(len(q.tasks) for q in self.queues)
        total_running_pods = sum(w.live.active_pods for w in self.workload)
        
        # Check for issues
        issues = []
        
        # Check for failed tasks
        for agent in self.agents:
            for task in agent.activity.active_task_ids:
                if task.status == TaskStatus.FAILED:
                    issues.append(f"Failed task {task.id} on agent {agent.name}")
        
        # Check for high-priority queued tasks
        for queue in self.queues:
            for task in queue.tasks:
                if task.priority.level == PriorityLevel.CRITICAL:
                    issues.append(f"Critical task {task.id} waiting in queue {queue.name}")
                if task.priority.waiting_since_mins and task.priority.waiting_since_mins > 5:
                    issues.append(f"Task {task.id} waiting for {task.priority.waiting_since_mins:.1f} mins")
        
        # Check for pods with issues
        for workload in self.workload:
            for pod in workload.pods:
                if pod.status in [PodStatus.FAILED, PodStatus.PENDING]:
                    issues.append(f"Pod {pod.pod_id} is {pod.status.value}")
        
        # Check LLM rate limits
        for model in self.litellm:
            if model.tpm_max > 0 and model.tpm / model.tpm_max > 0.9:
                issues.append(f"Model {model.model} at {model.tpm/model.tpm_max*100:.0f}% TPM limit")
            if model.rpm_max > 0 and model.rpm / model.rpm_max > 0.9:
                issues.append(f"Model {model.model} at {model.rpm/model.rpm_max*100:.0f}% RPM limit")
        
        return {
            "agent_count": len(self.agents),
            "active_tasks": total_active_tasks,
            "queued_tasks": total_queued_tasks,
            "running_pods": total_running_pods,
            "llm_models": len(self.litellm),
            "issues": issues,
            "status": "healthy" if not issues else "degraded",
            "generated_at": self.generated_at.isoformat()
        }
    
    def to_diff_log(self, previous_state: Optional['OracleMonitorState'] = None) -> str:
        """
        Generate a diff log format as shown in the presentation.
        This is the format Claude Code can easily parse.
        """
        lines = []
        lines.append(f"=== Oracle Monitor State Diff ===")
        lines.append(f"Generated: {self.generated_at.isoformat()}")
        lines.append(f"Cluster: {self.cluster_name}/{self.namespace}")
        lines.append("")
        
        # Summary
        summary = self.get_summary()
        lines.append(f"[STATUS: {summary['status'].upper()}]")
        lines.append(f"Agents: {summary['agent_count']} | Active Tasks: {summary['active_tasks']} | Queued: {summary['queued_tasks']}")
        lines.append(f"Pods: {summary['running_pods']} | LLM Models: {summary['llm_models']}")
        lines.append("")
        
        # Issues
        if summary['issues']:
            lines.append("[ISSUES]")
            for issue in summary['issues']:
                lines.append(f"  ! {issue}")
            lines.append("")
        
        # Agent details
        lines.append("[AGENTS]")
        for agent in self.agents:
            task_count = len(agent.activity.active_task_ids)
            running = sum(1 for t in agent.activity.active_task_ids if t.status == TaskStatus.RUNNING)
            lines.append(f"  {agent.name}: {running}/{task_count} running (max: {agent.max_parallel_invocations})")
        lines.append("")
        
        # Workload details
        lines.append("[WORKLOAD]")
        for workload in self.workload:
            lines.append(f"  {workload.deployment_name}: {workload.live.active_pods}/{workload.max_pods} pods")
        lines.append("")
        
        # Queue details
        lines.append("[QUEUES]")
        for queue in self.queues:
            critical = sum(1 for t in queue.tasks if t.priority.level == PriorityLevel.CRITICAL)
            high = sum(1 for t in queue.tasks if t.priority.level == PriorityLevel.HIGH)
            lines.append(f"  {queue.name}: {len(queue.tasks)} tasks (critical: {critical}, high: {high})")
        lines.append("")
        
        # LLM details
        lines.append("[LLM USAGE]")
        for model in self.litellm:
            tpm_pct = (model.tpm / model.tpm_max * 100) if model.tpm_max > 0 else 0
            rpm_pct = (model.rpm / model.rpm_max * 100) if model.rpm_max > 0 else 0
            lines.append(f"  {model.model}: TPM {tpm_pct:.0f}% | RPM {rpm_pct:.0f}%")
        
        return "\n".join(lines)


# JSON Schema for validation
ORACLE_MONITOR_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Oracle Monitor System State",
    "description": "Comprehensive state representation for multi-agent orchestration monitoring",
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": "Unique identifier for the system instance"
        },
        "agents": {
            "type": "array",
            "description": "List of registered agents in the system",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Agent identifier/name"
                    },
                    "description": {
                        "type": "string",
                        "description": "Human-readable description of agent's purpose"
                    },
                    "max_parallel_invocations": {
                        "type": "integer",
                        "description": "Maximum concurrent tasks this agent can handle"
                    },
                    "deployment_name": {
                        "type": "string",
                        "description": "Kubernetes deployment name for this agent"
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "LLM models this agent is authorized to use"
                    },
                    "activity": {
                        "type": "object",
                        "properties": {
                            "active_task_ids": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "started_on": {"type": "string", "format": "date-time"},
                                        "status": {"type": "string", "enum": ["running", "waiting", "completed", "failed"]}
                                    },
                                    "required": ["id", "started_on", "status"]
                                }
                            },
                            "updated_at": {"type": "string", "format": "date-time"}
                        },
                        "required": ["active_task_ids", "updated_at"]
                    }
                },
                "required": ["name", "deployment_name", "models", "activity"]
            }
        },
        "workload": {
            "type": "array",
            "description": "Kubernetes workload metrics per deployment",
            "items": {
                "type": "object",
                "properties": {
                    "deployment_name": {
                        "type": "string",
                        "description": "Kubernetes deployment name"
                    },
                    "max_pods": {
                        "type": "integer",
                        "description": "Maximum pods allowed for this deployment"
                    },
                    "live": {
                        "type": "object",
                        "properties": {
                            "active_pods": {"type": "integer"},
                            "updated_at": {"type": "string", "format": "date-time"},
                            "image": {"type": "string"},
                            "rolled_out_at": {"type": "string", "format": "date-time"}
                        },
                        "required": ["active_pods", "updated_at"]
                    },
                    "pod_max_ram": {
                        "type": "string",
                        "description": "Memory limit per pod (e.g., '2Gi')"
                    },
                    "pod_max_cpu": {
                        "type": "string",
                        "description": "CPU limit per pod (e.g., '1000m')"
                    },
                    "pods": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "pod_id": {"type": "string"},
                                "cpu": {"type": "number", "description": "CPU usage in millicores"},
                                "memory": {"type": "number", "description": "Memory usage in MB"},
                                "network_in": {"type": "number", "description": "Network ingress bytes"},
                                "network_out": {"type": "number", "description": "Network egress bytes"},
                                "status": {"type": "string", "enum": ["Running", "Pending", "Failed", "Succeeded", "Unknown"]},
                                "updated_at": {"type": "string", "format": "date-time"}
                            },
                            "required": ["pod_id", "cpu", "memory", "status"]
                        }
                    }
                },
                "required": ["deployment_name", "max_pods", "live", "pods"]
            }
        },
        "queues": {
            "type": "array",
            "description": "Task queues and their contents",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Queue/topic name"
                    },
                    "tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "invoked_by": {"type": "string", "description": "Agent or system that submitted the task"},
                                "priority": {
                                    "type": "object",
                                    "properties": {
                                        "level": {"type": "string", "enum": ["low", "normal", "high", "critical"]},
                                        "blocked_task": {"type": "string", "description": "Task ID this task is blocked by"},
                                        "waiting_since_mins": {"type": "number", "description": "Minutes waiting in queue"}
                                    },
                                    "required": ["level"]
                                },
                                "prompt": {"type": "string", "description": "Task description or prompt"},
                                "args": {"type": "object", "description": "Task arguments"},
                                "submitted_at": {"type": "string", "format": "date-time"}
                            },
                            "required": ["id", "priority", "submitted_at"]
                        }
                    },
                    "updated_at": {"type": "string", "format": "date-time"}
                },
                "required": ["name", "tasks", "updated_at"]
            }
        },
        "litellm": {
            "type": "array",
            "description": "LLM model configurations and usage",
            "items": {
                "type": "object",
                "properties": {
                    "model": {"type": "string", "description": "Model identifier"},
                    "provider": {"type": "string", "description": "Provider name (openai, anthropic, etc.)"},
                    "tpm": {"type": "number", "description": "Current tokens per minute usage"},
                    "rpm": {"type": "number", "description": "Current requests per minute usage"},
                    "tpm_max": {"type": "number", "description": "Maximum tokens per minute limit"},
                    "rpm_max": {"type": "number", "description": "Maximum requests per minute limit"},
                    "credits": {"type": "number", "description": "Available credits/balance"},
                    "payment_type": {"type": "string", "enum": ["prepaid", "postpaid", "free"]},
                    "input_context": {"type": "integer", "description": "Maximum input context length"},
                    "output_context": {"type": "integer", "description": "Maximum output context length"},
                    "output_dim": {"type": "integer", "description": "Embedding output dimensions (if applicable)"},
                    "input_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Supported input types (text, image, audio)"
                    },
                    "output_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Supported output types"
                    }
                },
                "required": ["model", "provider", "tpm_max", "rpm_max"]
            }
        }
    },
    "required": ["id", "agents", "workload", "queues", "litellm"]
}
