"""OpenFGA adapter tests — no live server; httpx layer (_request) is mocked.

These prove the adapter speaks the OpenFGA Check/Write protocol correctly and
that store/model bootstrap is resolved-or-created exactly once.
"""
import pytest
from unittest.mock import AsyncMock

from core import authz


@pytest.fixture(autouse=True)
def _reset():
    authz.reset_cache()
    yield
    authz.reset_cache()


def test_model_defines_expected_types():
    types = {t["type"] for t in authz.AUTHORIZATION_MODEL["type_definitions"]}
    assert {"user", "organization", "project"} <= types
    project = next(t for t in authz.AUTHORIZATION_MODEL["type_definitions"] if t["type"] == "project")
    assert {"can_view", "can_edit", "can_manage"} <= set(project["relations"])


@pytest.mark.asyncio
async def test_ensure_creates_store_and_model_when_unconfigured(monkeypatch):
    calls = []

    async def fake_request(method, path, json=None):
        calls.append((method, path))
        if method == "GET" and path == "/stores":
            return {"stores": []}
        if path == "/stores":
            return {"id": "store-1"}
        if path.endswith("/authorization-models"):
            return {"authorization_model_id": "model-1"}
        return {}

    monkeypatch.setattr(authz.settings, "openfga_api_url", "http://fga")
    monkeypatch.setattr(authz.settings, "openfga_store_id", "")
    monkeypatch.setattr(authz.settings, "openfga_model_id", "")
    monkeypatch.setattr(authz, "_request", fake_request)

    store_id, model_id = await authz.ensure_store_and_model()
    assert (store_id, model_id) == ("store-1", "model-1")
    # Cached: a second call performs no further HTTP work.
    calls.clear()
    await authz.ensure_store_and_model()
    assert calls == []


@pytest.mark.asyncio
async def test_check_builds_tuple_and_returns_allowed(monkeypatch):
    seen = {}

    async def fake_request(method, path, json=None):
        if path.endswith("/check"):
            seen["check"] = json
            return {"allowed": True}
        return {"id": "store-1", "authorization_model_id": "model-1", "stores": []}

    monkeypatch.setattr(authz.settings, "openfga_api_url", "http://fga")
    monkeypatch.setattr(authz.settings, "openfga_store_id", "store-1")
    monkeypatch.setattr(authz.settings, "openfga_model_id", "model-1")
    monkeypatch.setattr(authz, "_request", fake_request)

    allowed = await authz.check("user:alice", "can_view", "project:p1")
    assert allowed is True
    assert seen["check"]["tuple_key"] == {
        "user": "user:alice",
        "relation": "can_view",
        "object": "project:p1",
    }


@pytest.mark.asyncio
async def test_check_unavailable_raises(monkeypatch):
    async def boom(*a, **k):
        raise authz.AuthzUnavailable("down")

    monkeypatch.setattr(authz.settings, "openfga_api_url", "http://fga")
    monkeypatch.setattr(authz.settings, "openfga_store_id", "store-1")
    monkeypatch.setattr(authz.settings, "openfga_model_id", "model-1")
    monkeypatch.setattr(authz, "_request", boom)

    with pytest.raises(authz.AuthzUnavailable):
        await authz.check("user:alice", "can_view", "project:p1")


@pytest.mark.asyncio
async def test_write_tuples_noop_on_empty(monkeypatch):
    req = AsyncMock()
    monkeypatch.setattr(authz, "_request", req)
    await authz.write_tuples([])
    assert req.await_count == 0
