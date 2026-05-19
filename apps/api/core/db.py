from collections.abc import AsyncIterator

from sqlalchemy import MetaData, Table
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from core.config import settings

engine = create_async_engine(settings.database_url, future=True, pool_pre_ping=True)
metadata = MetaData()


async def get_conn() -> AsyncIterator[AsyncConnection]:
    async with engine.begin() as conn:
        yield conn


async def reflect_table(name: str) -> Table:
    table_metadata = MetaData()
    async with engine.begin() as conn:
        return await conn.run_sync(lambda sync_conn: Table(name, table_metadata, autoload_with=sync_conn))
