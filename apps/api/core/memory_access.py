"""Authorization helpers for durable memory scopes.

Memory rows are tenant-scoped, but tenant scoping alone is not enough: a single
organization can contain private member memories, private conversations and
restricted projects.  This module is the shared, fail-closed policy used by the
control-center APIs and write primitives.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.sql.elements import ColumnElement

from core.db import engine, reflect_table
from core.models import Member, RequesterContext
from core import conversation_access as conversation_acl
from core.project_access import member_can_access_project as _canonical_project_access
from core.task_access import task_access_role, visible_task


ADMIN_ROLES = {"admin", "owner"}
ENTRY_SCOPES = {
    "org",
    "workspace",
    "project",
    "persona",
    "personal",
    "restricted",
    "conversation",
    "task",
}
POLICY_SCOPES = {"org", "workspace", "project", "member", "conversation"}


def _clean_scope(scope: str) -> str:
    value = str(scope or "").strip().lower()
    if value not in ENTRY_SCOPES:
        raise ValueError(f"invalid memory scope: {value or '<empty>'}")
    return value


def canonical_scope_for_context(
    requester_context: RequesterContext,
    scope: str,
) -> tuple[str, str]:
    """Resolve an autonomous-memory scope without allowing the model to widen it.

    Model-selected scope labels are untrusted. Autonomous extraction is always
    private to the member who caused it. Shared project or organization memory
    requires an explicit, reviewed user action through the control center.
    """
    _clean_scope(scope)  # reject malformed model output for auditability
    return "personal", requester_context.member_id


async def authorized_autonomous_scope(
    requester_context: RequesterContext,
) -> tuple[str, str]:
    """Return the only permitted destination for model-derived memory.

    Project membership proves read access, not consent to publish an extracted
    conversation fact to every project member. Promotion remains an explicit
    user operation and autonomous capture therefore always stays personal.
    """
    return canonical_scope_for_context(requester_context, "personal")


async def _project_membership(member: Member, project_id: str) -> dict[str, Any] | None:
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(project_members)
                .join(projects, projects.c.id == project_members.c.project_id)
                .where(
                    cast(project_members.c.project_id, String) == project_id,
                    project_members.c.member_id == member.id,
                    project_members.c.organization_id == member.organization_id,
                    projects.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def member_can_access_project(member: Member, project_id: str) -> bool:
    return await _canonical_project_access(member, str(project_id))


async def _owns_conversation(member: Member, conversation_id: str) -> bool:
    try:
        await conversation_acl.require_conversation(
            member, conversation_id, minimum="owner"
        )
    except LookupError:
        return False
    return True


async def _can_access_conversation(member: Member, conversation_id: str) -> bool:
    row, role = await conversation_acl.conversation_access(member, conversation_id)
    return row is not None and conversation_acl.role_allows(role, "viewer")


async def require_conversation_owner(member: Member, conversation_id: str) -> None:
    """Raise a non-enumerating error when a conversation is not caller-owned."""
    if not await _owns_conversation(member, str(conversation_id)):
        raise LookupError("Conversation not found")


async def _owns_task(member: Member, task_id: str) -> bool:
    task = await visible_task(member, task_id)
    return task is not None and task_access_role(member, task) in {"owner", "admin"}


async def _can_access_task(member: Member, task_id: str) -> bool:
    return await visible_task(member, task_id) is not None


async def _persona_exists(member: Member, persona_id: str) -> bool:
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(profiles.c.id).where(
                    cast(profiles.c.id, String) == persona_id,
                    profiles.c.organization_id == member.organization_id,
                    profiles.c.status != "deleted",
                )
            )
        ).first()
    return row is not None


async def can_access_scope(member: Member, scope: str, scope_id: str) -> bool:
    """Return whether ``member`` may read the exact scope pair."""
    try:
        scope = _clean_scope(scope)
    except ValueError:
        return False
    scope_id = str(scope_id or "")
    if scope == "org":
        return scope_id == member.organization_id
    if scope == "workspace":
        # Chronos currently has one durable workspace context per organization;
        # arbitrary client-provided workspace ids are therefore never trusted.
        return scope_id == member.organization_id
    if scope == "personal":
        return scope_id == member.id
    if scope == "restricted":
        return scope_id == member.id or (
            scope_id == member.organization_id and member.role in ADMIN_ROLES
        )
    if scope == "project":
        return await member_can_access_project(member, scope_id)
    if scope == "conversation":
        return await _can_access_conversation(member, scope_id)
    if scope == "task":
        return await _can_access_task(member, scope_id)
    if scope == "persona":
        return await _persona_exists(member, scope_id)
    return False


async def normalize_entry_scope(
    member: Member,
    scope: str,
    scope_id: str | None,
) -> tuple[str, str]:
    """Canonicalize a user-supplied scope and prove the target is accessible."""
    scope = _clean_scope(scope)
    supplied = str(scope_id).strip() if scope_id is not None else ""
    if scope in {"personal", "restricted"}:
        target = supplied or member.id
    elif scope in {"org", "workspace"}:
        target = supplied or member.organization_id
    else:
        if not supplied:
            raise ValueError(f"scope_id is required for {scope} memory")
        target = supplied
    if not await can_access_scope(member, scope, target):
        raise ValueError("memory scope is not accessible")
    if scope in {"org", "workspace"} and member.role not in ADMIN_ROLES:
        raise ValueError("admin role required for shared memory")
    if scope == "project":
        membership = await _project_membership(member, target)
        if member.role not in ADMIN_ROLES and (
            membership is None or membership.get("role") != "owner"
        ):
            raise ValueError("project owner role required for project memory")
    if scope == "persona" and member.role not in ADMIN_ROLES:
        profiles = await reflect_table("agent_profiles")
        async with engine.begin() as conn:
            profile = (
                await conn.execute(
                    select(profiles.c.created_by).where(
                        cast(profiles.c.id, String) == target,
                        profiles.c.organization_id == member.organization_id,
                        profiles.c.status != "deleted",
                    )
                )
            ).mappings().first()
        if profile is None or str(profile.get("created_by") or "") != member.id:
            raise ValueError("persona owner role required for persona memory")
    if scope == "conversation":
        _row, role = await conversation_acl.conversation_access(member, target)
        if not conversation_acl.role_allows(role, "editor"):
            raise ValueError("conversation memory requires editor access")
    if scope == "task":
        task = await visible_task(member, target)
        if task is None or task_access_role(member, task) not in {"owner", "admin"}:
            raise ValueError("task owner role required for task memory")
    return scope, target


async def validate_policy_target(
    member: Member,
    scope: str,
    scope_id: str | None,
) -> tuple[str, str]:
    """Validate a memory-capture policy target and its required ownership."""
    scope = str(scope or "").strip().lower()
    if scope not in POLICY_SCOPES:
        raise ValueError(f"invalid memory policy scope: {scope or '<empty>'}")
    supplied = str(scope_id).strip() if scope_id is not None else ""
    if scope in {"org", "workspace"}:
        target = supplied or member.organization_id
        if target != member.organization_id or member.role not in ADMIN_ROLES:
            raise ValueError("admin role required for organization memory policy")
    elif scope == "member":
        target = supplied or member.id
        if target != member.id:
            raise ValueError("member memory policy must target the current member")
    elif scope == "conversation":
        if not supplied or not await _owns_conversation(member, supplied):
            raise ValueError("conversation not found")
        target = supplied
    else:  # project
        if not supplied:
            raise ValueError("scope_id is required for project memory policy")
        membership = await _project_membership(member, supplied)
        if membership is None:
            raise ValueError("project not found")
        if member.role not in ADMIN_ROLES and membership.get("role") != "owner":
            raise ValueError("project owner role required for project memory policy")
        target = supplied
    return scope, target


async def memory_access_condition(table: Any, member: Member) -> ColumnElement[bool]:
    """Build the SQL predicate for all memory rows visible to ``member``."""
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    conversations = await reflect_table("conversations")
    conversation_members = await reflect_table("conversation_members")
    tasks = await reflect_table("tasks")
    profiles = await reflect_table("agent_profiles")

    member_projects = (
        select(cast(project_members.c.project_id, String))
        .join(projects, projects.c.id == project_members.c.project_id)
        .where(
            project_members.c.organization_id == member.organization_id,
            project_members.c.member_id == member.id,
            projects.c.organization_id == member.organization_id,
        )
    )
    member_conversations = select(cast(conversations.c.id, String)).where(
        conversations.c.organization_id == member.organization_id,
    )
    member_tasks = select(cast(tasks.c.id, String)).where(
        tasks.c.organization_id == member.organization_id,
    )
    if member.role not in ADMIN_ROLES:
        member_conversations = member_conversations.where(
            conversation_acl.visibility_clause(
                conversations, conversation_members, member
            )
        )
        member_tasks = member_tasks.where(
            or_(
                tasks.c.triggered_by_member_id == member.id,
                tasks.c.assignee_member_id == member.id,
            )
        )
    org_personas = select(cast(profiles.c.id, String)).where(
        profiles.c.organization_id == member.organization_id,
        profiles.c.status != "deleted",
    )
    clauses: list[ColumnElement[bool]] = [
        and_(table.c.scope == "org", table.c.scope_id == member.organization_id),
        and_(table.c.scope == "workspace", table.c.scope_id == member.organization_id),
        and_(table.c.scope == "personal", table.c.scope_id == member.id),
        and_(table.c.scope == "restricted", table.c.scope_id == member.id),
        and_(table.c.scope == "project", table.c.scope_id.in_(member_projects)),
        and_(table.c.scope == "conversation", table.c.scope_id.in_(member_conversations)),
        and_(table.c.scope == "task", table.c.scope_id.in_(member_tasks)),
        and_(table.c.scope == "persona", table.c.scope_id.in_(org_personas)),
    ]
    if member.role in ADMIN_ROLES:
        clauses.append(
            and_(table.c.scope == "restricted", table.c.scope_id == member.organization_id)
        )
    # Legacy ``synthesized`` rows were generated from raw organization
    # conversation transcripts and were never reviewed before becoming
    # organization-visible.  Migration 0048 quarantines those rows; this source
    # guard also fails closed during rolling deploys where new application code
    # can briefly run before every database has completed the migration.
    return and_(or_(*clauses), table.c.source != "synthesized")


def memory_mutation_allowed(
    member: Member,
    memory: dict[str, Any],
    *,
    project_role: str | None = None,
    shared_role: str | None = None,
) -> bool:
    """Return whether a readable memory may be changed by ``member``.

    Read access and edit access deliberately differ for shared scopes. Project
    members can read project memory, while only the fact's creator, a project
    owner, or an organization administrator can change it. Organization,
    workspace, and persona memory follow the same creator/admin rule. Private
    scopes are mutable because their exact owner was already proven by
    ``can_access_scope``.
    """
    scope = str(memory.get("scope") or "")
    creator = str(memory.get("created_by") or "")
    if scope in {"personal", "restricted"}:
        return True
    if member.role in ADMIN_ROLES or creator == member.id:
        return True
    if scope in {"conversation", "task"}:
        return shared_role == "owner"
    if scope == "project" and project_role == "owner":
        return True
    return False


async def get_memory_for_member(
    memory_id: str,
    member: Member,
    *,
    mutate: bool = False,
) -> dict[str, Any] | None:
    """Load a memory if readable/mutable, without revealing inaccessible rows."""
    table = await reflect_table("memory_entries")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(
                    table.c.id == memory_id,
                    table.c.organization_id == member.organization_id,
                    table.c.is_deleted.is_(False),
                )
            )
        ).mappings().first()
    if row is None:
        return None
    data = dict(row)
    if str(data.get("source") or "") == "synthesized":
        return None
    if not await can_access_scope(member, str(data["scope"]), str(data["scope_id"])):
        return None
    if not mutate:
        return data

    project_role: str | None = None
    shared_role: str | None = None
    if str(data["scope"]) == "project":
        membership = await _project_membership(member, str(data["scope_id"]))
        project_role = str(membership.get("role") or "") if membership else None
    elif str(data["scope"]) == "conversation":
        _conversation, shared_role = await conversation_acl.conversation_access(
            member, str(data["scope_id"])
        )
    elif str(data["scope"]) == "task":
        task = await visible_task(member, str(data["scope_id"]))
        shared_role = task_access_role(member, task) if task else None
    return data if memory_mutation_allowed(
        member,
        data,
        project_role=project_role,
        shared_role=shared_role,
    ) else None
