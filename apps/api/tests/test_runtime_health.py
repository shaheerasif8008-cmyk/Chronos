from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest

import main
from core import runtime_health
from core.auth import create_access_token
from core.db import engine, reflect_table


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    )


async def _member(role: str) -> tuple[str, dict[str, str]]:
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    subdomain = f"o{org_id[:8]}"
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            organizations.insert().values(
                id=org_id,
                slug=subdomain,
                subdomain=subdomain,
                name="Runtime health test",
            )
        )
        await conn.execute(
            members.insert().values(
                id=member_id,
                organization_id=org_id,
                email=f"{member_id[:8]}@runtime.test",
                role=role,
            )
        )
    token = create_access_token(member_id, org_id=org_id)
    return org_id, {
        "Authorization": f"Bearer {token}",
        "X-Chronos-Org": subdomain,
    }


def _report(*, can_admin: bool) -> dict:
    required = [
        {
            "id": "database",
            "label": "Database",
            "required": True,
            "status": "healthy",
            "summary": "The database is healthy.",
        }
    ]
    optional = [
        {
            "id": "email_delivery",
            "label": "Email delivery",
            "required": False,
            "status": "unavailable",
            "summary": "Email delivery is not configured.",
            **(
                {"remediation": "Configure a verified notification sender."}
                if can_admin
                else {}
            ),
        }
    ]
    return {
        "status": "degraded",
        "can_complete_onboarding": True,
        "environment": "test",
        "checked_at": "2026-07-12T12:00:00Z",
        "required": required,
        "optional": optional,
        "blockers": [],
        "summary": {
            "required_healthy": 1,
            "required_total": 1,
            "optional_degraded": 1,
            "optional_total": 1,
        },
        "admin_actions_available": can_admin,
    }


@pytest.mark.asyncio
async def test_runtime_report_separates_required_from_optional_and_redacts_actions(
    monkeypatch,
):
    class FakeConnection:
        async def execute(self, _statement):
            return None

    class FakeBegin:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, *_args):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    class FakeRedis:
        async def ping(self):
            return True

        async def scan_iter(self, *, match, count):
            assert match.endswith("*")
            assert count == 100
            yield b"redacted-worker-key"

        async def get(self, _key):
            return str(time.time()).encode()

    async def storage_ready():
        return None

    async def authorization_ready():
        return True

    async def scanner_ready():
        return {"healthy": True, "engine": "clamav", "version": "ClamAV test"}

    verified = {
        "status": "verified",
        "tier": "live",
        "configured": True,
        "verified": True,
        "stale": False,
        "reason": "Provider credentials passed verification.",
    }
    unavailable = {
        "status": "unavailable",
        "tier": "unavailable",
        "configured": False,
        "verified": False,
        "stale": True,
        "reason": "Provider is not configured.",
        "setup": "Configure the provider credential.",
    }

    async def providers(*, refresh):
        assert refresh is True
        return {
            "openrouter": verified,
            "e2b": unavailable,
            "computer": unavailable,
            "browser_operator": unavailable,
            "repo": unavailable,
            "composio": unavailable,
        }

    monkeypatch.setattr(runtime_health, "engine", FakeEngine())
    monkeypatch.setattr(runtime_health, "redis_client", FakeRedis())
    monkeypatch.setattr(runtime_health, "check_bucket", storage_ready)
    monkeypatch.setattr(runtime_health.authz, "is_enabled", lambda: True)
    monkeypatch.setattr(runtime_health.authz, "healthcheck", authorization_ready)
    monkeypatch.setattr(runtime_health, "scanner_health", scanner_ready)
    monkeypatch.setattr(runtime_health, "check_connectors", providers)
    monkeypatch.setattr(
        runtime_health.notification_delivery, "email_is_configured", lambda: False
    )
    monkeypatch.setattr(runtime_health.billing, "is_configured", lambda: False)
    monkeypatch.setattr(runtime_health.settings, "environment", "production")
    monkeypatch.setattr(runtime_health.settings, "auth_provider", "cognito")
    monkeypatch.setattr(runtime_health.settings, "cognito_user_pool_id", "pool")
    monkeypatch.setattr(runtime_health.settings, "cognito_app_client_id", "client")
    monkeypatch.setattr(runtime_health.settings, "cognito_domain", "login.example.test")
    monkeypatch.setattr(runtime_health.settings, "langfuse_public_key", "")
    monkeypatch.setattr(runtime_health.settings, "langfuse_secret_key", "")
    monkeypatch.setattr(runtime_health.settings, "sentry_dsn", "")

    admin_report = await runtime_health.build_runtime_health_report(
        can_admin=True, refresh_providers=True
    )
    member_report = await runtime_health.build_runtime_health_report(
        can_admin=False, refresh_providers=True
    )

    assert admin_report["can_complete_onboarding"] is True
    assert admin_report["status"] == "degraded"
    assert all(item["status"] == "healthy" for item in admin_report["required"])
    assert any(item["status"] == "unavailable" for item in admin_report["optional"])
    assert any("remediation" in item for item in admin_report["optional"])
    assert all("remediation" not in item for item in member_report["required"])
    assert all("remediation" not in item for item in member_report["optional"])
    serialized = json.dumps(member_report)
    assert "redacted-worker-key" not in serialized
    assert "DATABASE_URL" not in serialized


@pytest.mark.asyncio
async def test_runtime_health_is_authenticated_and_admin_refresh_is_audited(
    monkeypatch,
):
    org_id, headers = await _member("admin")
    calls: list[dict[str, bool]] = []

    async def report(**kwargs):
        calls.append(kwargs)
        return _report(can_admin=kwargs["can_admin"])

    monkeypatch.setattr(
        "routers.settings.runtime_health.build_runtime_health_report", report
    )
    async with _client() as client:
        response = await client.get(
            "/settings/runtime-health?refresh=true", headers=headers
        )

    assert response.status_code == 200
    payload = response.json()
    assert calls == [{"can_admin": True, "refresh_providers": True}]
    assert payload["optional"][0]["remediation"].startswith("Configure")

    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                audit_log.select().where(
                    audit_log.c.organization_id == org_id,
                    audit_log.c.event_type == "runtime_health_refreshed",
                )
            )
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["payload"] == {"status": "degraded"}


@pytest.mark.asyncio
async def test_runtime_health_keeps_non_admin_status_useful_but_hides_remediation(
    monkeypatch,
):
    _, headers = await _member("viewer")

    async def report(**kwargs):
        return _report(can_admin=kwargs["can_admin"])

    monkeypatch.setattr(
        "routers.settings.runtime_health.build_runtime_health_report", report
    )
    async with _client() as client:
        response = await client.get("/settings/runtime-health", headers=headers)
        refresh = await client.get(
            "/settings/runtime-health?refresh=true", headers=headers
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["can_complete_onboarding"] is True
    assert payload["optional"][0]["status"] == "unavailable"
    assert "remediation" not in payload["optional"][0]
    assert refresh.status_code == 403


@pytest.mark.asyncio
async def test_runtime_health_rejects_anonymous_callers():
    async with _client() as client:
        response = await client.get("/settings/runtime-health")

    assert response.status_code == 401
