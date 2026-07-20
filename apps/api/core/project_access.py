"""Canonical project visibility and tool-default policy.

Projects are private by default.  An owner may make a project visible to every
active member of the same organization without granting edit/manage rights.
The project ``default_tools`` field is also interpreted here: an empty list
inherits workspace tool availability, while a non-empty list is an allowlist
of tool families or exact registry/broker names for project chat and tasks.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import String, cast, or_, select

from core.db import engine, reflect_table
from core.models import Member


PROJECT_VISIBILITIES = frozenset({"private", "organization"})
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_-]*(?:(?:__|\.)[a-z][a-z0-9_-]*)?$")
_CONTROL_TOOLS = frozenset({"start_task", "ask_clarification", "spawn__subagent"})


def normalize_visibility(value: str | None) -> str:
    visibility = str(value or "private").strip().lower()
    if visibility not in PROJECT_VISIBILITIES:
        raise ValueError("visibility must be 'private' or 'organization'")
    return visibility


def normalize_default_tools(values: list[Any] | None) -> list[str]:
    """Return a bounded, stable project tool allowlist.

    Empty means inherit organization/workspace defaults.  Wildcards are not
    accepted because they make the stored policy ambiguous and hard to audit.
    """
    if not values:
        return []
    if len(values) > 64:
        raise ValueError("default_tools may contain at most 64 entries")
    normalized: list[str] = []
    for raw in values:
        value = str(raw or "").strip().lower()
        if not value or not _TOOL_NAME.fullmatch(value):
            raise ValueError(f"invalid project tool name: {raw!r}")
        if value not in normalized:
            normalized.append(value)
    return normalized


def tool_is_allowed(allowlist: list[str] | None, tool_name: str) -> bool:
    """Match a registry or broker tool name against an inherited/explicit policy."""
    allowed = normalize_default_tools(allowlist)
    if not allowed:
        return True
    registry_name = str(tool_name).strip().lower()
    if registry_name in _CONTROL_TOOLS:
        return True
    broker_name = registry_name.replace("__", ".", 1)
    family = broker_name.split(".", 1)[0]
    return registry_name in allowed or broker_name in allowed or family in allowed


async def project_access_role(member: Member, project_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """Return the tenant-scoped project and caller role, without enumeration leaks."""
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        project = (
            await conn.execute(
                select(projects).where(
                    cast(projects.c.id, String) == str(project_id),
                    projects.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
        if project is None:
            return None, None
        membership = (
            await conn.execute(
                select(project_members).where(
                    cast(project_members.c.project_id, String) == str(project_id),
                    project_members.c.member_id == member.id,
                    project_members.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if membership is not None:
        return dict(project), str(membership.get("role") or "member")
    if normalize_visibility(project.get("visibility")) == "organization":
        return dict(project), "viewer"
    return None, None


async def member_can_access_project(member: Member, project_id: str) -> bool:
    project, role = await project_access_role(member, project_id)
    return project is not None and role is not None


async def member_can_edit_project(member: Member, project_id: str) -> bool:
    """Return whether the caller holds an explicit project membership.

    ``organization`` visibility deliberately synthesizes the ``viewer`` role
    for same-tenant non-members.  That role is sufficient for reads, but must
    never authorize project-linked conversations, tasks, sources, or other
    mutations.
    """
    project, role = await project_access_role(member, project_id)
    return project is not None and role in {"member", "owner"}


async def project_tool_allowlist(org_id: str, project_id: str | None) -> list[str]:
    """Load a project's stored allowlist using both project and tenant identity."""
    if not project_id:
        return []
    projects = await reflect_table("projects")
    async with engine.begin() as conn:
        value = (
            await conn.execute(
                select(projects.c.default_tools).where(
                    cast(projects.c.id, String) == str(project_id),
                    projects.c.organization_id == str(org_id),
                )
            )
        ).scalar_one_or_none()
    if value is None:
        raise LookupError("Project not found")
    return normalize_default_tools(value if isinstance(value, list) else [])


def visible_project_clause(projects: Any, project_members: Any, member: Member):
    """SQL predicate for caller membership or organization-visible projects."""
    return or_(
        project_members.c.member_id == member.id,
        projects.c.visibility == "organization",
    )
