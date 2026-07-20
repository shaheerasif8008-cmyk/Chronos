from __future__ import annotations

from collections import Counter

import httpx
import pytest


def test_application_has_no_duplicate_route_registrations():
    import main

    route_keys = [
        (route.path, tuple(sorted(route.methods or ())))
        for route in main.app.routes
        if getattr(route, "methods", None)
    ]
    assert [key for key, count in Counter(route_keys).items() if count > 1] == []


@pytest.mark.asyncio
async def test_api_responses_include_browser_security_headers():
    import main

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://api.localhost",
    ) as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_public_share_responses_are_never_cached_indexed_or_referred(monkeypatch):
    import main
    from routers import artifact_share

    async def missing(_token: str):
        return None

    monkeypatch.setattr(artifact_share, "get_active_share_by_token", missing)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://api.localhost",
    ) as client:
        response = await client.get("/shared/not-a-real-token")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"


@pytest.mark.asyncio
async def test_health_does_not_call_the_model(monkeypatch):
    """The unauthenticated /health probe must never trigger a billed model call.

    A model completion costs money and ties readiness to a third party, so an
    anonymous caller must not be able to drive it. The model probe lives on the
    admin-only /health/deep endpoint instead.
    """
    import litellm

    import main

    async def _fail(*_args, **_kwargs):
        raise AssertionError("/health must not call the model provider")

    monkeypatch.setattr(litellm, "acompletion", _fail)

    result = await main.health()

    assert "model" not in result["checks"]
    assert result["status"] in {"ok", "degraded"}


@pytest.mark.asyncio
async def test_openfga_healthcheck_requires_serving_datastore(monkeypatch):
    from core import authz

    monkeypatch.setattr(authz.settings, "permissions_enforce", True)
    monkeypatch.setattr(authz.settings, "openfga_api_url", "http://openfga.internal:8080")

    async def serving(method, path, json=None):
        assert (method, path, json) == ("GET", "/healthz", None)
        return {"status": "SERVING"}

    monkeypatch.setattr(authz, "_request", serving)
    assert await authz.healthcheck() is True

    async def not_serving(_method, _path, json=None):
        return {"status": "NOT_SERVING"}

    monkeypatch.setattr(authz, "_request", not_serving)
    assert await authz.healthcheck() is False


@pytest.mark.asyncio
async def test_connector_worker_heartbeat_is_unique_and_expiring(monkeypatch):
    from connectors import worker_main

    captured = {}

    class FakeRedis:
        async def set(self, key, value, *, ex):
            captured.update({"key": key, "value": value, "ex": ex})

    monkeypatch.setattr(worker_main, "redis_client", FakeRedis())
    await worker_main._publish_heartbeat("worker-1")

    assert captured["key"] == f"{worker_main.WORKER_HEARTBEAT_PREFIX}worker-1"
    assert captured["ex"] == worker_main.WORKER_HEARTBEAT_TTL_SECONDS
    assert float(captured["value"]) > 0


@pytest.mark.asyncio
async def test_liveness_does_not_probe_dependencies(monkeypatch):
    import main

    async def _fail():
        raise AssertionError("liveness must not probe dependencies")

    monkeypatch.setattr(main, "_core_health_checks", _fail)

    assert (await main.health_live())["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checks", "expected_status", "expected_body"),
    [
        ({"postgres": "ok", "redis": "ok", "s3": "ok"}, 200, "ok"),
        ({"postgres": "error", "redis": "ok", "s3": "ok"}, 503, "degraded"),
    ],
)
async def test_readiness_uses_http_status(monkeypatch, checks, expected_status, expected_body):
    import json

    import main

    async def _checks():
        return checks

    monkeypatch.setattr(main, "_core_health_checks", _checks)
    response = await main.readiness()

    assert response.status_code == expected_status
    assert json.loads(response.body)["status"] == expected_body
