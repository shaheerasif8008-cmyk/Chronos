from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

import pytest
from sqlalchemy import delete, insert, select, update

from core.db import engine, reflect_table
from core.models import ToolResult
from jobs import monitor_polling


def _monitor(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "00000000-0000-0000-0000-000000000001",
        "organization_id": "org-1",
        "created_by": "member-1",
        "name": "Release notes",
        "monitor_type": "website",
        "target": "https://example.com/releases",
        "condition": {"operator": "changed"},
        "source_config": {},
        "interval_seconds": 900,
    }
    row.update(overrides)
    return row


def test_monitor_errors_redact_provider_urls_and_credentials() -> None:
    message = monitor_polling._safe_error_summary(  # noqa: SLF001 - focused safety regression
        "provider https://user:pass@example.com/private api_key=sk_live_supersecret Bearer abc.def"  # gitleaks:allow - redaction fixture only
    )

    assert "example.com" not in message
    assert "supersecret" not in message
    assert "abc.def" not in message
    assert "[redacted-url]" in message
    assert "api_key=[redacted]" in message


def test_connector_monitor_rejects_mutating_tools_and_methods() -> None:
    monitor_polling.validate_read_only_tool("gmail.search", {"query": "is:unread"})

    with pytest.raises(ValueError, match="read-only"):
        monitor_polling.validate_read_only_tool("gmail.send", {"to": "client@example.com"})
    with pytest.raises(ValueError, match="read-only"):
        monitor_polling.validate_read_only_tool("gmail.draft_message", {"query": "is:unread"})
    with pytest.raises(ValueError, match="GET or HEAD"):
        monitor_polling.validate_read_only_tool("custom.http", {"method": "POST"})
    with pytest.raises(ValueError, match="cannot contain credentials"):
        monitor_polling.validate_read_only_tool(
            "custom.http_get", {"headers": {"Authorization": "Bearer secret"}}
        )


def test_changed_condition_requires_a_real_previous_baseline() -> None:
    observation = {"hash": "new", "_match_text": "release"}

    assert monitor_polling._condition_matches(_monitor(), observation) == (False, False)  # noqa: SLF001
    assert monitor_polling._condition_matches(_monitor(content_hash="old"), observation) == (True, True)  # noqa: SLF001
    assert monitor_polling._condition_matches(  # noqa: SLF001
        _monitor(content_hash="old", condition={"operator": "contains", "value": "release"}),
        observation,
    ) == (True, True)


@pytest.mark.asyncio
async def test_website_poll_ignores_proxy_environment_and_rechecks_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Any]] = []

    class FakeResponse:
        status_code = 200
        is_redirect = False
        encoding = "utf-8"
        headers = {"content-type": "text/html", "etag": '"v1"'}

        async def aiter_bytes(self):
            yield b"<html><title>Releases</title><body>Version 2 is live</body></html>"

    class FakeStream:
        async def __aenter__(self) -> FakeResponse:
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(("client", kwargs))

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        def stream(self, method: str, url: str, *, headers: dict[str, str]) -> FakeStream:
            calls.append(("request", (method, url, headers)))
            return FakeStream()

    def safe_url(url: str) -> str:
        calls.append(("ssrf", url))
        return url

    monkeypatch.setattr(monitor_polling, "assert_safe_url", safe_url)
    monkeypatch.setattr(monitor_polling.httpx, "AsyncClient", FakeClient)

    observed = await monitor_polling.collect_observation(_monitor())

    assert [kind for kind, _ in calls[:3]] == ["ssrf", "client", "request"]
    assert calls[1][1]["trust_env"] is False
    assert calls[1][1]["follow_redirects"] is False
    assert observed["title"] == "Releases"
    assert observed["hash"] == hashlib.sha256(b"Releases Version 2 is live").hexdigest()
    assert observed["untrusted_content"]["trusted"] is False


@pytest.mark.asyncio
async def test_news_monitor_reports_provider_degradation_without_fixture_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unavailable(*_args: Any, **_kwargs: Any) -> ToolResult:
        return ToolResult(
            data={"tier": "unavailable", "is_unavailable": True, "results": []},
            summary="Live search is unavailable",
        )

    monkeypatch.setattr(monitor_polling.tool_broker, "execute", unavailable)

    with pytest.raises(monitor_polling.MonitorPollError) as caught:
        await monitor_polling.collect_observation(
            _monitor(monitor_type="news", target="Chronos release news")
        )

    assert caught.value.code == "provider_degraded"
    assert caught.value.degraded is True
    assert caught.value.retryable is False


def test_monitor_migration_follows_admin_head_and_has_durable_dedupe_contract() -> None:
    migration = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0062_monitor_polling.py"
    ).read_text()
    admin_migration = Path(__file__).parents[1] / "migrations" / "versions" / "0061_admin_lifecycle.py"

    assert admin_migration.exists()
    assert 'down_revision = "0061_admin_lifecycle"' in migration
    assert 'sa.UniqueConstraint("monitor_id", "run_key"' in migration
    assert '"uq_monitor_alerts_dedupe"' in migration
    assert '"next_attempt_at"' in migration
    assert '"lease_expires_at"' in migration


@pytest.mark.asyncio
async def test_postgres_claims_runs_and_alerts_are_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = f"monitor-proof-{uuid.uuid4().hex}"
    monitors = await reflect_table("monitors")
    runs = await reflect_table("monitor_runs")
    alerts = await reflect_table("monitor_alerts")

    async def no_op(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def observation(_monitor: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": "https://example.com/releases",
            "snippet": "Version 2 is live",
            "_match_text": "Version 2 is live",
            "hash": "release-v2",
            "untrusted_content": {"trusted": False, "risk": "external_content"},
        }

    monkeypatch.setattr(monitor_polling.audit, "log", no_op)
    monkeypatch.setattr(monitor_polling.notifications, "emit", no_op)
    monkeypatch.setattr(monitor_polling, "collect_observation", observation)
    now = datetime.now(timezone.utc)
    try:
        async with engine.begin() as conn:
            monitor_id = await conn.scalar(
                insert(monitors)
                .values(
                    organization_id=org_id,
                    region="us",
                    name="Idempotency proof",
                    monitor_type="website",
                    target="https://example.com/releases",
                    condition={"operator": "changed"},
                    status="active",
                    next_run_at=now,
                    alert_cooldown_seconds=0,
                    created_by="member-1",
                )
                .returning(monitors.c.id)
            )
        monitor_id_text = str(monitor_id)

        baseline = await monitor_polling.run_monitor_now(
            monitor_id_text,
            org_id,
            actor_id="member-1",
            idempotency_key="same-request",
        )
        assert baseline["status"] == "baseline"
        with pytest.raises(monitor_polling.MonitorPollError, match="already accepted"):
            await monitor_polling.run_monitor_now(
                monitor_id_text,
                org_id,
                actor_id="member-1",
                idempotency_key="same-request",
            )

        first_claim = await monitor_polling._claim_monitor(  # noqa: SLF001 - concurrency proof
            monitor_id_text, org_id, now=now, require_due=False
        )
        assert first_claim is not None
        second_claim = await monitor_polling._claim_monitor(  # noqa: SLF001
            monitor_id_text, org_id, now=now, require_due=False
        )
        assert second_claim is None
        await monitor_polling._release_monitor(monitor_id_text, first_claim[1])  # noqa: SLF001

        for key in ("change-1", "change-2"):
            async with engine.begin() as conn:
                await conn.execute(
                    update(monitors)
                    .where(monitors.c.id == monitor_id, monitors.c.organization_id == org_id)
                    .values(content_hash="release-v1")
                )
            await monitor_polling.run_monitor_now(
                monitor_id_text,
                org_id,
                actor_id="member-1",
                idempotency_key=key,
            )

        async with engine.begin() as conn:
            run_count = await conn.scalar(
                select(monitor_polling.func.count()).select_from(runs).where(
                    runs.c.organization_id == org_id
                )
            )
            alert_count = await conn.scalar(
                select(monitor_polling.func.count()).select_from(alerts).where(
                    alerts.c.organization_id == org_id
                )
            )
        assert run_count == 3
        assert alert_count == 1
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(alerts).where(alerts.c.organization_id == org_id))
            await conn.execute(delete(runs).where(runs.c.organization_id == org_id))
            await conn.execute(delete(monitors).where(monitors.c.organization_id == org_id))


@pytest.mark.asyncio
async def test_retry_resumes_same_run_then_dead_letters(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id = f"monitor-retry-{uuid.uuid4().hex}"
    monitors = await reflect_table("monitors")
    runs = await reflect_table("monitor_runs")

    async def no_op(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def fail(_monitor: dict[str, Any]) -> dict[str, Any]:
        raise monitor_polling.MonitorPollError(
            "upstream_unavailable", "Provider unavailable", retryable=True
        )

    monkeypatch.setattr(monitor_polling.audit, "log", no_op)
    monkeypatch.setattr(monitor_polling, "collect_observation", fail)
    try:
        async with engine.begin() as conn:
            monitor_id = await conn.scalar(
                insert(monitors)
                .values(
                    organization_id=org_id,
                    region="us",
                    name="Retry proof",
                    monitor_type="website",
                    target="https://example.com/releases",
                    condition={"operator": "changed"},
                    status="active",
                    max_attempts=2,
                    next_run_at=datetime.now(timezone.utc),
                    created_by="member-1",
                )
                .returning(monitors.c.id)
            )
        monitor_id_text = str(monitor_id)
        first = await monitor_polling.run_monitor_now(
            monitor_id_text, org_id, actor_id="member-1", idempotency_key="retry-proof"
        )
        assert first["status"] == "retry"

        async with engine.begin() as conn:
            retry_row = (
                await conn.execute(
                    select(runs).where(
                        runs.c.organization_id == org_id,
                        runs.c.monitor_id == monitor_id,
                    )
                )
            ).mappings().one()
        claim = await monitor_polling._claim_monitor(  # noqa: SLF001 - retry ownership proof
            monitor_id_text, org_id, now=datetime.now(timezone.utc), require_due=False
        )
        assert claim is not None
        terminal = await monitor_polling._execute_claimed(  # noqa: SLF001
            claim[0],
            claim[1],
            trigger_source="manual",
            retry_run=dict(retry_row),
        )
        assert terminal is not None
        assert terminal["status"] == "dead_letter"
        assert terminal["attempt"] == 2

        async with engine.begin() as conn:
            rows = (
                await conn.execute(select(runs).where(runs.c.organization_id == org_id))
            ).mappings().all()
        assert len(rows) == 1
        assert rows[0]["status"] == "dead_letter"
        assert rows[0]["attempt"] == 2
    finally:
        async with engine.begin() as conn:
            await conn.execute(delete(runs).where(runs.c.organization_id == org_id))
            await conn.execute(delete(monitors).where(monitors.c.organization_id == org_id))
