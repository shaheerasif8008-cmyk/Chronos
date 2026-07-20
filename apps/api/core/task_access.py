"""Canonical member-level visibility for task records.

Tasks are private to their immutable creator by default.  An explicit current
assignee gains read access; organization admins retain the audited break-glass
path.  Every router and cross-resource helper should reuse this policy instead
of treating organization membership alone as sufficient.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement

from core.db import engine, reflect_table
from core.models import Member


ORG_ADMIN_ROLES = {"admin", "owner"}


def visibility_clause(tasks: Any, member: Member) -> ColumnElement[bool]:
    """Member-level task predicate (caller must also add tenant scoping)."""

    try:
        assignee = tasks.c.assignee_member_id
    except (AttributeError, KeyError):
        assignee = None
    if assignee is not None:
        return or_(
            tasks.c.triggered_by_member_id == member.id,
            assignee == member.id,
        )
    # Rolling-deploy fallback before migration 0049 reaches a replica.
    return tasks.c.triggered_by_member_id == member.id


def task_access_role(member: Member, task: dict) -> str | None:
    """Return admin/owner/assignee for a tenant-validated task row."""

    if str(task.get("organization_id")) != str(member.organization_id):
        return None
    if member.role in ORG_ADMIN_ROLES:
        return "admin"
    if str(task.get("triggered_by_member_id") or "") == str(member.id):
        return "owner"
    if str(task.get("assignee_member_id") or "") == str(member.id):
        return "assignee"
    return None


async def visible_task(member: Member, task_id: str) -> dict | None:
    """Return a task visible to its owner, current assignee, or an org admin."""

    tasks = await reflect_table("tasks")
    conditions = [
        tasks.c.id == str(task_id),
        tasks.c.organization_id == member.organization_id,
    ]
    if member.role not in ORG_ADMIN_ROLES:
        conditions.append(visibility_clause(tasks, member))
    async with engine.begin() as conn:
        row = (await conn.execute(select(tasks).where(*conditions))).mappings().first()
    return dict(row) if row else None
