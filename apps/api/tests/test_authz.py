"""OpenFGA adapter tests — no live server; httpx layer (_request) is mocked.

These prove the adapter speaks the OpenFGA Check/Write protocol correctly and
that store/model bootstrap is resolved-or-created exactly once.
"""
import asyncio
from contextlib import asynccontextmanager
import pytest
from unittest.mock import AsyncMock

from core import authz


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    @asynccontextmanager
    async def unlocked():
        yield

    # Unit tests mock the HTTP boundary and should not require PostgreSQL. Live
    # enforcement tests exercise the real advisory lock against the CI database.
    monkeypatch.setattr(authz, "_bootstrap_advisory_lock", unlocked)
    authz.reset_cache()
    yield
    authz.reset_cache()


def test_model_defines_expected_types():
    types = {t["type"] for t in authz.AUTHORIZATION_MODEL["type_definitions"]}
    assert {"user", "organization", "project", "workspace", "task", "conversation"} <= types
    for resource in ("project", "workspace", "task", "conversation"):
        td = next(t for t in authz.AUTHORIZATION_MODEL["type_definitions"] if t["type"] == resource)
        assert {"can_view", "can_edit", "can_manage"} <= set(td["relations"])
    project = next(
        item for item in authz.AUTHORIZATION_MODEL["type_definitions"]
        if item["type"] == "project"
    )
    related = project["metadata"]["relations"]["organization_viewer"]
    assert related["directly_related_user_types"] == [
        {"type": "organization", "relation": "member"}
    ]


@pytest.mark.asyncio
async def test_ensure_creates_store_and_model_when_unconfigured(monkeypatch):
    calls = []

    async def fake_request(method, path, json=None):
        calls.append((method, path))
        if method == "GET" and path.startswith("/stores?"):
            return {"stores": []}
        if method == "POST" and path == "/stores":
            return {"id": "store-1"}
        if method == "GET" and "/authorization-models?" in path:
            return {"authorization_models": []}
        if method == "POST" and path.endswith("/authorization-models"):
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
async def test_ensure_reuses_matching_model_instead_of_writing(monkeypatch):
    calls = []
    matching = {"id": "model-existing", **authz.AUTHORIZATION_MODEL}

    async def fake_request(method, path, json=None):
        calls.append((method, path))
        if method == "GET" and path.startswith("/stores?"):
            return {"stores": [{"id": "store-existing", "name": "chronos"}]}
        if method == "GET" and "/authorization-models?" in path:
            return {"authorization_models": [matching]}
        raise AssertionError(f"unexpected write: {method} {path}")

    monkeypatch.setattr(authz.settings, "openfga_api_url", "http://fga")
    monkeypatch.setattr(authz.settings, "openfga_store_id", "")
    monkeypatch.setattr(authz.settings, "openfga_model_id", "")
    monkeypatch.setattr(authz, "_request", fake_request)

    assert await authz.ensure_store_and_model() == (
        "store-existing",
        "model-existing",
    )
    assert not any(method == "POST" for method, _ in calls)


@pytest.mark.asyncio
async def test_concurrent_bootstrap_creates_one_store_and_model(monkeypatch):
    """Model a shared datastore while several callers cold-start together."""
    stores: list[dict] = []
    models: list[dict] = []
    lock = asyncio.Lock()

    @asynccontextmanager
    async def serialized():
        async with lock:
            yield

    async def fake_request(method, path, json=None):
        if method == "GET" and path.startswith("/stores?"):
            return {"stores": list(stores)}
        if method == "POST" and path == "/stores":
            stores.append({"id": "store-1", "name": "chronos"})
            return {"id": "store-1"}
        if method == "GET" and "/authorization-models?" in path:
            return {"authorization_models": list(models)}
        if method == "POST" and path.endswith("/authorization-models"):
            models.append({"id": "model-1", **json})
            return {"authorization_model_id": "model-1"}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(authz.settings, "openfga_api_url", "http://fga")
    monkeypatch.setattr(authz.settings, "openfga_store_id", "")
    monkeypatch.setattr(authz.settings, "openfga_model_id", "")
    monkeypatch.setattr(authz, "_bootstrap_advisory_lock", serialized)
    monkeypatch.setattr(authz, "_request", fake_request)

    results = await asyncio.gather(*(authz.ensure_store_and_model() for _ in range(8)))
    assert results == [("store-1", "model-1")] * 8
    assert len(stores) == 1
    assert len(models) == 1


def test_request_headers_use_configured_preshared_token(monkeypatch):
    monkeypatch.setattr(authz.settings, "openfga_api_token", "secret-token")
    assert authz._request_headers() == {"Authorization": "Bearer secret-token"}


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
