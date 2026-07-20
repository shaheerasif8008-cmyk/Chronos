"""Leader-only recovery and retention for the connector write outbox."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from connectors.framework.queue_factory import connector_execution_queue
from connectors.framework.repository import DatabaseConnectorRepository
from core.connector_write_ledger import recover_framework_outbox


scheduler = AsyncIOScheduler()


async def recover_connector_writes() -> dict[str, int]:
    return await recover_framework_outbox(
        DatabaseConnectorRepository(), connector_execution_queue(), limit=100
    )


scheduler.add_job(
    recover_connector_writes,
    "interval",
    seconds=30,
    id="connector-write-outbox-recovery",
    max_instances=1,
    coalesce=True,
)
