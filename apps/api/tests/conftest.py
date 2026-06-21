import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
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

    The same cross-loop hazard applies to the global async Redis client (e.g. a
    test that exercises the ToolBroker's rate-limit/loop-detection paths), so we
    also disconnect its connection pool here. The pool is recreated lazily on the
    next test's loop.
    """
    yield
    from core.db import engine

    await engine.dispose()

    try:
        from core.redis import redis_client

        await redis_client.connection_pool.disconnect()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _isolate_provisioning_context(tmp_path, monkeypatch):
    """Redirect provision_org's context-folder writes to a temp dir so tests
    never pollute apps/api/context/ with uuid-named org folders."""
    try:
        monkeypatch.setattr("core.provisioning.ROOT", tmp_path)
    except (ImportError, AttributeError):
        pass  # module not imported yet in suites that don't touch provisioning
    yield
