"""W5.1 — compliance-grade audit export.

Proves the audit export is *complete* (no silent 500-row truncation), honors a
date range / event-type filter, ships a manifest that proves completeness, is
itself audited, and stays tenant-scoped + admin-gated.

Requires DATABASE_URL pointing at a migrated Chronos database (defaults to the
local docker Postgres on :55432).
"""
import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, insert, select

from core.config import settings as app_settings
from core.db import engine, reflect_table
from core.models import Member
from routers import settings


async def _insert_org(org_id: str) -> None:
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(
            insert(orgs).values(id=org_id, slug=f"slug-{org_id}", name=f"Org {org_id}")
        )


def _member(org_id: str, role: str = "owner") -> Member:
    mid = f"m-{uuid.uuid4().hex[:8]}"
    return Member(id=mid, organization_id=org_id, email=f"{mid}@example.com", role=role)


async def _insert_audit_rows(org_id: str, rows: list[dict]) -> None:
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        await conn.execute(insert(audit_log), [
            {
                "organization_id": org_id,
                "region": app_settings.region,
                "event_type": r.get("event_type", "test"),
                "actor_id": r.get("actor_id", "system"),
                "action": r["action"],
                "resource_type": r.get("resource_type"),
                "resource_id": r.get("resource_id"),
                "payload": r.get("payload"),
                "decision": r.get("decision"),
                "created_at": r["created_at"],
            }
            for r in rows
        ])


async def _cleanup(org_ids: list[str]) -> None:
    # audit_log is append-only (DELETE blocked); rows are isolated by unique org id.
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(delete(orgs).where(orgs.c.id.in_(org_ids)))


async def _drain(streaming_response) -> str:
    parts = []
    async for chunk in streaming_response.body_iterator:
        parts.append(chunk if isinstance(chunk, str) else chunk.decode())
    return "".join(parts)


@pytest.mark.asyncio
async def test_list_audit_honors_date_range_and_event_type():
    org = f"orgA-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        admin = _member(org)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await _insert_audit_rows(org, [
            {"action": "old.event", "event_type": "settings", "created_at": base},
            {"action": "mid.event", "event_type": "compliance", "created_at": base + timedelta(days=10)},
            {"action": "new.event", "event_type": "settings", "created_at": base + timedelta(days=20)},
        ])
        # Date range excludes the rows outside [since, until).
        ranged = await settings.list_audit(
            since=(base + timedelta(days=5)).isoformat(),
            until=(base + timedelta(days=15)).isoformat(),
            limit=500, offset=0, member=admin,
        )
        actions = {r["action"] for r in ranged}
        assert "mid.event" in actions
        assert "old.event" not in actions and "new.event" not in actions
        # event_type filter.
        by_type = await settings.list_audit(event_type="compliance", limit=500, offset=0, member=admin)
        assert {r["action"] for r in by_type} == {"mid.event"}
        # tenant scoped.
        assert {r["organization_id"] for r in ranged} == {org}
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_export_is_complete_beyond_500_rows_with_manifest():
    org = f"orgB-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        admin = _member(org)
        base = datetime(2026, 2, 1, tzinfo=timezone.utc)
        n = 650  # > the old hard cap of 500
        await _insert_audit_rows(org, [
            {"action": f"evt.{i}", "event_type": "bulk", "created_at": base + timedelta(seconds=i)}
            for i in range(n)
        ])

        # JSON export: manifest count must equal the full row count, not 500.
        # Filter to the bulk rows so the export's own audit entry (written under
        # event_type "compliance") doesn't perturb the count across the two calls.
        resp = await settings.export_audit(format="json", event_type="bulk", member=admin)
        payload = json.loads(await _drain(resp))
        assert payload["manifest"]["count"] == n, "export truncated below complete row count"
        assert len(payload["records"]) == n
        assert payload["manifest"]["organization_id"] == org
        assert payload["manifest"]["generated_by"] == admin.id

        # CSV export: every row present + manifest echoed in headers.
        csv_resp = await settings.export_audit(format="csv", event_type="bulk", member=admin)
        text = await _drain(csv_resp)
        reader = list(csv.DictReader(io.StringIO(text)))
        assert len(reader) == n
        assert csv_resp.headers["X-Chronos-Audit-Export-Count"] == str(n)
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_export_records_an_audit_entry_for_the_export():
    org = f"orgC-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        admin = _member(org)
        base = datetime(2026, 3, 1, tzinfo=timezone.utc)
        await _insert_audit_rows(org, [
            {"action": "seed.event", "event_type": "settings", "created_at": base},
        ])
        await settings.export_audit(format="json", member=admin)
        audit_log = await reflect_table("audit_log")
        async with engine.begin() as conn:
            rows = (await conn.execute(
                select(audit_log).where(
                    audit_log.c.organization_id == org,
                    audit_log.c.action == "export_audit_log",
                )
            )).mappings().all()
        assert rows, "export did not write its own audit entry"
        assert rows[0]["actor_id"] == admin.id
    finally:
        await _cleanup([org])


@pytest.mark.asyncio
async def test_export_date_range_filters_rows():
    org = f"orgD-{uuid.uuid4().hex[:8]}"
    await _insert_org(org)
    try:
        admin = _member(org)
        base = datetime(2026, 4, 1, tzinfo=timezone.utc)
        await _insert_audit_rows(org, [
            {"action": "before", "created_at": base},
            {"action": "inside", "created_at": base + timedelta(days=3)},
            {"action": "after", "created_at": base + timedelta(days=9)},
        ])
        resp = await settings.export_audit(
            format="json",
            since=(base + timedelta(days=1)).isoformat(),
            until=(base + timedelta(days=5)).isoformat(),
            member=admin,
        )
        payload = json.loads(await _drain(resp))
        actions = {r["action"] for r in payload["records"]}
        assert actions == {"inside"}
        assert payload["manifest"]["count"] == 1
    finally:
        await _cleanup([org])
