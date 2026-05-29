import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _reset_db_engine(event_loop):
    """Dispose the SQLAlchemy async engine after each test so the next test
    gets a fresh connection pool bound to its own event loop.  Without this,
    asyncpg futures created in test-N's loop are reused in test-N+1's loop,
    producing "Future attached to a different loop" errors.

    We use a *sync* fixture that receives the event_loop fixture (function-scoped)
    so teardown runs while that loop is still open.
    """
    yield
    try:
        from core.db import engine
        event_loop.run_until_complete(engine.dispose())
    except Exception:
        pass
