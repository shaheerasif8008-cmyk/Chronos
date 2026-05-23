from __future__ import annotations

import asyncio
import signal

from connectors.framework.adapters import adapter_registry
from connectors.framework.queue_factory import connector_execution_queue
from connectors.framework.repository import DatabaseConnectorRepository
from connectors.framework.tracing import ExecutionTracer
from connectors.framework.worker import ConnectorWorker


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    repo = DatabaseConnectorRepository()
    worker = ConnectorWorker(repo, adapter_registry(), connector_execution_queue(), tracer=ExecutionTracer(repo))
    while not stop.is_set():
        await worker.run_once()
        await asyncio.sleep(0.05)


if __name__ == "__main__":
    asyncio.run(main())
