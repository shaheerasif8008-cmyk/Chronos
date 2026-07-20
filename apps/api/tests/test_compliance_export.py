"""Production compliance bundle: complete categories, tenant scope, redaction, proof."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import insert, select

from core import audit, compliance_export
from core.artifacts import read_artifact_content
from core.db import engine, reflect_table
from core.models import Member
from routers.compliance import CreateComplianceExport, create_export


def _member(org_id: str, role: str = "admin") -> Member:
    ident = str(uuid.uuid4())
    return Member(
        id=ident,
        organization_id=org_id,
        email=f"{ident[:8]}@example.com",
        role=role,
    )


async def _seed_org(org_id: str) -> None:
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(
            insert(organizations).values(
                id=org_id,
                slug=f"compliance-{org_id[:8]}",
                name="Compliance fixture",
            )
        )


@pytest.mark.asyncio
async def test_compliance_export_is_tenant_scoped_redacted_durable_and_verifiable(monkeypatch):
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    await _seed_org(org_a)
    await _seed_org(org_b)
    admin = _member(org_a)

    async def unavailable_storage(*_args, **_kwargs):
        raise RuntimeError("force deterministic local artifact fallback")

    monkeypatch.setattr("core.artifacts.put_object", unavailable_storage)

    tasks = await reflect_table("tasks")
    approvals = await reflect_table("approvals")
    connectors = await reflect_table("connectors")
    task_id = str(uuid.uuid4())
    foreign_task_id = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            insert(tasks),
            [
                {
                    "id": task_id,
                    "organization_id": org_a,
                    "region": "us",
                    "triggered_by": "manual",
                    "status": "complete",
                    "goal": "use sk_live_never-export-this",
                    "triggered_by_member_id": None,
                },
                {
                    "id": foreign_task_id,
                    "organization_id": org_b,
                    "region": "us",
                    "triggered_by": "manual",
                    "status": "complete",
                    "goal": "FOREIGN_TENANT_SENTINEL",
                    "triggered_by_member_id": None,
                },
            ],
        )
        await conn.execute(
            insert(approvals).values(
                organization_id=org_a,
                region="us",
                task_id=task_id,
                step_id="governed-write",
                action_type="custom_http.write",
                action_payload={"authorization": "Bearer should-never-export", "safe": "digest-me"},
                status="approved",
                decided_by=admin.id,
            )
        )
        await conn.execute(
            insert(connectors).values(
                id=str(uuid.uuid4()),
                organization_id=org_a,
                region="us",
                provider="github",
                account_handle="release@example.com",
                vault_ref="vault:raw-secret-reference",
                status="active",
                scopes=["repo:read"],
                name="GitHub production",
                description="",
                type="native",
                auth_type="oauth2",
            )
        )

    await audit.log(
        "connector_access",
        admin.id,
        "connector.github.read",
        organization_id=org_a,
        resource_type="connector",
        resource_id="github",
        payload={
            "api_key": "sk_live_supersecret",  # gitleaks:allow -- redaction fixture
            "safe": "visible-metadata",
            "nested": {"authorization": "Bearer abc.def.ghi"},
        },
    )
    await audit.log(
        "task",
        "foreign",
        "task.foreign",
        organization_id=org_b,
        resource_type="task",
        resource_id=foreign_task_id,
        payload={"sentinel": "FOREIGN_AUDIT_SENTINEL"},
    )

    response = await create_export(CreateComplianceExport(), member=admin)
    artifact_id = response["artifact_id"]
    assert response["download_path"] == f"/artifacts/{artifact_id}/content"
    raw = await read_artifact_content(artifact_id)
    assert raw is not None
    text = raw.decode()
    bundle = json.loads(text)

    assert bundle["manifest"]["organization_id"] == org_a
    assert bundle["manifest"]["record_count"] == len(bundle["events"])
    assert set(bundle["manifest"]["category_counts"]) == compliance_export.ALLOWED_CATEGORIES
    assert compliance_export.verify_bundle(bundle)
    assert "connector_access" in {event["category"] for event in bundle["events"]}
    assert "approvals" in {event["category"] for event in bundle["events"]}
    assert "task_execution" in {event["category"] for event in bundle["events"]}

    for forbidden in (
        "sk_live_supersecret",  # gitleaks:allow -- redaction assertion fixture
        "should-never-export",
        "vault:raw-secret-reference",
        "FOREIGN_TENANT_SENTINEL",
        "FOREIGN_AUDIT_SENTINEL",
    ):
        assert forbidden not in text
    assert "[REDACTED]" not in text  # secrets are omitted or hashed, never echoed

    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        meta = (
            await conn.execute(select(artifacts).where(artifacts.c.id == artifact_id))
        ).mappings().one()
    assert meta["organization_id"] == org_a
    assert meta["created_by"] == f"member:{admin.id}"
    assert meta["mime_type"] == "application/json"


@pytest.mark.asyncio
async def test_compliance_export_role_floor_denies_regular_member():
    org_id = str(uuid.uuid4())
    await _seed_org(org_id)
    with pytest.raises(HTTPException) as exc:
        await create_export(CreateComplianceExport(), member=_member(org_id, "user"))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_audit_append_boundary_redacts_nested_credentials():
    org_id = str(uuid.uuid4())
    await _seed_org(org_id)
    event_id = await audit.log(
        "security",
        "tester",
        "credential.redaction",
        organization_id=org_id,
        payload={
            "client_secret": "never-store-me",
            "nested": {"password": "also-never", "safe": "kept"},
            "header": "Bearer opaque-token-value",
        },
    )
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(audit_log.c.payload).where(audit_log.c.id == event_id))
        ).scalar_one()
    assert row["client_secret"] == "[REDACTED]"
    assert row["nested"] == {"password": "[REDACTED]", "safe": "kept"}
    assert row["header"] == "[REDACTED]"
