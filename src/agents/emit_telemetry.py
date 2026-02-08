"""
Sample agent that demonstrates inter-agent communication via Kafka queues.
Periodically sends messages to a target agent's queue and records errors.
Designed for validating end-to-end flow in Kubernetes.
"""

import asyncio
import logging
import os
import random

from src.telemetry.collector import TelemetryCollector
from src.telemetry.schemas import (
    AgentSpan, SpanKind,
    AgentMessage, MessageType, MessagePriority, agent_queue_topic
)

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


async def emit_loop():
    agent_name = os.getenv("AGENT_NAME", "SampleAgent")
    agent_id = os.getenv("AGENT_ID", agent_name.lower().replace(" ", "-"))
    target_agent = os.getenv("TARGET_AGENT_NAME")
    kafka_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    environment = os.getenv("ENVIRONMENT", "development")
    work_seconds = _env_int("AGENT_WORK_SECONDS", 20)
    interval_seconds = _env_int("AGENT_INTERVAL_SECONDS", 10)

    collector = TelemetryCollector(
        kafka_bootstrap_servers=kafka_servers,
        service_name=f"agent-{agent_id}",
        environment=environment
    )
    await collector.start()

    logger.info(
        "Sample agent started",
        extra={
            "agent_name": agent_name,
            "agent_id": agent_id,
            "kafka": kafka_servers
        }
    )

    iteration = 0
    try:
        while True:
            # Create an internal trace for tracking
            trace = collector.create_trace(
                user_input=f"sample task {iteration}",
                session_id=f"sample-session-{agent_id}",
                user_id="sample-user",
                metadata={"agent": agent_name, "iteration": iteration}
            )

            # Use the span context manager (tracked internally, not sent to Kafka)
            async with collector.span(
                trace_id=trace.trace_id,
                agent_id=agent_id,
                agent_name=agent_name,
                span_kind=SpanKind.AGENT
            ) as span:
                span.input_data = {"iteration": iteration, "note": "sample work"}
                logger.info(f"{agent_name} working on iteration {iteration}")
                await asyncio.sleep(work_seconds)
                span.output_data = {"status": "ok", "iteration": iteration}

            # Send a message to target agent's queue (inter-agent communication)
            if target_agent:
                msg = AgentMessage(
                    source_agent=agent_name,
                    target_agent=target_agent,
                    message_type=MessageType.TASK,
                    priority=MessagePriority.NORMAL,
                    payload={"task": "sample_work", "iteration": iteration},
                    trace_id=trace.trace_id,
                    span_id=span.span_id,
                    reason=f"Sample task from {agent_name} iteration {iteration}"
                )
                await collector.send_to_agent(msg)

            # Occasionally simulate an error (sent to Kafka errors topic)
            if random.random() < 0.1:
                try:
                    raise RuntimeError(
                        f"Simulated error in {agent_name} at iteration {iteration}"
                    )
                except Exception as e:
                    await collector.record_error_from_exception(
                        exception=e,
                        trace_id=trace.trace_id,
                        agent_name=agent_name,
                        agent_id=agent_id,
                        operation=f"sample_work_iteration_{iteration}"
                    )

            await collector.complete_trace(
                trace.trace_id,
                output=f"{agent_name} completed iteration {iteration}"
            )

            iteration += 1
            await asyncio.sleep(interval_seconds)
    finally:
        await collector.stop()


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(emit_loop())


if __name__ == "__main__":
    main()
