"""Member-level authorization for artifact content and metadata."""

from __future__ import annotations

from sqlalchemy import and_, select

from core.db import engine, reflect_table
from core.models import Member

ORG_ADMIN_ROLES = {"admin", "owner"}


def created_by_member(meta: dict, member: Member) -> bool:
    created_by = str(meta.get("created_by") or "")
    return created_by in {str(member.id), f"member:{member.id}"}


async def artifact_access(member: Member, meta: dict) -> tuple[bool, bool]:
    """Return ``(visible, writable)`` for one tenant-validated artifact row."""

    if member.role in ORG_ADMIN_ROLES:
        return True, True
    if created_by_member(meta, member):
        return True, True

    # Only an artifact explicitly moved into a project becomes project-visible.
    # A private parent conversation/task must not be widened merely because the
    # parent itself carries a project_id.
    project_ids: set[str] = {
        str(meta["project_id"])
    } if meta.get("project_id") else set()
    parent_visible = False
    parent_writable = False
    conversations = await reflect_table("conversations")
    conversation_members = await reflect_table("conversation_members")
    tasks = await reflect_table("tasks")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        if meta.get("conversation_id"):
            conversation = (
                await conn.execute(
                    select(
                        conversations.c.member_id,
                        conversations.c.project_id,
                        conversation_members.c.role.label("access_role"),
                    )
                    .select_from(
                        conversations.outerjoin(
                            conversation_members,
                            and_(
                                conversation_members.c.organization_id
                                == conversations.c.organization_id,
                                conversation_members.c.conversation_id
                                == conversations.c.id,
                                conversation_members.c.member_id == member.id,
                            ),
                        )
                    )
                    .where(
                        conversations.c.id == str(meta["conversation_id"]),
                        conversations.c.organization_id == member.organization_id,
                    )
                )
            ).mappings().first()
            if conversation:
                owner = str(conversation.get("member_id")) == str(member.id)
                acl_role = str(conversation.get("access_role") or "")
                parent_visible = parent_visible or owner or acl_role in {
                    "owner",
                    "editor",
                    "viewer",
                }
                parent_writable = parent_writable or owner or acl_role in {"owner", "editor"}
        if meta.get("task_id"):
            task = (
                await conn.execute(
                    select(
                        tasks.c.triggered_by_member_id,
                        tasks.c.assignee_member_id,
                        tasks.c.project_id,
                    ).where(
                        tasks.c.id == str(meta["task_id"]),
                        tasks.c.organization_id == member.organization_id,
                    )
                )
            ).mappings().first()
            if task:
                task_owner = str(task.get("triggered_by_member_id")) == str(member.id)
                task_assignee = str(task.get("assignee_member_id") or "") == str(member.id)
                parent_visible = parent_visible or task_owner or task_assignee
                parent_writable = parent_writable or task_owner

        membership = None
        if project_ids:
            membership = (
                await conn.execute(
                    select(project_members.c.role).where(
                        project_members.c.organization_id == member.organization_id,
                        project_members.c.member_id == member.id,
                        project_members.c.project_id.in_(project_ids),
                    )
                )
            ).first()

    if parent_visible:
        return True, parent_writable
    if membership:
        return True, str(membership[0]) in {"owner", "editor"}
    return False, False
