"""
Telemetry Collector - Kafka Integration
Collects and streams telemetry data for multi-agent debugging
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
    AgentSpan, AgentTrace, AgentHandoff, DebugEvent,
    KafkaMessage, KAFKA_TOPICS, AgentStatus, SpanKind
)

logger = logging.getLogger(__name__)


class TelemetryCollector:
    """
    Main telemetry collector for multi-agent systems.
    Sends telemetry data to Kafka for real-time debugging.
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
        """Complete a trace and send it to Kafka"""
        if trace_id not in self._active_traces:
            logger.warning(f"Trace {trace_id} not found")
            return
            
        trace = self._active_traces[trace_id]
        trace.complete(output)
        
        await self._send(
            KAFKA_TOPICS["traces"],
            trace_id,
            trace.to_dict(),
            immediate=True
        )
        
        # Emit debug event
        await self.emit_event(
            trace_id=trace_id,
            event_type="trace_complete",
            message=f"Trace completed with {trace.agent_count} agents",
            data={
                "duration_ms": trace.total_duration_ms,
                "total_tokens": trace.total_tokens,
                "agent_count": trace.agent_count
            }
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
        
        # Emit start event
        await self.emit_event(
            trace_id=trace_id,
            span_id=span.span_id,
            event_type=f"{span_kind.value}_start",
            agent_id=agent_id,
            agent_name=agent_name,
            message=f"Started {span_kind.value}: {agent_name}"
        )
        
        try:
            yield span
            span.complete()
        except Exception as e:
            span.set_error(e)
            await self.emit_event(
                trace_id=trace_id,
                span_id=span.span_id,
                event_type="error",
                severity="error",
                agent_id=agent_id,
                agent_name=agent_name,
                message=f"Error in {agent_name}: {str(e)}",
                data={"error_type": type(e).__name__, "error_stack": span.error_stack}
            )
            raise
        finally:
            # Add span to trace
            if trace_id in self._active_traces:
                self._active_traces[trace_id].add_span(span)
            
            # Send span to Kafka
            await self._send(
                KAFKA_TOPICS["spans"],
                span.span_id,
                span.to_dict()
            )
            
            # Emit end event
            await self.emit_event(
                trace_id=trace_id,
                span_id=span.span_id,
                event_type=f"{span_kind.value}_end",
                agent_id=agent_id,
                agent_name=agent_name,
                message=f"Completed {span_kind.value}: {agent_name}",
                data={"duration_ms": span.duration_ms, "status": span.status.value}
            )
            
            del self._active_spans[span.span_id]
    
    async def record_span(self, span: AgentSpan):
        """Manually record a span"""
        if span.trace_id in self._active_traces:
            self._active_traces[span.trace_id].add_span(span)
            
        await self._send(
            KAFKA_TOPICS["spans"],
            span.span_id,
            span.to_dict()
        )
    
    # Handoff Recording
    
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
        """Record a handoff between agents"""
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
        
        await self._send(
            KAFKA_TOPICS["handoffs"],
            handoff.handoff_id,
            handoff.to_dict(),
            immediate=True
        )
        
        await self.emit_event(
            trace_id=trace_id,
            event_type="handoff",
            message=f"Handoff: {source_agent_name} -> {target_agent_name}",
            data={
                "reason": reason,
                "source_agent": source_agent_name,
                "target_agent": target_agent_name
            }
        )
    
    # Debug Events
    
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
        """Emit a debug event for real-time debugging"""
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
        
        await self._send(
            KAFKA_TOPICS["events"],
            event.event_id,
            event.to_dict(),
            immediate=True
        )
        
        # Notify local handlers
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
    
    async def consume_events(self, callback: Callable[[Dict[str, Any]], None]):
        """Consume events for real-time debugging"""
        consumer = await self.subscribe(
            [KAFKA_TOPICS["events"]],
            group_id="debug-event-consumer"
        )
        
        async for message in consumer:
            try:
                await callback(message.value)
            except Exception as e:
                logger.error(f"Error processing event: {e}")


class InMemoryTelemetryCollector(TelemetryCollector):
    """
    In-memory telemetry collector for testing and local development
    without Kafka dependency.
    """
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._messages: Dict[str, List[Dict[str, Any]]] = {
            topic: [] for topic in KAFKA_TOPICS.values()
        }
    
    async def start(self):
        """No-op for in-memory collector"""
        logger.info("In-memory telemetry collector started")
    
    async def stop(self):
        """No-op for in-memory collector"""
        logger.info("In-memory telemetry collector stopped")
    
    async def _send(self, topic: str, key: str, value: Dict[str, Any], immediate: bool = False):
        """Store message in memory"""
        self._messages[topic].append({
            "key": key,
            "value": value,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Notify handlers
        if topic == KAFKA_TOPICS["events"]:
            event_type = value.get("event_type")
            if event_type in self._event_handlers:
                for handler in self._event_handlers[event_type]:
                    try:
                        if asyncio.iscoroutinefunction(handler):
                            await handler(DebugEvent(**value))
                        else:
                            handler(DebugEvent(**value))
                    except Exception as e:
                        logger.error(f"Event handler error: {e}")
    
    def get_messages(self, topic: str) -> List[Dict[str, Any]]:
        """Get all messages for a topic"""
        return self._messages.get(topic, [])
    
    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """Get a completed trace by ID"""
        for msg in self._messages[KAFKA_TOPICS["traces"]]:
            if msg["key"] == trace_id:
                return msg["value"]
        return None
    
    def get_spans(self, trace_id: str) -> List[Dict[str, Any]]:
        """Get all spans for a trace"""
        return [
            msg["value"]
            for msg in self._messages[KAFKA_TOPICS["spans"]]
            if msg["value"].get("trace_id") == trace_id
        ]
    
    def clear(self):
        """Clear all messages"""
        for topic in self._messages:
            self._messages[topic].clear()
