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
    AgentSpan, SpanKind, ToolDefinition, ToolInvocation
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
    
    def set_trace_context(self, trace_id: str, parent_span_id: Optional[str] = None):
        """Set the current trace context for this agent"""
        self._trace_id = trace_id
        self._parent_span_id = parent_span_id
    
    async def think(self, messages: List[Dict[str, Any]]) -> str:
        """Make an LLM call with telemetry"""
        if not self._trace_id:
            raise ValueError("Trace context not set. Call set_trace_context first.")
        
        response = await self.llm.completion(
            trace_id=self._trace_id,
            agent_id=self.agent_id,
            agent_name=self.name,
            messages=messages,
            model=self.config.model,
            parent_span_id=self._parent_span_id
        )
        
        return response.choices[0].message.content
    
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
        """Hand off control to another agent"""
        if target_agent_name not in self.sub_agents:
            raise ValueError(f"Agent {target_agent_name} not found")
        
        target_agent = self.sub_agents[target_agent_name]
        
        # Record the handoff
        await self.collector.record_handoff(
            trace_id=self._trace_id,
            source_agent_id=self.agent_id,
            source_agent_name=self.name,
            target_agent_id=target_agent.agent_id,
            target_agent_name=target_agent.name,
            reason=reason,
            context=context
        )
        
        # Set trace context on target agent
        target_agent.set_trace_context(self._trace_id, self._parent_span_id)
        
        # Run the target agent
        return await target_agent.run(context)
    
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
    """Create a demo multi-agent system with telemetry"""
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
