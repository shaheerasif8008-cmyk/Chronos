from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.sql import func

from core import audit, permissions
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member


AGENT_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "research",
        "name": "Research Analyst",
        "role": "research analyst",
        "description": "Source-grounded research, synthesis, and cited brief creation.",
        "tool_grants": ["web.search", "research.run", "artifact.write"],
        "connector_grants": ["google_drive", "notion", "slack"],
        "memory_scopes": ["project", "workspace"],
        "approval_policy": {"risky_writes": "require_approval", "external_replies": "require_approval"},
    },
    {
        "id": "executive_assistant",
        "name": "Executive Assistant",
        "role": "executive assistant",
        "description": "Calendar, inbox, briefing, and follow-up workflows with approvals.",
        "tool_grants": ["gmail.search", "calendar.read", "task.create"],
        "connector_grants": ["gmail", "google_calendar", "slack"],
        "memory_scopes": ["personal", "workspace"],
        "approval_policy": {"external_replies": "require_approval", "calendar_writes": "require_approval"},
    },
    {
        "id": "sales_sdr",
        "name": "Sales SDR",
        "role": "sales development representative",
        "description": "Account research, qualification, and approved outreach drafting.",
        "tool_grants": ["web.search", "connector.search", "gmail.draft"],
        "connector_grants": ["hubspot", "salesforce", "gmail", "linkedin"],
        "memory_scopes": ["workspace", "project"],
        "approval_policy": {"external_replies": "require_approval", "crm_writes": "require_approval"},
    },
    {
        "id": "support",
        "name": "Support Triage",
        "role": "support specialist",
        "description": "Customer issue triage, policy answers, and escalation routing.",
        "tool_grants": ["connector.search", "artifact.write", "task.create"],
        "connector_grants": ["slack", "teams", "jira", "zendesk"],
        "memory_scopes": ["workspace", "project"],
        "approval_policy": {"external_replies": "require_approval"},
    },
    {
        "id": "engineering",
        "name": "Engineering Agent",
        "role": "software engineer",
        "description": "Repository inspection, coding tasks, tests, and review assistance.",
        "tool_grants": ["repo.open", "repo.read", "repo.write", "repo.test"],
        "connector_grants": ["github", "linear", "jira"],
        "memory_scopes": ["project", "task"],
        "approval_policy": {"repo_writes": "require_approval", "pull_requests": "require_approval"},
    },
    {
        "id": "data_analysis",
        "name": "Data Analyst",
        "role": "data analyst",
        "description": "Dataset analysis, charts, reports, and metric diagnostics.",
        "tool_grants": ["data.run", "artifact.write", "code.python"],
        "connector_grants": ["google_drive", "airtable", "stripe"],
        "memory_scopes": ["project", "workspace"],
        "approval_policy": {"external_replies": "require_approval"},
    },
    {
        "id": "operations",
        "name": "Operations Agent",
        "role": "operations coordinator",
        "description": "Recurring operational checks, workflows, and handoffs.",
        "tool_grants": ["task.create", "workflow.run", "connector.search"],
        "connector_grants": ["slack", "teams", "linear", "jira"],
        "memory_scopes": ["workspace", "org"],
        "approval_policy": {"workflow_writes": "require_approval", "external_replies": "require_approval"},
    },
)

PUBLISH_TARGETS = {"slack", "teams", "email", "web", "api"}
PROFILE_KINDS = {"assistant", "agent"}
AUTONOMY_LEVELS = {"manual", "supervised", "approval_required", "autonomous"}


def templates() -> list[dict[str, Any]]:
    return [dict(template) for template in AGENT_TEMPLATES]


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="Expected a list")
    return value


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Expected an object")
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
        else:
            data[key] = str(value) if key.endswith("_id") and value is not None else value
    if data.get("id") is not None:
        data["id"] = str(data["id"])
    if data.get("agent_profile_id") is not None:
        data["agent_profile_id"] = str(data["agent_profile_id"])
    return data


async def _require_project_access(member: Member, project_ids: list[str]) -> None:
    if not project_ids:
        return
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(project_members.c.project_id)
                .join(projects, projects.c.id == project_members.c.project_id)
                .where(
                    project_members.c.member_id == member.id,
                    project_members.c.organization_id == member.organization_id,
                    projects.c.organization_id == member.organization_id,
                    project_members.c.project_id.in_(project_ids),
                )
            )
        ).all()
    allowed = {str(row[0]) for row in rows}
    missing = set(project_ids) - allowed
    if missing:
        raise HTTPException(status_code=404, detail="Project not found")
    for project_id in project_ids:
        await permissions.check(member, "view_project", project_id)


async def _event(
    *,
    agent_id: str,
    organization_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    publication_id: str | None = None,
    task_id: str | None = None,
) -> None:
    events = await reflect_table("agent_profile_events")
    async with engine.begin() as conn:
        values: dict[str, Any] = {
            "organization_id": organization_id,
            "region": settings.region,
            "agent_profile_id": agent_id,
            "event_type": event_type,
            "payload": payload or {},
        }
        if publication_id is not None:
            values["publication_id"] = publication_id
        if task_id is not None:
            values["task_id"] = task_id
        await conn.execute(insert(events).values(**values))


async def create_profile(member: Member, data: dict[str, Any]) -> dict[str, Any]:
    await permissions.check(member, "create_agent", settings.org_id)
    project_ids = [str(pid) for pid in _json_list(data.get("project_ids"))]
    await _require_project_access(member, project_ids)

    autonomy_level = str(data.get("autonomy_level") or "supervised")
    if autonomy_level not in AUTONOMY_LEVELS:
        raise HTTPException(status_code=422, detail="Invalid autonomy level")
    profile_kind = str(data.get("profile_kind") or "agent").lower()
    if profile_kind not in PROFILE_KINDS:
        raise HTTPException(status_code=422, detail="Invalid profile kind")

    profiles = await reflect_table("agent_profiles")
    values = {
        "organization_id": member.organization_id,
        "region": settings.region,
        "profile_kind": profile_kind,
        "name": str(data["name"]).strip(),
        "role": str(data["role"]).strip(),
        "template_id": data.get("template_id"),
        "instructions": str(data.get("instructions") or "").strip(),
        "personality": str(data.get("personality") or "").strip(),
        "model": data.get("model"),
        "tool_grants": _json_list(data.get("tool_grants")),
        "connector_grants": _json_list(data.get("connector_grants")),
        "workflows": _json_list(data.get("workflows")),
        "connected_accounts": _json_list(data.get("connected_accounts")),
        "project_ids": project_ids,
        "memory_scopes": _json_list(data.get("memory_scopes")),
        "autonomy_level": autonomy_level,
        "approval_policy": _json_dict(data.get("approval_policy")),
        "schedule_permissions": _json_dict(data.get("schedule_permissions")),
        "status": data.get("status") or "active",
        "created_by": member.id,
    }
    if not values["name"] or not values["role"]:
        raise HTTPException(status_code=422, detail="Profile name and role are required")

    async with engine.begin() as conn:
        row = (
            await conn.execute(insert(profiles).values(**values).returning(profiles))
        ).mappings().first()
    agent = _row_dict(row)
    await _event(agent_id=agent["id"], organization_id=member.organization_id, event_type=f"{profile_kind}_created", payload={"name": agent["name"]})
    await audit.log(
        f"{profile_kind}_created",
        member.id,
        "agents.create",
        organization_id=member.organization_id,
        resource_type="agent_profile",
        resource_id=agent["id"],
        payload={"profile_kind": profile_kind, "template_id": agent.get("template_id"), "tool_grants": agent.get("tool_grants", [])},
    )
    return agent


async def list_profiles(member: Member, profile_kind: str | None = None) -> list[dict[str, Any]]:
    await permissions.check(member, "list_agents", settings.org_id)
    profiles = await reflect_table("agent_profiles")
    filters = [profiles.c.organization_id == member.organization_id, profiles.c.status != "deleted"]
    if profile_kind:
        kind = profile_kind.lower()
        if kind not in PROFILE_KINDS:
            raise HTTPException(status_code=422, detail="Invalid profile kind")
        filters.append(profiles.c.profile_kind == kind)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(profiles)
                .where(*filters)
                .order_by(profiles.c.created_at.desc())
            )
        ).mappings().all()
    return [_row_dict(row) for row in rows]


async def get_profile(member: Member, agent_id: str) -> dict[str, Any]:
    await permissions.check(member, "view_agent", agent_id)
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(profiles).where(
                    profiles.c.id == agent_id,
                    profiles.c.organization_id == member.organization_id,
                    profiles.c.status != "deleted",
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _row_dict(row)


async def patch_profile(member: Member, agent_id: str, data: dict[str, Any]) -> dict[str, Any]:
    await get_profile(member, agent_id)
    patch = {key: value for key, value in data.items() if value is not None}
    if "profile_kind" in patch:
        patch["profile_kind"] = str(patch["profile_kind"]).lower()
        if patch["profile_kind"] not in PROFILE_KINDS:
            raise HTTPException(status_code=422, detail="Invalid profile kind")
    if "project_ids" in patch:
        patch["project_ids"] = [str(pid) for pid in _json_list(patch["project_ids"])]
        await _require_project_access(member, patch["project_ids"])
    for list_key in ("tool_grants", "connector_grants", "memory_scopes", "workflows", "connected_accounts"):
        if list_key in patch:
            patch[list_key] = _json_list(patch[list_key])
    if "autonomy_level" in patch and patch["autonomy_level"] not in AUTONOMY_LEVELS:
        raise HTTPException(status_code=422, detail="Invalid autonomy level")
    if not patch:
        return await get_profile(member, agent_id)
    patch["updated_at"] = func.now()
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(profiles)
                .where(profiles.c.id == agent_id, profiles.c.organization_id == member.organization_id)
                .values(**patch)
                .returning(profiles)
            )
        ).mappings().first()
    agent = _row_dict(row)
    await _event(agent_id=agent_id, organization_id=member.organization_id, event_type="agent_updated", payload={"fields": list(data.keys())})
    await audit.log("agent_updated", member.id, "agents.patch", organization_id=member.organization_id, resource_type="agent_profile", resource_id=agent_id, payload={"fields": list(data.keys())})
    return agent


async def delete_profile(member: Member, agent_id: str) -> dict[str, Any]:
    await get_profile(member, agent_id)
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        await conn.execute(
            update(profiles)
            .where(profiles.c.id == agent_id, profiles.c.organization_id == member.organization_id)
            .values(status="deleted")
        )
    await _event(agent_id=agent_id, organization_id=member.organization_id, event_type="agent_deleted")
    await audit.log("agent_deleted", member.id, "agents.delete", organization_id=member.organization_id, resource_type="agent_profile", resource_id=agent_id)
    return {"deleted": True, "agent_id": agent_id}


async def publish_agent(member: Member, agent_id: str, data: dict[str, Any]) -> dict[str, Any]:
    agent = await get_profile(member, agent_id)
    await permissions.check(member, "publish_agent", agent_id)
    target = str(data.get("target") or "").lower()
    if target not in PUBLISH_TARGETS:
        raise HTTPException(status_code=422, detail="Unsupported publish target")
    publications = await reflect_table("agent_publications")
    token = secrets.token_urlsafe(32)
    values = {
        "organization_id": member.organization_id,
        "region": settings.region,
        "agent_profile_id": agent_id,
        "target": target,
        "display_name": data.get("display_name") or agent["name"],
        "external_channel_id": data.get("external_channel_id"),
        "config": _json_dict(data.get("config")),
        "approval_policy": agent.get("approval_policy") or {},
        "inbound_token": token,
        "status": "active",
        "created_by": member.id,
    }
    async with engine.begin() as conn:
        row = (
            await conn.execute(insert(publications).values(**values).returning(publications))
        ).mappings().first()
    publication = _row_dict(row)
    await _event(
        agent_id=agent_id,
        organization_id=member.organization_id,
        publication_id=publication["id"],
        event_type="agent_published",
        payload={"target": target, "external_channel_id": values["external_channel_id"]},
    )
    await audit.log("agent_published", member.id, "agents.publish", organization_id=member.organization_id, resource_type="agent_publication", resource_id=publication["id"], payload={"agent_id": agent_id, "target": target})
    return publication


async def get_publication(publication_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    publications = await reflect_table("agent_publications")
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        publication = (
            await conn.execute(
                select(publications).where(publications.c.id == publication_id, publications.c.status == "active")
            )
        ).mappings().first()
        if publication is None:
            raise HTTPException(status_code=404, detail="Publication not found")
        agent = (
            await conn.execute(
                select(profiles).where(
                    profiles.c.id == publication["agent_profile_id"],
                    profiles.c.organization_id == publication["organization_id"],
                    profiles.c.status == "active",
                )
            )
        ).mappings().first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _row_dict(publication), _row_dict(agent)
