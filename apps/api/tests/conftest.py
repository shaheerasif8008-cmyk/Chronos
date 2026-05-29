import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest_asyncio


@pytest_asyncio.fixture(autouse=True)
async def _reset_db_engine():
    """Dispose the SQLAlchemy async engine after each test so the next test gets a
    fresh connection pool bound to its own event loop. Without this, asyncpg
    futures created in test-N's loop are reused in test-N+1's loop, producing
    "Future attached to a different loop" errors.

    Async autouse fixture (function-scoped loop via pytest.ini's
    asyncio_default_fixture_loop_scope=function) so teardown awaits dispose on
    the same loop the test used — no reliance on the removed ``event_loop`` fixture.
    """
    yield
    from core.db import engine

    await engine.dispose()
