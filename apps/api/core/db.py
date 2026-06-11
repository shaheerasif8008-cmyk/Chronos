from collections.abc import AsyncIterator

from sqlalchemy import MetaData, Table
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from core.config import settings

_connect_args: dict = {}
if settings.db_ssl_mode:
    # asyncpg accepts sslmode strings ("require", "verify-full", …) on the `ssl` arg.
    _connect_args["ssl"] = settings.db_ssl_mode

engine = create_async_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
    connect_args=_connect_args,
)
metadata = MetaData()


async def get_conn() -> AsyncIterator[AsyncConnection]:
    async with engine.begin() as conn:
        yield conn


# Schema is static at runtime, so reflected Table objects are cached by name to
# avoid a fresh connection + full schema round-trip on every hot-path query.
_TABLE_CACHE: dict[str, Table] = {}


async def reflect_table(name: str) -> Table:
    cached = _TABLE_CACHE.get(name)
    if cached is not None:
        return cached
    table_metadata = MetaData()
    async with engine.begin() as conn:
        table = await conn.run_sync(
            lambda sync_conn: Table(name, table_metadata, autoload_with=sync_conn)
        )
    _TABLE_CACHE[name] = table
    return table
