"""
Telemetry Collector - Kafka Integration
Kafka is ONLY for inter-agent queues.
Agents do NOT write telemetry — the Telemetry API observer captures data
from Kafka queues and K8s pod states, then persists to JSONL files.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from contextlib import asynccontextmanager

from aiokafka import AIOKafkaProducer, AIOKafkaConsumer
from aiokafka.errors import KafkaError

from .schemas import (
    AgentSpan, AgentTrace, AgentHandoff, DebugEvent, DetailedError,
    KafkaMessage, AgentStatus, SpanKind,
    ErrorSeverity, ErrorCategory,
    AgentMessage, MessageType, MessagePriority,
    agent_queue_topic, AGENT_QUEUE_PREFIX
)

logger = logging.getLogger(__name__)


class TelemetryCollector:
    """
    Main telemetry collector for multi-agent systems.
    Kafka is used ONLY for inter-agent queues (agent-queue-{name}).
    All telemetry (errors, traces, spans) is stored internally — not in Kafka.
    """
    
    def __init__(
        self,
        kafka_bootstrap_servers: str = "localhost:9092",
        service_name: str = "multi-agent-system",
        environment: str = "development"
    ):
        self.kafka_servers = kafka_bootstrap_servers
        self.service_name = service_name
        self.environment = environment
        
        self._producer: Optional[AIOKafkaProducer] = None
        self._consumers: Dict[str, AIOKafkaConsumer] = {}
        self._active_traces: Dict[str, AgentTrace] = {}
        self._active_spans: Dict[str, AgentSpan] = {}
        
        # Event handlers for real-time debugging
        self._event_handlers: Dict[str, List[Callable]] = {}
        
        # Buffer for batch sending
        self._buffer: List[KafkaMessage] = []
        self._buffer_size = 100
        self._flush_interval = 1.0  # seconds
        
    async def start(self):
        """Initialize Kafka connections"""
        try:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self.kafka_servers,
                value_serializer=lambda v: json.dumps(v, default=str).encode('utf-8'),
                key_serializer=lambda k: k.encode('utf-8') if k else None,
                acks='all',
                enable_idempotence=True
            )
            await self._producer.start()
            logger.info(f"Telemetry collector started, connected to {self.kafka_servers}")
            
            # Start background flush task
            asyncio.create_task(self._periodic_flush())
            
        except KafkaError as e:
            logger.error(f"Failed to connect to Kafka: {e}")
            raise
    
    async def stop(self):
        """Cleanup Kafka connections"""
        if self._producer:
            await self._flush_buffer()
            await self._producer.stop()
            
        for consumer in self._consumers.values():
            await consumer.stop()
            
        logger.info("Telemetry collector stopped")
    
    async def _periodic_flush(self):
        """Periodically flush the buffer"""
        while True:
            await asyncio.sleep(self._flush_interval)
            await self._flush_buffer()
    
    async def _flush_buffer(self):
        """Flush buffered messages to Kafka"""
        if not self._buffer or not self._producer:
            return
            
        messages = self._buffer.copy()
        self._buffer.clear()
        
        for msg in messages:
            try:
                await self._producer.send(
                    msg.topic,
                    key=msg.key,
                    value=msg.value,
                    headers=[(k, v.encode()) for k, v in msg.headers.items()]
                )
            except KafkaError as e:
                logger.error(f"Failed to send message to Kafka: {e}")
    
    async def _send(self, topic: str, key: str, value: Dict[str, Any], immediate: bool = False):
        """Send a message to Kafka"""
        msg = KafkaMessage(
            topic=topic,
            key=key,
            value=value,
            headers={
                "service": self.service_name,
                "environment": self.environment,
                "timestamp": datetime.utcnow().isoformat()
            }
        )
        
        if immediate and self._producer:
            try:
                await self._producer.send_and_wait(
                    msg.topic,
                    key=msg.key,
                    value=msg.value,
                    headers=[(k, v.encode()) for k, v in msg.headers.items()]
                )
            except KafkaError as e:
                logger.error(f"Failed to send immediate message: {e}")
        else:
            self._buffer.append(msg)
            if len(self._buffer) >= self._buffer_size:
                await self._flush_buffer()
    
    # Trace Management
    
    def create_trace(
        self,
        user_input: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Dict[str, Any] = None
    ) -> AgentTrace:
        """Create a new trace for a multi-agent interaction"""
        trace = AgentTrace(
            session_id=session_id,
            user_id=user_id,
            user_input=user_input,
            metadata=metadata or {}
        )
        self._active_traces[trace.trace_id] = trace
        return trace
    
    async def complete_trace(self, trace_id: str, output: str = ""):
        """Complete a trace (tracked internally, not sent to Kafka)"""
        if trace_id not in self._active_traces:
            logger.warning(f"Trace {trace_id} not found")
            return
            
        trace = self._active_traces[trace_id]
        trace.complete(output)
        
        logger.info(
            f"Trace {trace_id} completed: {trace.agent_count} agents, "
            f"{trace.total_duration_ms:.0f}ms"
        )
        
        del self._active_traces[trace_id]
    
    # Span Management
    
    @asynccontextmanager
    async def span(
        self,
        trace_id: str,
        agent_id: str,
        agent_name: str,
        span_kind: SpanKind = SpanKind.AGENT,
        parent_span_id: Optional[str] = None,
        **kwargs
    ):
        """Context manager for creating and managing spans"""
        span = AgentSpan(
            trace_id=trace_id,
            agent_id=agent_id,
            agent_name=agent_name,
            span_kind=span_kind,
            parent_span_id=parent_span_id,
            **kwargs
        )
        self._active_spans[span.span_id] = span
        
        try:
            yield span
            span.complete()
        except Exception as e:
            span.set_error(e)
            
            # Send rich DetailedError to the dedicated errors topic
            await self.record_error_from_exception(
                exception=e,
                trace_id=trace_id,
                span_id=span.span_id,
                agent_name=agent_name,
                agent_id=agent_id,
                operation=f"{span_kind.value}:{agent_name}"
            )
            raise
        finally:
            # Track span internally (not sent to Kafka)
            if trace_id in self._active_traces:
                self._active_traces[trace_id].add_span(span)
            
            del self._active_spans[span.span_id]
    
    async def record_span(self, span: AgentSpan):
        """Manually record a span (tracked internally, not sent to Kafka)"""
        if span.trace_id in self._active_traces:
            self._active_traces[span.trace_id].add_span(span)
    
    # Error Recording (stored internally, NOT sent to Kafka)
    
    async def record_error(
        self,
        error: DetailedError,
        immediate: bool = True
    ):
        """
        Record a DetailedError internally.
        Agents do NOT persist errors — the Telemetry API observer captures
        them from Kafka queue traffic and pod logs, then writes to JSONL.
        """
        # Notify local error handlers
        if "error_recorded" in self._event_handlers:
            for handler in self._event_handlers["error_recorded"]:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(error)
                    else:
                        handler(error)
                except Exception as e:
                    logger.error(f"Error handler callback failed: {e}")
        
        logger.info(
            f"Recorded error {error.error_id}: [{error.severity.value}] "
            f"{error.category.value} - {error.error_message[:100]}"
        )
    
    async def record_error_from_exception(
        self,
        exception: Exception,
        trace_id: Optional[str] = None,
        span_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        agent_id: Optional[str] = None,
        operation: Optional[str] = None,
        **kwargs
    ) -> DetailedError:
        """
        Create a DetailedError from an exception and store it internally.
        Convenience method that auto-classifies the error.
        Returns the DetailedError for further use.
        """
        error = DetailedError.from_exception(
            exception,
            trace_id=trace_id,
            span_id=span_id,
            agent_name=agent_name,
            operation=operation,
            **kwargs
        )
        error.agent_id = agent_id
        error.service_name = self.service_name
        error.environment = self.environment
        
        await self.record_error(error)
        return error
    
    # Handoff Recording (internal tracking only, not sent to Kafka)
    
    async def record_handoff(
        self,
        trace_id: str,
        source_agent_id: str,
        source_agent_name: str,
        target_agent_id: str,
        target_agent_name: str,
        reason: str,
        context: Dict[str, Any] = None
    ):
        """Record a handoff between agents (tracked internally)"""
        handoff = AgentHandoff(
            trace_id=trace_id,
            source_agent_id=source_agent_id,
            source_agent_name=source_agent_name,
            target_agent_id=target_agent_id,
            target_agent_name=target_agent_name,
            reason=reason,
            context=context or {}
        )
        
        if trace_id in self._active_traces:
            self._active_traces[trace_id].add_handoff(handoff)
        
        logger.info(f"Handoff: {source_agent_name} -> {target_agent_name} ({reason})")
    
    # Debug Events (local handlers only, not sent to Kafka)
    
    async def emit_event(
        self,
        trace_id: str,
        event_type: str,
        message: str,
        span_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        severity: str = "info",
        data: Dict[str, Any] = None
    ):
        """Emit a debug event to local handlers (not sent to Kafka)"""
        event = DebugEvent(
            trace_id=trace_id,
            span_id=span_id,
            event_type=event_type,
            severity=severity,
            agent_id=agent_id,
            agent_name=agent_name,
            message=message,
            data=data or {}
        )
        
        # Notify local handlers only
        if event_type in self._event_handlers:
            for handler in self._event_handlers[event_type]:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event)
                    else:
                        handler(event)
                except Exception as e:
                    logger.error(f"Event handler error: {e}")
    
    def on_event(self, event_type: str, handler: Callable):
        """Register an event handler for debugging"""
        if event_type not in self._event_handlers:
            self._event_handlers[event_type] = []
        self._event_handlers[event_type].append(handler)
    
    # Consumer for Debug UI
    
    async def subscribe(
        self,
        topics: List[str],
        group_id: str = "debug-consumer"
    ) -> AIOKafkaConsumer:
        """Subscribe to telemetry topics for real-time debugging"""
        consumer = AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self.kafka_servers,
            group_id=group_id,
            value_deserializer=lambda v: json.loads(v.decode('utf-8')),
            auto_offset_reset='latest'
        )
        await consumer.start()
        self._consumers[group_id] = consumer
        return consumer
    
    # ========================================================================
    # Inter-Agent Communication via Kafka Queues
    # ========================================================================
    
    async def send_to_agent(
        self,
        message: AgentMessage,
        immediate: bool = True
    ):
        """
        Send a message to another agent's Kafka queue.
        
        Pattern:
            Agent1 calls send_to_agent(AgentMessage(target_agent="Researcher", ...))
            -> writes to Kafka topic "agent-queue-researcher"
            Researcher reads from "agent-queue-researcher"
        
        Also emits a telemetry event so the communication is observable.
        """
        topic = agent_queue_topic(message.target_agent)
        
        await self._send(
            topic,
            message.message_id,
            message.to_dict(),
            immediate=immediate
        )
        
        logger.info(
            f"Sent {message.message_type.value} to {topic}: "
            f"{message.source_agent} -> {message.target_agent}"
        )
    
    async def consume_agent_queue(
        self,
        agent_name: str,
        callback: Callable[[AgentMessage], Any],
        group_id: Optional[str] = None
    ):
        """
        Consume messages from an agent's own Kafka queue.
        
        Pattern:
            Agent2 calls consume_agent_queue("Researcher", handler)
            -> reads from Kafka topic "agent-queue-researcher"
        
        Each consumed message triggers a telemetry event for observability.
        """
        topic = agent_queue_topic(agent_name)
        consumer_group = group_id or f"agent-{agent_name.lower()}-consumer"
        
        consumer = await self.subscribe(
            [topic],
            group_id=consumer_group
        )
        
        logger.info(f"Agent '{agent_name}' consuming from queue: {topic}")
        
        async for raw_message in consumer:
            try:
                msg = AgentMessage.from_dict(raw_message.value)
                
                # Invoke the agent's handler
                if asyncio.iscoroutinefunction(callback):
                    await callback(msg)
                else:
                    callback(msg)
                    
            except Exception as e:
                logger.error(f"Error processing queue message for {agent_name}: {e}")
                # Record the processing error
                await self.record_error_from_exception(
                    exception=e,
                    agent_name=agent_name,
                    operation=f"consume_queue:{topic}"
                )


class InMemoryTelemetryCollector(TelemetryCollector):
    """
    In-memory telemetry collector for testing and local development
    without Kafka dependency.
    
    Stores inter-agent queue messages and errors in memory.
    Also writes to JSONL time-machine files (inherited from parent).
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)  # JSONL persistence inherited from parent
        # Internal error store (no Kafka)
        self._errors: List[Dict[str, Any]] = []
        # Inter-agent queue messages (keyed by topic)
        self._agent_queues: Dict[str, List[Dict[str, Any]]] = {}
        # Callbacks registered per agent for in-memory queue consumption
        self._queue_callbacks: Dict[str, Callable] = {}
    
    async def start(self):
        """No-op for in-memory collector"""
        logger.info("In-memory telemetry collector started")
    
    async def stop(self):
        """No-op for in-memory collector"""
        logger.info("In-memory telemetry collector stopped")
    
    async def _send(self, topic: str, key: str, value: Dict[str, Any], immediate: bool = False):
        """Store inter-agent queue messages in memory (only agent-queue-* topics use Kafka)"""
        if topic.startswith(AGENT_QUEUE_PREFIX):
            if topic not in self._agent_queues:
                self._agent_queues[topic] = []
            self._agent_queues[topic].append({
                "key": key,
                "value": value,
                "timestamp": datetime.utcnow().isoformat()
            })
            
            # If there is a registered callback for this queue, invoke it
            agent_name = topic.replace(AGENT_QUEUE_PREFIX, "")
            for registered_name, callback in self._queue_callbacks.items():
                if registered_name.lower() == agent_name:
                    try:
                        msg = AgentMessage.from_dict(value)
                        if asyncio.iscoroutinefunction(callback):
                            await callback(msg)
                        else:
                            callback(msg)
                    except Exception as e:
                        logger.error(f"In-memory queue callback error: {e}")
    
    async def consume_agent_queue(
        self,
        agent_name: str,
        callback: Callable[[AgentMessage], Any],
        group_id: Optional[str] = None
    ):
        """Register an in-memory callback for an agent's queue"""
        self._queue_callbacks[agent_name] = callback
        logger.info(f"In-memory queue consumer registered for agent '{agent_name}'")
    
    async def record_error(self, error: DetailedError, immediate: bool = True):
        """Store error in memory"""
        self._errors.append(error.to_dict())
        # Also call parent to trigger local handlers
        await super().record_error(error, immediate)
    
    def get_agent_queue_messages(self, agent_name: str) -> List[Dict[str, Any]]:
        """Get all messages in an agent's queue"""
        topic = agent_queue_topic(agent_name)
        return self._agent_queues.get(topic, [])
    
    def get_all_agent_queue_messages(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get all inter-agent queue messages grouped by topic"""
        return dict(self._agent_queues)
    
    def get_errors(self, trace_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all errors, optionally filtered by trace_id"""
        errors = list(self._errors)
        if trace_id:
            errors = [e for e in errors if e.get("trace_id") == trace_id]
        return errors
    
    def get_errors_by_severity(self, severity: str) -> List[Dict[str, Any]]:
        """Get errors filtered by severity level"""
        return [e for e in self._errors if e.get("severity") == severity]
    
    def get_errors_by_category(self, category: str) -> List[Dict[str, Any]]:
        """Get errors filtered by category"""
        return [e for e in self._errors if e.get("category") == category]
    
    def clear(self):
        """Clear all in-memory data (errors + agent queues)"""
        self._errors.clear()
        for topic in self._agent_queues:
            self._agent_queues[topic].clear()
