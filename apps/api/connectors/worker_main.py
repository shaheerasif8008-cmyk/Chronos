from __future__ import annotations

import asyncio
import signal
import time
import uuid

from connectors.framework.adapters import adapter_registry
from connectors.framework.queue_factory import connector_execution_queue
from connectors.framework.repository import DatabaseConnectorRepository
from connectors.framework.tracing import ExecutionTracer
from connectors.framework.worker import ConnectorWorker
from core.redis import redis_client
from core.connector_write_ledger import recover_framework_outbox


WORKER_HEARTBEAT_PREFIX = "chronos:connector-worker:heartbeat:"
WORKER_HEARTBEAT_TTL_SECONDS = 30
WORKER_HEARTBEAT_INTERVAL_SECONDS = 10


async def _publish_heartbeat(worker_id: str) -> None:
    await redis_client.set(
        f"{WORKER_HEARTBEAT_PREFIX}{worker_id}",
        str(time.time()),
        ex=WORKER_HEARTBEAT_TTL_SECONDS,
    )


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    repo = DatabaseConnectorRepository()
    worker = ConnectorWorker(repo, adapter_registry(), connector_execution_queue(), tracer=ExecutionTracer(repo))
    worker_id = str(uuid.uuid4())
    last_heartbeat = 0.0
    last_recovery = 0.0
    while not stop.is_set():
        now = time.monotonic()
        if now - last_heartbeat >= WORKER_HEARTBEAT_INTERVAL_SECONDS:
            await _publish_heartbeat(worker_id)
            last_heartbeat = now
        if now - last_recovery >= 15:
            await recover_framework_outbox(repo, worker.queue, limit=100)
            last_recovery = now
        await worker.run_once()
        await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(main())
