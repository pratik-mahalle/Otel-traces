"""
LiteLLM Integration for Multi-Agent Telemetry
Wraps LiteLLM calls with automatic telemetry collection
"""

import asyncio
import json
import logging
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Union

import litellm
from litellm import acompletion, completion
from litellm.utils import ModelResponse

from .schemas import (
    AgentSpan, TokenUsage, LLMMessage, MessageRole,
    SpanKind, ToolDefinition, ToolInvocation
)
from .collector import TelemetryCollector

logger = logging.getLogger(__name__)


class TracedLiteLLM:
    """
    LiteLLM wrapper with automatic telemetry collection.
    Tracks all LLM calls, token usage, and tool invocations.
    """
    
    def __init__(
        self,
        collector: TelemetryCollector,
        default_model: str = "gpt-3.5-turbo",
        default_provider: str = "openai"
    ):
        self.collector = collector
        self.default_model = default_model
        self.default_provider = default_provider
        
        # Cost tracking (approximate costs per 1K tokens)
        self.model_costs = {
            "gpt-4": {"input": 0.03, "output": 0.06},
            "gpt-4-turbo": {"input": 0.01, "output": 0.03},
            "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
            "claude-3-opus": {"input": 0.015, "output": 0.075},
            "claude-3-sonnet": {"input": 0.003, "output": 0.015},
            "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
            "ollama/llama2": {"input": 0.0, "output": 0.0},
            "ollama/mistral": {"input": 0.0, "output": 0.0},
        }
    
    def _calculate_cost(self, model: str, token_usage: TokenUsage) -> float:
        """Calculate estimated cost for the LLM call"""
        costs = self.model_costs.get(model, {"input": 0.001, "output": 0.002})
        return (
            (token_usage.prompt_tokens / 1000) * costs["input"] +
            (token_usage.completion_tokens / 1000) * costs["output"]
        )
    
    def _parse_messages(self, messages: List[Dict[str, Any]]) -> List[LLMMessage]:
        """Parse messages into LLMMessage objects"""
        parsed = []
        for msg in messages:
            role = MessageRole(msg.get("role", "user"))
            parsed.append(LLMMessage(
                role=role,
                content=msg.get("content", ""),
                name=msg.get("name"),
                tool_call_id=msg.get("tool_call_id"),
                tool_calls=msg.get("tool_calls")
            ))
        return parsed
    
    def _parse_tools(self, tools: Optional[List[Dict[str, Any]]]) -> List[ToolDefinition]:
        """Parse tools into ToolDefinition objects"""
        if not tools:
            return []
        
        parsed = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                parsed.append(ToolDefinition(
                    name=func.get("name", ""),
                    description=func.get("description", ""),
                    parameters=func.get("parameters", {})
                ))
        return parsed
    
    def _extract_token_usage(self, response: ModelResponse) -> TokenUsage:
        """Extract token usage from LiteLLM response"""
        usage = response.usage
        return TokenUsage(
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
            total_tokens=getattr(usage, "total_tokens", 0),
            cached_tokens=getattr(usage, "cached_tokens", 0) if hasattr(usage, "cached_tokens") else 0
        )
    
    def _extract_tool_calls(self, response: ModelResponse) -> List[ToolInvocation]:
        """Extract tool calls from response"""
        invocations = []
        
        if response.choices and len(response.choices) > 0:
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            
            if tool_calls:
                for tc in tool_calls:
                    func = tc.function
                    invocations.append(ToolInvocation(
                        tool_id=tc.id,
                        tool_name=func.name,
                        input_parameters=json.loads(func.arguments) if func.arguments else {}
                    ))
        
        return invocations
    
    async def completion(
        self,
        trace_id: str,
        agent_id: str,
        agent_name: str,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs
    ) -> ModelResponse:
        """
        Make an LLM completion call with automatic telemetry.
        
        Args:
            trace_id: The trace ID for this interaction
            agent_id: ID of the calling agent
            agent_name: Name of the calling agent
            messages: List of messages for the completion
            model: LLM model to use (defaults to self.default_model)
            parent_span_id: Parent span ID for nested calls
            tools: Optional list of tools available to the model
            **kwargs: Additional arguments passed to LiteLLM
        
        Returns:
            LiteLLM ModelResponse
        """
        model = model or self.default_model
        
        async with self.collector.span(
            trace_id=trace_id,
            agent_id=agent_id,
            agent_name=agent_name,
            span_kind=SpanKind.LLM_CALL,
            parent_span_id=parent_span_id,
            model_name=model,
            model_provider=self.default_provider,
            messages=self._parse_messages(messages),
            tools_available=self._parse_tools(tools),
            temperature=kwargs.get("temperature"),
            max_tokens=kwargs.get("max_tokens")
        ) as span:
            
            # Emit event before LLM call
            await self.collector.emit_event(
                trace_id=trace_id,
                span_id=span.span_id,
                event_type="llm_request",
                agent_id=agent_id,
                agent_name=agent_name,
                message=f"LLM request to {model}",
                data={
                    "model": model,
                    "message_count": len(messages),
                    "has_tools": bool(tools)
                }
            )
            
            # Make the actual LLM call
            start_time = datetime.utcnow()
            response = await acompletion(
                model=model,
                messages=messages,
                tools=tools,
                **kwargs
            )
            latency_ms = (datetime.utcnow() - start_time).total_seconds() * 1000
            
            # Extract telemetry data
            token_usage = self._extract_token_usage(response)
            tool_invocations = self._extract_tool_calls(response)
            
            # Update span with response data
            span.token_usage = token_usage
            span.tool_invocations = tool_invocations
            
            if response.choices and len(response.choices) > 0:
                span.response = response.choices[0].message.content
            
            # Calculate cost
            cost = self._calculate_cost(model, token_usage)
            span.attributes["cost"] = cost
            span.attributes["latency_ms"] = latency_ms
            
            # Emit response event
            await self.collector.emit_event(
                trace_id=trace_id,
                span_id=span.span_id,
                event_type="llm_response",
                agent_id=agent_id,
                agent_name=agent_name,
                message=f"LLM response from {model}",
                data={
                    "model": model,
                    "latency_ms": latency_ms,
                    "tokens": token_usage.to_dict(),
                    "cost": cost,
                    "tool_calls": len(tool_invocations)
                }
            )
            
            return response
    
    def completion_sync(
        self,
        trace_id: str,
        agent_id: str,
        agent_name: str,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        **kwargs
    ) -> ModelResponse:
        """Synchronous version of completion"""
        return asyncio.run(self.completion(
            trace_id=trace_id,
            agent_id=agent_id,
            agent_name=agent_name,
            messages=messages,
            model=model,
            **kwargs
        ))
    
    async def stream_completion(
        self,
        trace_id: str,
        agent_id: str,
        agent_name: str,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        parent_span_id: Optional[str] = None,
        **kwargs
    ):
        """
        Stream an LLM completion with telemetry.
        Yields chunks as they arrive.
        """
        model = model or self.default_model
        
        span = AgentSpan(
            trace_id=trace_id,
            agent_id=agent_id,
            agent_name=agent_name,
            span_kind=SpanKind.LLM_CALL,
            parent_span_id=parent_span_id,
            model_name=model,
            model_provider=self.default_provider,
            messages=self._parse_messages(messages)
        )
        
        await self.collector.emit_event(
            trace_id=trace_id,
            span_id=span.span_id,
            event_type="llm_stream_start",
            agent_id=agent_id,
            agent_name=agent_name,
            message=f"Starting stream from {model}"
        )
        
        full_response = ""
        chunk_count = 0
        
        try:
            response = await acompletion(
                model=model,
                messages=messages,
                stream=True,
                **kwargs
            )
            
            async for chunk in response:
                chunk_count += 1
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, 'content') and delta.content:
                        full_response += delta.content
                        yield delta.content
            
            span.response = full_response
            span.complete()
            
            await self.collector.emit_event(
                trace_id=trace_id,
                span_id=span.span_id,
                event_type="llm_stream_end",
                agent_id=agent_id,
                agent_name=agent_name,
                message=f"Stream completed from {model}",
                data={
                    "chunk_count": chunk_count,
                    "response_length": len(full_response)
                }
            )
            
        except Exception as e:
            span.set_error(e)
            await self.collector.emit_event(
                trace_id=trace_id,
                span_id=span.span_id,
                event_type="llm_stream_error",
                severity="error",
                agent_id=agent_id,
                agent_name=agent_name,
                message=f"Stream error: {str(e)}"
            )
            raise
        finally:
            await self.collector.record_span(span)


def trace_llm_call(collector: TelemetryCollector):
    """
    Decorator for tracing LLM calls in agent methods.
    
    Usage:
        @trace_llm_call(collector)
        async def my_llm_method(self, messages):
            return await acompletion(model="gpt-4", messages=messages)
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Try to extract trace context from args
            self = args[0] if args else None
            trace_id = getattr(self, 'trace_id', None) or kwargs.get('trace_id', 'unknown')
            agent_id = getattr(self, 'agent_id', None) or kwargs.get('agent_id', 'unknown')
            agent_name = getattr(self, 'agent_name', None) or kwargs.get('agent_name', func.__name__)
            
            async with collector.span(
                trace_id=trace_id,
                agent_id=agent_id,
                agent_name=agent_name,
                span_kind=SpanKind.LLM_CALL
            ) as span:
                result = await func(*args, **kwargs)
                
                # Try to extract token usage if result is a ModelResponse
                if isinstance(result, ModelResponse):
                    usage = result.usage
                    span.token_usage = TokenUsage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0),
                        completion_tokens=getattr(usage, "completion_tokens", 0),
                        total_tokens=getattr(usage, "total_tokens", 0)
                    )
                    if result.choices:
                        span.response = result.choices[0].message.content
                
                return result
        
        return wrapper
    return decorator


class LiteLLMCallbackHandler:
    """
    Callback handler for LiteLLM that automatically logs telemetry.
    Can be used with LiteLLM's callback system.
    """
    
    def __init__(
        self,
        collector: TelemetryCollector,
        trace_id: str,
        agent_id: str,
        agent_name: str
    ):
        self.collector = collector
        self.trace_id = trace_id
        self.agent_id = agent_id
        self.agent_name = agent_name
        self._current_span: Optional[AgentSpan] = None
    
    async def log_pre_api_call(
        self,
        model: str,
        messages: List[Dict],
        kwargs: Dict
    ):
        """Called before API call"""
        self._current_span = AgentSpan(
            trace_id=self.trace_id,
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            span_kind=SpanKind.LLM_CALL,
            model_name=model,
            input_data={"messages": messages}
        )
        
        await self.collector.emit_event(
            trace_id=self.trace_id,
            span_id=self._current_span.span_id,
            event_type="llm_call_start",
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            message=f"Calling {model}"
        )
    
    async def log_post_api_call(
        self,
        kwargs: Dict,
        response_obj: ModelResponse,
        start_time: datetime,
        end_time: datetime
    ):
        """Called after API call"""
        if self._current_span:
            self._current_span.end_time = end_time
            self._current_span.duration_ms = (end_time - start_time).total_seconds() * 1000
            
            if response_obj.usage:
                self._current_span.token_usage = TokenUsage(
                    prompt_tokens=response_obj.usage.prompt_tokens,
                    completion_tokens=response_obj.usage.completion_tokens,
                    total_tokens=response_obj.usage.total_tokens
                )
            
            await self.collector.record_span(self._current_span)
            self._current_span = None
    
    async def log_failure(
        self,
        kwargs: Dict,
        exception: Exception,
        start_time: datetime
    ):
        """Called on API failure"""
        if self._current_span:
            self._current_span.set_error(exception)
            await self.collector.record_span(self._current_span)
            
            await self.collector.emit_event(
                trace_id=self.trace_id,
                span_id=self._current_span.span_id,
                event_type="llm_call_error",
                severity="error",
                agent_id=self.agent_id,
                agent_name=self.agent_name,
                message=f"LLM call failed: {str(exception)}"
            )
            
            self._current_span = None
