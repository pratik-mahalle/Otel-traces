"""
Sample agent that emits telemetry spans and traces to Kafka.
Designed for validating end-to-end telemetry flow in Kubernetes.
"""

import asyncio
import logging
import os
from typing import Optional

from src.telemetry.collector import TelemetryCollector
from src.telemetry.schemas import AgentSpan, SpanKind, KAFKA_TOPICS

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
            trace = collector.create_trace(
                user_input=f"sample task {iteration}",
                session_id=f"sample-session-{agent_id}",
                user_id="sample-user",
                metadata={"agent": agent_name, "iteration": iteration}
            )

            span = AgentSpan(
                trace_id=trace.trace_id,
                agent_id=agent_id,
                agent_name=agent_name,
                span_kind=SpanKind.AGENT
            )
            span.input_data = {"iteration": iteration, "note": "sample work"}

            # Emit a running span first (to show active tasks)
            await collector._send(
                KAFKA_TOPICS["spans"],
                span.span_id,
                span.to_dict()
            )
            await collector.emit_event(
                trace_id=trace.trace_id,
                span_id=span.span_id,
                event_type="agent_heartbeat",
                agent_id=agent_id,
                agent_name=agent_name,
                message=f"{agent_name} started work",
                data={"iteration": iteration}
            )

            await asyncio.sleep(work_seconds)

            span.complete(output={"status": "ok", "iteration": iteration})
            await collector.record_span(span)

            if target_agent:
                await collector.record_handoff(
                    trace_id=trace.trace_id,
                    source_agent_id=agent_id,
                    source_agent_name=agent_name,
                    target_agent_id=target_agent.lower().replace(" ", "-"),
                    target_agent_name=target_agent,
                    reason="Sample handoff",
                    context={"iteration": iteration}
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
