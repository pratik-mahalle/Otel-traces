"""
Sample Multi-Agent System with Telemetry
Demonstrates the telemetry system for debugging multi-agent interactions
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from ..telemetry.collector import TelemetryCollector, InMemoryTelemetryCollector
from ..telemetry.litellm_integration import TracedLiteLLM
from ..telemetry.schemas import (
    AgentSpan, SpanKind, ToolDefinition, ToolInvocation,
    DetailedError, ErrorCategory, ErrorSeverity,
    AgentMessage, MessageType, MessagePriority, agent_queue_topic
)

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Configuration for an agent"""
    agent_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Agent"
    description: str = ""
    model: str = "ollama/llama2"
    system_prompt: str = ""
    tools: List[ToolDefinition] = field(default_factory=list)
    max_iterations: int = 10


class BaseAgent(ABC):
    """
    Base class for agents with built-in telemetry support.
    All agents should inherit from this class.
    
    Each agent has its own Kafka queue:
        agent-queue-{agent_name_lowercase}
    
    Other agents send tasks by writing to this queue.
    This agent reads from its own queue to receive work.
    """
    
    def __init__(
        self,
        config: AgentConfig,
        collector: TelemetryCollector,
        llm: TracedLiteLLM
    ):
        self.config = config
        self.collector = collector
        self.llm = llm
        
        self.agent_id = config.agent_id
        self.name = config.name
        
        # Current trace context
        self._trace_id: Optional[str] = None
        self._parent_span_id: Optional[str] = None
        
        # Queue for receiving results back from other agents
        self._pending_results: Dict[str, asyncio.Future] = {}
        self._queue_consumer_task: Optional[asyncio.Task] = None
    
    def set_trace_context(self, trace_id: str, parent_span_id: Optional[str] = None):
        """Set the current trace context for this agent"""
        self._trace_id = trace_id
        self._parent_span_id = parent_span_id
    
    @property
    def queue_topic(self) -> str:
        """The Kafka topic for this agent's personal queue"""
        return agent_queue_topic(self.name)
    
    async def start_queue_consumer(self):
        """
        Start consuming messages from this agent's own Kafka queue.
        
        Messages arrive as AgentMessage objects. Task messages trigger
        run(), and results are routed to pending futures.
        """
        async def _handle_queue_message(msg: AgentMessage):
            await self._process_queue_message(msg)
        
        self._queue_consumer_task = asyncio.create_task(
            self.collector.consume_agent_queue(self.name, _handle_queue_message)
        )
        logger.info(f"Agent '{self.name}' started queue consumer on {self.queue_topic}")
    
    async def _process_queue_message(self, msg: AgentMessage):
        """
        Process an incoming message from this agent's queue.
        
        - TASK messages: execute run() and send result back to source agent
        - RESULT/ERROR messages: resolve a pending future so the caller unblocks
        """
        if msg.message_type == MessageType.TASK:
            # Set trace context from the incoming message
            if msg.trace_id:
                self.set_trace_context(msg.trace_id, msg.span_id)
            
            try:
                result = await self.run(msg.payload)
                
                # Send the result back to the source agent's queue
                result_msg = AgentMessage(
                    source_agent=self.name,
                    target_agent=msg.source_agent,
                    message_type=MessageType.RESULT,
                    payload=result,
                    trace_id=msg.trace_id,
                    parent_message_id=msg.message_id,
                    reason=f"Result for: {msg.reason}"
                )
                await self.collector.send_to_agent(result_msg)
                
            except Exception as e:
                # Send an error message back to the source agent's queue
                error_msg = AgentMessage(
                    source_agent=self.name,
                    target_agent=msg.source_agent,
                    message_type=MessageType.ERROR,
                    payload={"error": str(e), "error_type": type(e).__name__},
                    trace_id=msg.trace_id,
                    parent_message_id=msg.message_id,
                    reason=f"Error processing: {msg.reason}"
                )
                await self.collector.send_to_agent(error_msg)
                
                # Also record to the telemetry error store
                await self.collector.record_error_from_exception(
                    exception=e,
                    trace_id=msg.trace_id,
                    agent_name=self.name,
                    agent_id=self.agent_id,
                    operation=f"queue_task:{msg.reason}"
                )
        
        elif msg.message_type in (MessageType.RESULT, MessageType.ERROR):
            # Resolve a pending future if this is a response to a task we sent
            parent_id = msg.parent_message_id
            if parent_id and parent_id in self._pending_results:
                future = self._pending_results.pop(parent_id)
                if msg.message_type == MessageType.ERROR:
                    future.set_exception(
                        RuntimeError(
                            f"Agent {msg.source_agent} failed: "
                            f"{msg.payload.get('error', 'Unknown error')}"
                        )
                    )
                else:
                    future.set_result(msg.payload)
    
    async def think(self, messages: List[Dict[str, Any]]) -> str:
        """Make an LLM call with telemetry"""
        if not self._trace_id:
            raise ValueError("Trace context not set. Call set_trace_context first.")
        
        try:
            response = await self.llm.completion(
                trace_id=self._trace_id,
                agent_id=self.agent_id,
                agent_name=self.name,
                messages=messages,
                model=self.config.model,
                parent_span_id=self._parent_span_id
            )
            
            return response.choices[0].message.content
        except Exception as e:
            # Record LLM error to the dedicated Kafka errors topic
            await self.collector.record_error_from_exception(
                exception=e,
                trace_id=self._trace_id,
                agent_name=self.name,
                agent_id=self.agent_id,
                operation=f"llm_call:{self.config.model}",
                model_name=self.config.model,
                prompt_preview=str(messages[-1].get("content", ""))[:500] if messages else None
            )
            raise
    
    async def execute_tool(
        self,
        tool_name: str,
        tool_func: Callable,
        parameters: Dict[str, Any]
    ) -> Any:
        """Execute a tool with telemetry"""
        if not self._trace_id:
            raise ValueError("Trace context not set.")
        
        async with self.collector.span(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=self.name,
            span_kind=SpanKind.TOOL,
            parent_span_id=self._parent_span_id,
            input_data={"tool": tool_name, "parameters": parameters}
        ) as span:
            
            invocation = ToolInvocation(
                tool_name=tool_name,
                input_parameters=parameters
            )
            
            try:
                if asyncio.iscoroutinefunction(tool_func):
                    result = await tool_func(**parameters)
                else:
                    result = tool_func(**parameters)
                
                invocation.output = result
                invocation.end_time = datetime.utcnow()
                invocation.duration_ms = (invocation.end_time - invocation.start_time).total_seconds() * 1000
                
                span.tool_invocations.append(invocation)
                span.output_data = {"result": result}
                
                return result
                
            except Exception as e:
                invocation.error = str(e)
                span.tool_invocations.append(invocation)
                
                # Record tool error to the dedicated Kafka errors topic
                await self.collector.record_error_from_exception(
                    exception=e,
                    trace_id=self._trace_id,
                    span_id=span.span_id,
                    agent_name=self.name,
                    agent_id=self.agent_id,
                    operation=f"tool_call:{tool_name}",
                    tool_name=tool_name,
                    tool_parameters=parameters
                )
                raise
    
    @abstractmethod
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run the agent - must be implemented by subclasses"""
        pass


class OrchestratorAgent(BaseAgent):
    """
    Orchestrator agent that coordinates other agents.
    Handles task decomposition and agent handoffs.
    """
    
    def __init__(
        self,
        config: AgentConfig,
        collector: TelemetryCollector,
        llm: TracedLiteLLM,
        sub_agents: Dict[str, BaseAgent] = None
    ):
        super().__init__(config, collector, llm)
        self.sub_agents = sub_agents or {}
    
    def register_agent(self, agent: BaseAgent):
        """Register a sub-agent"""
        self.sub_agents[agent.name] = agent
    
    async def handoff_to(
        self,
        target_agent_name: str,
        reason: str,
        context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Hand off a task to another agent via its Kafka queue.
        
        Flow:
            1. Orchestrator writes AgentMessage(type=TASK) -> agent-queue-{target}
            2. Target agent reads from its own queue
            3. Target agent processes and writes AgentMessage(type=RESULT) -> agent-queue-orchestrator
            4. Orchestrator reads the result from its own queue
        """
        if target_agent_name not in self.sub_agents:
            error = ValueError(f"Agent {target_agent_name} not found")
            # Record handoff error to telemetry errors topic
            await self.collector.record_error_from_exception(
                exception=error,
                trace_id=self._trace_id,
                agent_name=self.name,
                agent_id=self.agent_id,
                operation=f"handoff_to:{target_agent_name}"
            )
            raise error
        
        target_agent = self.sub_agents[target_agent_name]
        
        # Build the inter-agent message
        task_msg = AgentMessage(
            source_agent=self.name,
            target_agent=target_agent_name,
            message_type=MessageType.TASK,
            priority=MessagePriority.NORMAL,
            payload=context,
            trace_id=self._trace_id,
            span_id=self._parent_span_id,
            reason=reason
        )
        
        # Register a future to receive the result
        result_future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_results[task_msg.message_id] = result_future
        
        # Send the task to the target agent's queue via Kafka
        # (send_to_agent also records a telemetry handoff event)
        await self.collector.send_to_agent(task_msg)
        
        try:
            # Wait for the result to come back on our own queue
            result = await asyncio.wait_for(result_future, timeout=300)
            return result
        except asyncio.TimeoutError:
            self._pending_results.pop(task_msg.message_id, None)
            error = TimeoutError(
                f"Handoff to {target_agent_name} timed out after 300s"
            )
            await self.collector.record_error_from_exception(
                exception=error,
                trace_id=self._trace_id,
                agent_name=self.name,
                agent_id=self.agent_id,
                operation=f"handoff_timeout:{target_agent_name}"
            )
            raise error
        except Exception as e:
            self._pending_results.pop(task_msg.message_id, None)
            # Record the downstream agent failure
            await self.collector.record_error_from_exception(
                exception=e,
                trace_id=self._trace_id,
                agent_name=self.name,
                agent_id=self.agent_id,
                operation=f"handoff_execution:{target_agent_name}"
            )
            raise
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run the orchestrator"""
        async with self.collector.span(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=self.name,
            span_kind=SpanKind.AGENT,
            parent_span_id=self._parent_span_id,
            input_data=input_data
        ) as span:
            
            # Planning phase
            plan = await self._plan(input_data)
            span.add_event("planning_complete", {"plan": plan})
            
            # Execute the plan
            results = {}
            for step in plan.get("steps", []):
                agent_name = step.get("agent")
                task = step.get("task")
                
                if agent_name and agent_name in self.sub_agents:
                    result = await self.handoff_to(
                        agent_name,
                        reason=f"Execute task: {task}",
                        context={"task": task, "input": input_data}
                    )
                    results[agent_name] = result
            
            # Synthesize results
            final_output = await self._synthesize(results)
            span.output_data = {"final_output": final_output}
            
            return {"output": final_output, "steps": results}
    
    async def _plan(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create an execution plan"""
        async with self.collector.span(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=f"{self.name}:Planning",
            span_kind=SpanKind.PLANNING,
            parent_span_id=self._parent_span_id,
            input_data=input_data
        ):
            available_agents = list(self.sub_agents.keys())
            
            prompt = f"""You are a task orchestrator. Given the user's request and available agents,
            create a simple execution plan.
            
            Available agents: {available_agents}
            User request: {input_data.get('query', input_data)}
            
            Respond with a JSON object containing:
            - steps: array of {{ agent: string, task: string }}
            
            Keep it simple - just identify which agent(s) should handle this.
            """
            
            messages = [
                {"role": "system", "content": self.config.system_prompt or "You are a helpful orchestrator."},
                {"role": "user", "content": prompt}
            ]
            
            response = await self.think(messages)
            
            # Try to parse JSON from response
            try:
                # Simple extraction of JSON
                start = response.find('{')
                end = response.rfind('}') + 1
                if start >= 0 and end > start:
                    return json.loads(response[start:end])
            except json.JSONDecodeError:
                pass
            
            # Default plan if parsing fails
            return {"steps": []}
    
    async def _synthesize(self, results: Dict[str, Any]) -> str:
        """Synthesize results from multiple agents"""
        if not results:
            return "No results to synthesize."
        
        prompt = f"""Synthesize these results from different agents into a coherent response:
        
        Results: {json.dumps(results, default=str)}
        
        Provide a clear, unified response.
        """
        
        messages = [
            {"role": "system", "content": "You are a helpful assistant that synthesizes information."},
            {"role": "user", "content": prompt}
        ]
        
        return await self.think(messages)


class ResearchAgent(BaseAgent):
    """Agent that performs research tasks"""
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run research task"""
        async with self.collector.span(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=self.name,
            span_kind=SpanKind.AGENT,
            parent_span_id=self._parent_span_id,
            input_data=input_data
        ) as span:
            
            task = input_data.get("task", str(input_data))
            
            messages = [
                {"role": "system", "content": self.config.system_prompt or "You are a research assistant."},
                {"role": "user", "content": f"Research and provide information about: {task}"}
            ]
            
            response = await self.think(messages)
            
            span.output_data = {"research_result": response}
            return {"result": response, "agent": self.name}


class WriterAgent(BaseAgent):
    """Agent that writes and edits content"""
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run writing task"""
        async with self.collector.span(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=self.name,
            span_kind=SpanKind.AGENT,
            parent_span_id=self._parent_span_id,
            input_data=input_data
        ) as span:
            
            task = input_data.get("task", str(input_data))
            
            messages = [
                {"role": "system", "content": self.config.system_prompt or "You are a skilled writer."},
                {"role": "user", "content": f"Write content for: {task}"}
            ]
            
            response = await self.think(messages)
            
            span.output_data = {"written_content": response}
            return {"result": response, "agent": self.name}


class CodeAgent(BaseAgent):
    """Agent that writes and analyzes code"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tools = {
            "execute_python": self._execute_python,
            "analyze_code": self._analyze_code
        }
    
    async def _execute_python(self, code: str) -> str:
        """Safely execute Python code (simulated)"""
        # In production, this would use a sandboxed environment
        return f"[Simulated execution of code: {code[:100]}...]"
    
    async def _analyze_code(self, code: str, language: str = "python") -> str:
        """Analyze code for issues"""
        return f"[Code analysis for {language}: No issues found]"
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run coding task"""
        async with self.collector.span(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=self.name,
            span_kind=SpanKind.AGENT,
            parent_span_id=self._parent_span_id,
            input_data=input_data
        ) as span:
            
            task = input_data.get("task", str(input_data))
            
            messages = [
                {"role": "system", "content": self.config.system_prompt or "You are an expert programmer."},
                {"role": "user", "content": f"Write code for: {task}"}
            ]
            
            response = await self.think(messages)
            
            # Execute tool if code was generated
            if "```" in response:
                execution_result = await self.execute_tool(
                    "execute_python",
                    self._execute_python,
                    {"code": response}
                )
                span.add_event("code_executed", {"result": execution_result})
            
            span.output_data = {"code": response}
            return {"result": response, "agent": self.name}


class ReflectionAgent(BaseAgent):
    """Agent that reflects on and improves responses"""
    
    async def run(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Run reflection task"""
        async with self.collector.span(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=self.name,
            span_kind=SpanKind.REFLECTION,
            parent_span_id=self._parent_span_id,
            input_data=input_data
        ) as span:
            
            content = input_data.get("content", str(input_data))
            
            messages = [
                {"role": "system", "content": "You are a thoughtful reviewer who improves content."},
                {"role": "user", "content": f"Review and suggest improvements for:\n\n{content}"}
            ]
            
            response = await self.think(messages)
            
            span.output_data = {"reflection": response}
            return {"result": response, "agent": self.name}


async def create_demo_system(collector: TelemetryCollector) -> OrchestratorAgent:
    """
    Create a demo multi-agent system with Kafka queue-based communication.
    
    Each agent gets its own Kafka queue:
        agent-queue-orchestrator
        agent-queue-researcher
        agent-queue-writer
        agent-queue-coder
        agent-queue-reviewer
    
    Agents communicate by writing to each other's queues.
    """
    llm = TracedLiteLLM(
        collector=collector,
        default_model="ollama/llama2",
        default_provider="ollama"
    )
    
    # Create sub-agents
    researcher = ResearchAgent(
        config=AgentConfig(
            name="Researcher",
            description="Researches and gathers information",
            model="ollama/llama2",
            system_prompt="You are a thorough research assistant."
        ),
        collector=collector,
        llm=llm
    )
    
    writer = WriterAgent(
        config=AgentConfig(
            name="Writer",
            description="Writes and edits content",
            model="ollama/llama2",
            system_prompt="You are a skilled content writer."
        ),
        collector=collector,
        llm=llm
    )
    
    coder = CodeAgent(
        config=AgentConfig(
            name="Coder",
            description="Writes and analyzes code",
            model="ollama/llama2",
            system_prompt="You are an expert programmer."
        ),
        collector=collector,
        llm=llm
    )
    
    reviewer = ReflectionAgent(
        config=AgentConfig(
            name="Reviewer",
            description="Reviews and improves outputs",
            model="ollama/llama2",
            system_prompt="You are a thoughtful reviewer."
        ),
        collector=collector,
        llm=llm
    )
    
    # Create orchestrator
    orchestrator = OrchestratorAgent(
        config=AgentConfig(
            name="Orchestrator",
            description="Coordinates multi-agent tasks",
            model="ollama/llama2",
            system_prompt="You are an efficient task coordinator."
        ),
        collector=collector,
        llm=llm
    )
    
    # Register sub-agents
    orchestrator.register_agent(researcher)
    orchestrator.register_agent(writer)
    orchestrator.register_agent(coder)
    orchestrator.register_agent(reviewer)
    
    # Start queue consumers so each agent reads from its own Kafka queue
    all_agents = [orchestrator, researcher, writer, coder, reviewer]
    for agent in all_agents:
        await agent.start_queue_consumer()
    
    return orchestrator


async def run_demo():
    """Run a demo of the multi-agent system with telemetry"""
    # Use in-memory collector for demo
    collector = InMemoryTelemetryCollector()
    await collector.start()
    
    # Setup event handler for debugging
    def debug_handler(event):
        print(f"[{event.severity.upper()}] {event.agent_name}: {event.message}")
    
    collector.on_event("agent_start", debug_handler)
    collector.on_event("llm_request", debug_handler)
    collector.on_event("llm_response", debug_handler)
    collector.on_event("handoff", debug_handler)
    collector.on_event("error", debug_handler)
    
    try:
        # Create the system
        orchestrator = await create_demo_system(collector)
        
        # Create a trace
        trace = collector.create_trace(
            user_input="Write a blog post about AI agents",
            session_id="demo-session-001",
            user_id="demo-user"
        )
        
        # Set trace context on orchestrator
        orchestrator.set_trace_context(trace.trace_id)
        
        # Run the orchestrator
        result = await orchestrator.run({"query": "Write a blog post about AI agents"})
        
        # Complete the trace
        await collector.complete_trace(trace.trace_id, str(result))
        
        # Print telemetry summary
        print("\n=== TELEMETRY SUMMARY ===")
        trace_data = collector.get_trace(trace.trace_id)
        if trace_data:
            print(f"Trace ID: {trace_data['trace_id']}")
            print(f"Duration: {trace_data['total_duration_ms']:.2f}ms")
            print(f"Agents: {trace_data['agent_count']}")
            print(f"LLM Calls: {trace_data['llm_call_count']}")
            print(f"Total Tokens: {trace_data['total_tokens']}")
        
        # Print spans
        print("\n=== SPANS ===")
        for span in collector.get_spans(trace.trace_id):
            print(f"  [{span['span_kind']}] {span['agent_name']}: {span['status']} ({span.get('duration_ms', 0):.2f}ms)")
        
        return result
        
    finally:
        await collector.stop()


if __name__ == "__main__":
    asyncio.run(run_demo())
