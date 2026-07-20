from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from types import ModuleType

import httpx
import pytest


def test_composio_sdk_check_requires_the_runtime_client_symbol(monkeypatch):
    from connectors import composio_client

    partial = ModuleType("composio")
    monkeypatch.setitem(sys.modules, "composio", partial)
    assert composio_client._sdk_available() is False

    partial.Composio = type("Composio", (), {})
    assert composio_client._sdk_available() is True


@pytest.fixture(autouse=True)
def _reset_connector_health(monkeypatch):
    from core import connector_health

    connector_health._CACHE = None
    connector_health._LAST_VERIFIED.clear()
    monkeypatch.setattr(connector_health.settings, "browserbase_api_key", "")
    monkeypatch.setattr(connector_health.settings, "browserbase_operator_enabled", False)
    monkeypatch.setattr(connector_health.settings, "browserbase_project_id", "")
    monkeypatch.setattr(connector_health.settings, "tavily_api_key", "")
    monkeypatch.setattr(connector_health.settings, "e2b_api_key", "")
    monkeypatch.setattr(connector_health.settings, "composio_api_key", "")

    async def no_playwright():
        return False, "Playwright is not installed."

    monkeypatch.setattr(connector_health, "_playwright_available", no_playwright)


@pytest.mark.asyncio
async def test_browserbase_key_is_not_live_until_read_only_probe_succeeds(monkeypatch):
    from core import connector_health

    monkeypatch.setattr(connector_health.settings, "browserbase_api_key", "bb-secret")

    async def rejected():
        return connector_health.ProbeResult(
            ok=False,
            checked_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
            latency_ms=14,
            error_code="auth_rejected",
        )

    monkeypatch.setattr(connector_health, "_probe_browserbase", rejected)
    health = await connector_health.check_connectors(refresh=True)

    assert health["browserbase"] == {
        "status": "error",
        "tier": "degraded",
        "configured": True,
        "verified": False,
        "checked_at": "2026-07-12T12:00:00Z",
        "verified_at": None,
        "stale": True,
        "latency_ms": 14,
        "error_code": "auth_rejected",
        "reason": "Browserbase rejected the configured credential.",
        "setup": "Replace or correct the credential, then refresh verification.",
    }
    assert health["browser"]["status"] == "error"
    assert "bb-secret" not in repr(health)


@pytest.mark.asyncio
async def test_production_browser_operator_health_requires_verified_remote_project(monkeypatch):
    from core import connector_health

    monkeypatch.setattr(connector_health.settings, "environment", "production")
    monkeypatch.setattr(connector_health.settings, "browserbase_api_key", "bb-secret")
    monkeypatch.setattr(connector_health.settings, "browserbase_operator_enabled", True)
    monkeypatch.setattr(connector_health.settings, "browserbase_project_id", "project-1")

    async def accepted():
        return connector_health.ProbeResult(
            ok=True,
            checked_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
            latency_ms=11,
        )

    monkeypatch.setattr(connector_health, "_probe_browserbase", accepted)
    health = await connector_health.check_connectors(refresh=True)

    assert health["browser_operator"]["status"] == "verified"
    assert health["browser_operator"]["provider"] == "browserbase"
    assert "encrypted Contexts" in health["browser_operator"]["reason"]
    assert "bb-secret" not in repr(health)


@pytest.mark.asyncio
async def test_e2b_failure_retains_last_verified_timestamp_and_becomes_stale(monkeypatch):
    from core import connector_health

    checked_at = datetime(2026, 7, 12, 12, 10, tzinfo=timezone.utc)
    connector_health._LAST_VERIFIED["e2b"] = checked_at - timedelta(minutes=10)
    monkeypatch.setattr(connector_health.settings, "environment", "production")
    monkeypatch.setattr(connector_health.settings, "e2b_api_key", "e2b-secret")
    monkeypatch.setattr(connector_health.settings, "e2b_computer_egress_allowlist", "github.com")

    async def timed_out():
        return connector_health.ProbeResult(
            ok=False,
            checked_at=checked_at,
            latency_ms=5000,
            error_code="timeout",
        )

    monkeypatch.setattr(connector_health, "_probe_e2b", timed_out)
    health = await connector_health.check_connectors(refresh=True)

    assert health["e2b"]["status"] == "degraded"
    assert health["e2b"]["verified"] is False
    assert health["e2b"]["verified_at"] == "2026-07-12T12:00:00Z"
    assert health["e2b"]["stale"] is True
    assert health["computer"]["status"] == "degraded"
    assert health["code"]["tier"] == "degraded"
    assert "e2b-secret" not in repr(health)


@pytest.mark.asyncio
async def test_composio_probe_verifies_control_plane_not_user_account(monkeypatch):
    from connectors import composio_client
    from core import connector_health

    monkeypatch.setattr(connector_health.settings, "composio_api_key", "cmp-secret")
    monkeypatch.setattr(composio_client, "is_configured", lambda: True)

    async def accepted():
        return connector_health.ProbeResult(
            ok=True,
            checked_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
            latency_ms=7,
        )

    monkeypatch.setattr(connector_health, "_probe_composio", accepted)
    health = await connector_health.check_connectors(refresh=True)

    assert health["composio"]["status"] == "verified"
    assert health["composio"]["stale"] is False
    assert health["gmail"]["status"] == "verified"
    assert health["gmail"]["auth"] == "composio_managed"
    assert "still needs an active connected account" in health["gmail"]["reason"]
    assert "cmp-secret" not in repr(health)


@pytest.mark.asyncio
async def test_http_probe_maps_provider_error_without_reading_or_returning_body(monkeypatch):
    from core import connector_health

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers, params):
            assert headers == {"Authorization": "secret-value"}
            return httpx.Response(
                401,
                text="credential secret-value rejected",
                request=httpx.Request("GET", url, params=params),
            )

    monkeypatch.setattr(connector_health.httpx, "AsyncClient", lambda **_kwargs: FakeClient())
    result = await connector_health._http_get_probe(
        url="https://provider.example/health",
        headers={"Authorization": "secret-value"},
        params={"limit": 1},
    )

    assert result.error_code == "auth_rejected"
    assert "secret-value" not in repr(result)


@pytest.mark.asyncio
async def test_unconfigured_providers_do_not_run_external_probes(monkeypatch):
    from core import connector_health

    async def should_not_run():
        raise AssertionError("unconfigured provider probe ran")

    monkeypatch.setattr(connector_health, "_probe_browserbase", should_not_run)
    monkeypatch.setattr(connector_health, "_probe_e2b", should_not_run)
    monkeypatch.setattr(connector_health, "_probe_composio", should_not_run)

    health = await connector_health.check_connectors(refresh=True)

    assert health["browserbase"]["error_code"] == "not_configured"
    assert health["e2b"]["error_code"] == "not_configured"
    assert health["composio"]["error_code"] == "not_configured"


@pytest.mark.asyncio
async def test_manual_refresh_is_rate_bounded(monkeypatch):
    from core import connector_health

    calls = 0
    monkeypatch.setattr(connector_health.settings, "browserbase_api_key", "bb-secret")

    async def accepted():
        nonlocal calls
        calls += 1
        return connector_health.ProbeResult(
            ok=True,
            checked_at=datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc),
            latency_ms=3,
        )

    monkeypatch.setattr(connector_health, "_probe_browserbase", accepted)

    first = await connector_health.check_connectors(refresh=True)
    second = await connector_health.check_connectors(refresh=True)

    assert calls == 1
    assert second is first
