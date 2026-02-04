#!/usr/bin/env python3
"""
Demo script for Multi-Agent Telemetry System
Runs a sample multi-agent interaction with full telemetry
"""

import asyncio
import os
import logging
import sys

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.telemetry.collector import TelemetryCollector, InMemoryTelemetryCollector
from src.telemetry.litellm_integration import TracedLiteLLM
from src.telemetry.schemas import SpanKind
from src.agents.multi_agent_system import (
    AgentConfig, OrchestratorAgent, ResearchAgent, WriterAgent, CodeAgent
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def run_demo():
    """Run a demo of the multi-agent telemetry system"""
    
    # Configuration from environment
    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    use_kafka = os.getenv("USE_KAFKA", "true").lower() == "true"
    
    logger.info(f"Starting demo with Kafka: {kafka_servers}, Ollama: {ollama_url}")
    
    # Initialize telemetry collector
    if use_kafka:
        try:
            collector = TelemetryCollector(
                kafka_bootstrap_servers=kafka_servers,
                service_name="multi-agent-demo",
                environment="development"
            )
            await collector.start()
            logger.info("Connected to Kafka")
        except Exception as e:
            logger.warning(f"Failed to connect to Kafka: {e}. Using in-memory collector.")
            collector = InMemoryTelemetryCollector()
            await collector.start()
    else:
        collector = InMemoryTelemetryCollector()
        await collector.start()
    
    # Set up event handlers for debugging output
    async def debug_event_handler(event):
        severity_colors = {
            "info": "\033[94m",
            "warning": "\033[93m", 
            "error": "\033[91m",
            "debug": "\033[90m"
        }
        reset = "\033[0m"
        color = severity_colors.get(event.severity, "")
        print(f"{color}[{event.event_type}] {event.agent_name}: {event.message}{reset}")
    
    collector.on_event("agent_start", debug_event_handler)
    collector.on_event("agent_end", debug_event_handler)
    collector.on_event("llm_request", debug_event_handler)
    collector.on_event("llm_response", debug_event_handler)
    collector.on_event("handoff", debug_event_handler)
    collector.on_event("error", debug_event_handler)
    
    try:
        # Initialize LiteLLM with Ollama
        llm = TracedLiteLLM(
            collector=collector,
            default_model=f"ollama/llama2",
            default_provider="ollama"
        )
        
        # Set Ollama base URL for LiteLLM
        import litellm
        litellm.api_base = ollama_url
        
        # Create agents
        researcher = ResearchAgent(
            config=AgentConfig(
                name="Researcher",
                description="Researches and gathers information",
                model="ollama/llama2",
                system_prompt="You are a thorough research assistant. Keep responses concise."
            ),
            collector=collector,
            llm=llm
        )
        
        writer = WriterAgent(
            config=AgentConfig(
                name="Writer", 
                description="Writes and edits content",
                model="ollama/llama2",
                system_prompt="You are a skilled content writer. Keep responses concise."
            ),
            collector=collector,
            llm=llm
        )
        
        coder = CodeAgent(
            config=AgentConfig(
                name="Coder",
                description="Writes and analyzes code",
                model="ollama/llama2",
                system_prompt="You are an expert programmer. Keep responses concise."
            ),
            collector=collector,
            llm=llm
        )
        
        orchestrator = OrchestratorAgent(
            config=AgentConfig(
                name="Orchestrator",
                description="Coordinates multi-agent tasks",
                model="ollama/llama2",
                system_prompt="You are an efficient task coordinator. Keep responses concise and return valid JSON."
            ),
            collector=collector,
            llm=llm
        )
        
        # Register sub-agents
        orchestrator.register_agent(researcher)
        orchestrator.register_agent(writer)
        orchestrator.register_agent(coder)
        
        # Demo queries
        demo_queries = [
            "What is machine learning and how does it work?",
            "Write a Python function to calculate fibonacci numbers",
            "Explain the benefits of microservices architecture"
        ]
        
        print("\n" + "="*60)
        print("Multi-Agent Telemetry Demo")
        print("="*60 + "\n")
        
        for i, query in enumerate(demo_queries, 1):
            print(f"\n--- Query {i}: {query} ---\n")
            
            # Create a new trace for each query
            trace = collector.create_trace(
                user_input=query,
                session_id=f"demo-session-{i}",
                user_id="demo-user",
                metadata={"demo_run": True, "query_number": i}
            )
            
            # Set trace context on orchestrator
            orchestrator.set_trace_context(trace.trace_id)
            
            try:
                # Run the orchestrator
                result = await orchestrator.run({"query": query})
                
                # Complete the trace
                await collector.complete_trace(
                    trace.trace_id,
                    str(result.get("output", ""))
                )
                
                print(f"\n--- Result ---")
                print(result.get("output", "No output")[:500] + "...")
                
            except Exception as e:
                logger.error(f"Error processing query: {e}")
                if trace.trace_id in collector._active_traces:
                    collector._active_traces[trace.trace_id].status = "failed"
                    collector._active_traces[trace.trace_id].error_message = str(e)
                    await collector.complete_trace(trace.trace_id, f"Error: {e}")
            
            # Small delay between queries
            await asyncio.sleep(2)
        
        # Print summary
        print("\n" + "="*60)
        print("Demo Summary")
        print("="*60)
        
        if isinstance(collector, InMemoryTelemetryCollector):
            traces = list(collector._messages.get("agent-telemetry-traces", []))
            spans = list(collector._messages.get("agent-telemetry-spans", []))
            
            print(f"Total traces: {len(traces)}")
            print(f"Total spans: {len(spans)}")
            
            for trace_msg in traces:
                trace = trace_msg.get("value", {})
                print(f"\nTrace: {trace.get('trace_id', 'N/A')[:8]}...")
                print(f"  Status: {trace.get('status', 'N/A')}")
                print(f"  Duration: {trace.get('total_duration_ms', 0):.2f}ms")
                print(f"  Agents: {trace.get('agent_count', 0)}")
                print(f"  LLM Calls: {trace.get('llm_call_count', 0)}")
                print(f"  Tokens: {trace.get('total_tokens', 0)}")
        
        print("\n" + "="*60)
        print("Demo completed!")
        print("="*60 + "\n")
        
    finally:
        await collector.stop()


async def run_simple_demo():
    """Run a simplified demo without LLM calls (for testing telemetry)"""
    
    logger.info("Running simplified demo...")
    
    collector = InMemoryTelemetryCollector()
    await collector.start()
    
    # Event handler
    def debug_handler(event):
        print(f"[{event.event_type}] {event.agent_name}: {event.message}")
    
    for event_type in ["agent_start", "agent_end", "handoff", "llm_call_start"]:
        collector.on_event(event_type, debug_handler)
    
    try:
        # Create a trace
        trace = collector.create_trace(
            user_input="Test query for telemetry",
            session_id="simple-demo",
            user_id="test-user"
        )
        
        # Simulate agent execution with spans
        async with collector.span(
            trace_id=trace.trace_id,
            agent_id="orchestrator-1",
            agent_name="Orchestrator",
            span_kind=SpanKind.AGENT
        ) as orchestrator_span:
            
            await asyncio.sleep(0.1)  # Simulate work
            
            # Simulate handoff
            await collector.record_handoff(
                trace_id=trace.trace_id,
                source_agent_id="orchestrator-1",
                source_agent_name="Orchestrator",
                target_agent_id="researcher-1",
                target_agent_name="Researcher",
                reason="Delegate research task",
                context={"task": "Research AI"}
            )
            
            # Simulate researcher span
            async with collector.span(
                trace_id=trace.trace_id,
                agent_id="researcher-1",
                agent_name="Researcher",
                span_kind=SpanKind.AGENT,
                parent_span_id=orchestrator_span.span_id
            ) as researcher_span:
                
                await asyncio.sleep(0.1)  # Simulate work
                
                # Simulate LLM call
                async with collector.span(
                    trace_id=trace.trace_id,
                    agent_id="researcher-1",
                    agent_name="Researcher:LLM",
                    span_kind=SpanKind.LLM_CALL,
                    parent_span_id=researcher_span.span_id,
                    model_name="ollama/llama2"
                ):
                    await asyncio.sleep(0.2)  # Simulate LLM call
        
        # Complete trace
        await collector.complete_trace(trace.trace_id, "Demo completed successfully")
        
        # Print results
        print("\n--- Telemetry Results ---")
        trace_data = collector.get_trace(trace.trace_id)
        if trace_data:
            print(f"Trace ID: {trace_data['trace_id']}")
            print(f"Duration: {trace_data['total_duration_ms']:.2f}ms")
            print(f"Agent Count: {trace_data['agent_count']}")
            print(f"LLM Calls: {trace_data['llm_call_count']}")
        
        spans = collector.get_spans(trace.trace_id)
        print(f"\nSpans ({len(spans)}):")
        for span in spans:
            print(f"  - [{span['span_kind']}] {span['agent_name']}: {span.get('duration_ms', 0):.2f}ms")
        
    finally:
        await collector.stop()


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Multi-Agent Telemetry Demo")
    parser.add_argument("--simple", action="store_true", help="Run simplified demo without LLM")
    args = parser.parse_args()
    
    if args.simple:
        asyncio.run(run_simple_demo())
    else:
        asyncio.run(run_demo())
