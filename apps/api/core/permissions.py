"""Permission seam.

The signature is frozen: ``check(actor, action, resource) -> bool``. 200+ call
sites depend on it. It always audits, and on success returns True. A denial
RAISES ``PermissionDenied`` (no call site reads the bool, so raising is the only
way to actually block).

Two enforcement layers, both ON BY DEFAULT:

1. **Deterministic role gates** — approval decisions and admin governance
   mutations (autonomy graduation, learned-policy ratification, risk pricing,
   evidence export) are denied for non-privileged human actors *regardless of
   whether a policy engine is configured*. This is the always-on floor.
2. **Relationship checks (OpenFGA)** — project- and workspace-scoped actions are
   checked against the authorization model whenever OpenFGA is configured
   (``permissions_enforce`` defaults true, so configuring the engine is enough).
   When the engine is unreachable, these checks fail CLOSED.

Actions that don't map to either layer are only allowed for low-risk generic
checks such as ``use_tool:*``. Sensitive router/governance actions must be
listed in the deterministic role gates below; otherwise they fail closed.
Internal/system actors (the agent runtime, schedulers, sync jobs) bypass FGA;
the broker's own safety limits still apply to their tool calls.
"""
from __future__ import annotations

from core import audit, authz
from core.authz import AuthzUnavailable
from core.exceptions import PermissionDenied
from core.models import Member

# Imported lazily inside reconcile_org_tuples to avoid circular imports at
# module load time (db depends on config which is fine, but keep it isolated).
from sqlalchemy import select
from sqlalchemy.exc import NoSuchTableError

# Role precedence used when aggregating group roles (mirrors core/scim.py).
_ROLE_RANK = {"viewer": 0, "user": 1, "operator": 2, "manager": 3, "admin": 4, "owner": 5}


def _rank(role: str) -> int:
    return _ROLE_RANK.get(role, 1)

# action → relation on project:{resource}
_VIEW_ACTIONS = {"view_project", "view_project_sources"}
_EDIT_ACTIONS = {
    "add_project_source",
    "sync_project_source",
    "reindex_project_source",
    "refresh_project_source",
    "update_project",
    "update_project_instructions",
}
_MANAGE_ACTIONS = {
    "delete_project",
    "delete_project_source",
    "add_project_member",
    "remove_project_member",
}

# action → relation on workspace:{resource}
_WORKSPACE_VIEW_ACTIONS = {"view_workspace"}
_WORKSPACE_EDIT_ACTIONS = {"update_workspace", "create_workspace_resource"}
_WORKSPACE_MANAGE_ACTIONS = {
    "delete_workspace",
    "add_workspace_member",
    "remove_workspace_member",
    "set_workspace_autonomy",
}

# action → relation on task:{resource}
_TASK_VIEW_ACTIONS = {"view_task", "view_task_events", "stream_task"}
_TASK_MANAGE_ACTIONS = {"cancel_task", "retry_task"}

# Deciding an approval (approving/rejecting a risky write) is the core enterprise
# governance gate. It is enforced deterministically by role — independent of
# OpenFGA — so the guarantee "an unauthorized user cannot approve" holds even
# when no policy engine is configured. Only these roles may decide.
_APPROVAL_DECISION_ACTIONS = {"decide_approval", "resolve_connector_approval"}
_APPROVER_ROLES = {"admin", "owner", "approver"}

# Admin-only governance mutations. Like approval decisions, these are enforced
# deterministically by role and ON BY DEFAULT — no OpenFGA required — so an
# unauthorized user can never graduate autonomy, ratify a learned policy, edit
# risk pricing, or export evidence, regardless of policy-engine configuration.
_ADMIN_ACTIONS = {
    "graduate_autonomy",
    "demote_autonomy",
    "confirm_learned_policy",
    "disable_learned_policy",
    "set_risk_override",
    "export_evidence",
    "manage_sso",
    "manage_scim",
    "list_memory",
    "export_memory",
    "list_audit_log",
    "export_audit_log",
    "list_connector_execution_logs",
    "list_connector_approvals",
    "list_connector_health",
    "list_connector_execution_traces",
    "get_connector_execution_trace",
    "list_connector_execution_jobs",
    "cancel_connector_execution_job",
    "create_connector_plan",
    "execute_connector_plan",
    "register_mcp_server",
    "discover_mcp_server",
    "create_connector_policy",
    "delete_connector_policy",
    "install_connector",
    "disable_connector",
    "grant_connector_permission",
    "revoke_connector_permission",
}
_ADMIN_ROLES = {"admin", "owner"}
_GENERIC_ALLOWED_PREFIXES = ("use_tool:", "connect_", "disconnect_")
_GENERIC_ALLOWED_ACTIONS = {
    "chat",
    "admin",
    "agent",
    "approver",
    "owner",
    "source_sync",
    "user",
    "analyze_dataset",
    "apply_context_suggestion",
    "approve_browser_sensitive_site",
    "cancel_research",
    "cancel_workflow_run",
    "close_browser_session",
    "close_desktop_session",
    "complete_workflow_step",
    "connect_gmail",
    "connect_{provider}",
    "create_agent",
    "create_browser_session",
    "create_computer_session",
    "create_dataset",
    "create_desktop_session",
    "create_local_computer_grant",
    "create_memory",
    "create_monitor",
    "create_project",
    "create_research",
    "create_schedule",
    "create_task",
    "create_workflow",
    "create_workflow_trigger",
    "delete_memory",
    "delete_schedule",
    "disconnect_{provider}",
    "dispatch_workflow_event",
    "evaluate_monitor",
    "execute_connector_action",
    "generate_context_suggestion",
    "get_autonomy",
    "get_dataset",
    "get_workflow_run",
    "hand_back_browser_session",
    "list_activity_actions",
    "list_agents",
    "list_approvals",
    "list_browser_sessions",
    "list_chat_models",
    "list_chat_modes",
    "list_computer_sessions",
    "list_context_suggestions",
    "list_conversations",
    "list_datasets",
    "list_desktop_sessions",
    "list_local_computer_grants",
    "list_monitor_alerts",
    "list_monitors",
    "list_projects",
    "list_schedule_runs",
    "list_skills",
    "list_tasks",
    "list_workflow_runs",
    "list_workflow_triggers",
    "list_workflows",
    "pause_workflow_run",
    "publish_agent",
    "read_settings",
    "recover_workflows",
    "reject_context_suggestion",
    "request_browser_takeover",
    "resume_workflow_run",
    "resolve_connector_approval",
    "revoke_browser_session",
    "revoke_desktop_session",
    "revoke_local_computer_grant",
    "run_schedule",
    "search",
    "skill.run_script",
    "skill.write",
    "start_workflow_run",
    "stream_memory_events",
    "stream_research",
    "tick_workflow_run",
    "undo_memory",
    "update_memory",
    "update_monitor",
    "update_schedule",
    "upload_attachment",
    "view_agent",
    "view_approval",
    "view_autonomy",
    "view_browser_session",
    "view_browser_session_events",
    "view_computer_session_events",
    "view_desktop_session_events",
    "view_local_computer_events",
    "view_project_artifacts",
    "view_project_conversations",
    "view_project_tasks",
    "view_skill",
    "list_connectors",
    "list_connector_tools",
    "list_connector_actions",
    "execute_connector_tool_call",
    "list_mcp_servers",
    "list_connector_policies",
    "view_conversation",
    "rename_conversation",
    "delete_conversation",
    "pin_message",
    "unpin_message",
    "edit_message",
    "branch_conversation",
    "save_to_memory",
    "convert_to_task",
    "convert_to_workflow",
    "regenerate_message",
    "retry_from_message",
    "artifact.create",
    "artifact.read",
    "artifact.edit",
    "artifact.delete",
    "artifact.publish",
}

# Actors that represent the system itself, not a human — they bypass FGA.
_INTERNAL_ACTOR_IDS = {"chronos", "source_sync", "system", "scheduler"}
_INTERNAL_ROLES = {"agent", "system"}


def _resource_for(action: str) -> tuple[str, str] | None:
    """Map an action to ``(relation, object_type)`` for FGA, or None if unmapped."""
    if action in _VIEW_ACTIONS:
        return "can_view", "project"
    if action in _EDIT_ACTIONS:
        return "can_edit", "project"
    if action in _MANAGE_ACTIONS:
        return "can_manage", "project"
    if action in _WORKSPACE_VIEW_ACTIONS:
        return "can_view", "workspace"
    if action in _WORKSPACE_EDIT_ACTIONS:
        return "can_edit", "workspace"
    if action in _WORKSPACE_MANAGE_ACTIONS:
        return "can_manage", "workspace"
    if action in _TASK_VIEW_ACTIONS:
        return "can_view", "task"
    if action in _TASK_MANAGE_ACTIONS:
        return "can_manage", "task"
    return None


def _is_internal(actor: Member) -> bool:
    return actor.id in _INTERNAL_ACTOR_IDS or actor.role in _INTERNAL_ROLES


def _is_generic_allowed_action(action: str) -> bool:
    return action in _GENERIC_ALLOWED_ACTIONS or any(
        action.startswith(prefix) for prefix in _GENERIC_ALLOWED_PREFIXES
    )


async def check(actor: Member, action: str, resource: str) -> bool:
    """Authorize ``actor`` to perform ``action`` on ``resource``.

    Returns True when allowed. Raises ``PermissionDenied`` when enforcement is on
    and the authorization model denies the action (or OpenFGA is unreachable —
    enforcement fails closed).
    """
    # Approval decisions: deny non-privileged human actors up front, always on.
    if (
        action in _APPROVAL_DECISION_ACTIONS
        and not _is_internal(actor)
        and actor.role not in _APPROVER_ROLES
    ):
        await audit.log(
            "permission_check",
            actor.id,
            action,
            organization_id=actor.organization_id,
            resource_type="approval",
            resource_id=resource,
            decision="denied",
        )
        raise PermissionDenied(actor.id, action, resource)

    # Admin governance mutations: deny non-admin human actors, always on (no
    # policy engine required). This is the "enforce by default" guarantee for the
    # highest-impact controls (autonomy graduation, learned policies, risk pricing).
    if (
        action in _ADMIN_ACTIONS
        and not _is_internal(actor)
        and actor.role not in _ADMIN_ROLES
    ):
        await audit.log(
            "permission_check",
            actor.id,
            action,
            organization_id=actor.organization_id,
            resource_type="admin",
            resource_id=resource,
            decision="denied",
        )
        raise PermissionDenied(actor.id, action, resource)

    if action in _APPROVAL_DECISION_ACTIONS and not _is_internal(actor):
        await audit.log(
            "permission_check",
            actor.id,
            action,
            organization_id=actor.organization_id,
            resource_type="approval",
            resource_id=resource,
            decision="granted_role_gate",
        )
        return True

    if action in _ADMIN_ACTIONS and not _is_internal(actor):
        await audit.log(
            "permission_check",
            actor.id,
            action,
            organization_id=actor.organization_id,
            resource_type="admin",
            resource_id=resource,
            decision="granted_role_gate",
        )
        return True

    mapped = _resource_for(action)
    relation, object_type = mapped if mapped else (None, None)
    enforce = authz.is_enabled() and relation is not None and not _is_internal(actor)

    if mapped is None and not _is_internal(actor) and not _is_generic_allowed_action(action):
        await audit.log(
            "permission_check",
            actor.id,
            action,
            organization_id=actor.organization_id,
            resource_type="generic",
            resource_id=resource,
            decision="denied_unmapped",
        )
        raise PermissionDenied(actor.id, action, resource)

    decision = "granted_stub"
    if enforce:
        try:
            allowed = await authz.check(f"user:{actor.id}", relation, f"{object_type}:{resource}")
        except AuthzUnavailable:
            # Operator chose enforcement but the server is down → fail closed.
            await audit.log(
                "permission_check",
                actor.id,
                action,
                organization_id=actor.organization_id,
                resource_type=object_type,
                resource_id=resource,
                decision="denied_authz_unavailable",
            )
            raise PermissionDenied(actor.id, action, resource)
        if not allowed:
            await audit.log(
                "permission_check",
                actor.id,
                action,
                organization_id=actor.organization_id,
                resource_type=object_type,
                resource_id=resource,
                decision="denied",
            )
            raise PermissionDenied(actor.id, action, resource)
        decision = "granted"

    await audit.log(
        "permission_check",
        actor.id,
        action,
        organization_id=actor.organization_id,
        resource_type=object_type or "generic",
        resource_id=resource,
        decision=decision,
    )
    return True


# ── Tuple seeding helpers ────────────────────────────────────────────────────
# Called from membership/seed code so the authorization model reflects the DB.
# No-ops when OpenFGA is not configured, so they're safe to call unconditionally.


async def grant_org_membership(member_id: str, org_id: str, *, admin: bool = False) -> None:
    if not settings_openfga_configured():
        return
    relation = "admin" if admin else "member"
    await _write_tuples_idempotently(
        [(f"user:{member_id}", relation, f"organization:{org_id}")]
    )


async def grant_project_role(member_id: str, role: str, project_id: str, org_id: str) -> None:
    """Seed a project tuple for a member plus the project→org link.

    Maps DB roles to model relations: owner→owner, anything else→editor.
    """
    if not settings_openfga_configured():
        return
    relation = "owner" if role == "owner" else "editor"
    await _write_tuples_idempotently(
        [
            (f"user:{member_id}", relation, f"project:{project_id}"),
            (f"organization:{org_id}", "org", f"project:{project_id}"),
        ]
    )


async def revoke_project_role(member_id: str, role: str, project_id: str) -> None:
    if not settings_openfga_configured():
        return
    relation = "owner" if role == "owner" else "editor"
    await authz.delete_tuples([(f"user:{member_id}", relation, f"project:{project_id}")])


async def grant_workspace_role(member_id: str, role: str, workspace_id: str, org_id: str) -> None:
    """Seed a workspace tuple for a member plus the workspace→org link."""
    if not settings_openfga_configured():
        return
    relation = "owner" if role == "owner" else "editor"
    await _write_tuples_idempotently(
        [
            (f"user:{member_id}", relation, f"workspace:{workspace_id}"),
            (f"organization:{org_id}", "org", f"workspace:{workspace_id}"),
        ]
    )


async def revoke_workspace_role(member_id: str, role: str, workspace_id: str) -> None:
    if not settings_openfga_configured():
        return
    relation = "owner" if role == "owner" else "editor"
    await authz.delete_tuples([(f"user:{member_id}", relation, f"workspace:{workspace_id}")])


async def grant_task_role(member_id: str, role: str, task_id: str, org_id: str) -> None:
    """Seed a task tuple for a member plus the task→org link.

    Maps DB roles to model relations: owner→owner, anything else→editor.
    """
    if not settings_openfga_configured():
        return
    relation = "owner" if role == "owner" else "editor"
    await _write_tuples_idempotently(
        [
            (f"user:{member_id}", relation, f"task:{task_id}"),
            (f"organization:{org_id}", "org", f"task:{task_id}"),
        ]
    )


async def revoke_task_role(member_id: str, role: str, task_id: str) -> None:
    """Remove a task ownership/editor tuple for a member."""
    if not settings_openfga_configured():
        return
    relation = "owner" if role == "owner" else "editor"
    await authz.delete_tuples([(f"user:{member_id}", relation, f"task:{task_id}")])


async def _seed_task_org_link(task_id: str, org_id: str) -> None:
    """Write only the org→task link (for system/sub-agent tasks with no human owner)."""
    if not settings_openfga_configured():
        return
    await _write_tuples_idempotently(
        [(f"organization:{org_id}", "org", f"task:{task_id}")]
    )


def settings_openfga_configured() -> bool:
    """Tuples are only written when an OpenFGA server is configured."""
    from core.config import settings

    return bool(settings.openfga_api_url)


async def _write_tuples_idempotently(tuples: list[tuple[str, str, str]]) -> None:
    try:
        await authz.write_tuples(tuples)
    except AuthzUnavailable as exc:
        if "tuple to be written already existed" in str(exc):
            return
        raise


async def reconcile_org_tuples(org_id: str) -> dict[str, int]:
    """Backfill OpenFGA relationship tuples from the DB for an org.

    Idempotently writes tuples for every member, project-member, and workspace
    row in the org so that enabling FGA on a populated org does not lock out
    existing members.

    Returns a dict with the count of tuples written per category::

        {"members": n, "projects": n, "workspaces": n}

    No-ops (returns all-zero counts) when OpenFGA is not configured.
    """
    if not settings_openfga_configured():
        return {"members": 0, "projects": 0, "workspaces": 0, "tasks": 0}

    from core.db import engine, reflect_table

    # ── Members ─────────────────────────────────────────────────────────────
    members_tbl = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(members_tbl.c.id, members_tbl.c.role).where(
                    members_tbl.c.organization_id == org_id
                )
            )
        ).fetchall()

    member_count = 0
    for row in rows:
        admin = row.role in {"admin", "owner"}
        await grant_org_membership(row.id, org_id, admin=admin)
        member_count += 1

    # ── Project members ──────────────────────────────────────────────────────
    pm_tbl = await reflect_table("project_members")
    async with engine.begin() as conn:
        pm_rows = (
            await conn.execute(
                select(
                    pm_tbl.c.member_id,
                    pm_tbl.c.role,
                    pm_tbl.c.project_id,
                ).where(pm_tbl.c.organization_id == org_id)
            )
        ).fetchall()

    project_count = 0
    for row in pm_rows:
        await grant_project_role(row.member_id, row.role, row.project_id, org_id)
        project_count += 1

    # ── Workspace → org links ────────────────────────────────────────────────
    # There is no per-member workspace relationship to backfill — only the
    # workspace→org link (`organization:{org}` -[org]-> `workspace:{id}`) so
    # org-admin inheritance works.  The ``workspaces`` table may not exist in
    # all deployed schemas, so we guard against NoSuchTableError.
    workspace_count = 0
    try:
        ws_tbl = await reflect_table("workspaces")
        async with engine.begin() as conn:
            ws_rows = (
                await conn.execute(
                    select(ws_tbl.c.id).where(ws_tbl.c.organization_id == org_id)
                )
            ).fetchall()
        ws_tuples = [
            (f"organization:{org_id}", "org", f"workspace:{row.id}") for row in ws_rows
        ]
        if ws_tuples:
            await _write_tuples_idempotently(ws_tuples)
        workspace_count = len(ws_rows)
    except NoSuchTableError:
        workspace_count = 0

    # ── Tasks → owner tuples + org links ────────────────────────────────────
    # For each task with a real human triggered_by_member_id, write owner+org.
    # For system tasks (null or sentinel member), write only the org→task link
    # so org-admin inheritance still works.
    task_count = 0
    try:
        tasks_tbl = await reflect_table("tasks")
        async with engine.begin() as conn:
            task_rows = (
                await conn.execute(
                    select(tasks_tbl.c.id, tasks_tbl.c.triggered_by_member_id).where(
                        tasks_tbl.c.organization_id == org_id
                    )
                )
            ).fetchall()
        for row in task_rows:
            member_id = str(row.triggered_by_member_id) if row.triggered_by_member_id else None
            task_id = str(row.id)
            if member_id and member_id not in _INTERNAL_ACTOR_IDS:
                await grant_task_role(member_id, "owner", task_id, org_id)
            else:
                await _seed_task_org_link(task_id, org_id)
            task_count += 1
    except NoSuchTableError:
        task_count = 0

    counts: dict[str, int] = {
        "members": member_count,
        "projects": project_count,
        "workspaces": workspace_count,
        "tasks": task_count,
    }
    await audit.log(
        "authz_reconciled",
        None,
        "reconcile_org_tuples",
        organization_id=org_id,
        payload=counts,
    )
    return counts


async def reconcile_org_groups(org_id: str) -> dict[str, int]:
    """Backfill OpenFGA relationship tuples from SCIM group memberships for an org.

    For each ``scim_groups`` row + its ``group_memberships``, grants each member
    the org-level role that the group confers.  A member in multiple groups gets
    the highest role among those groups (matching ``recompute_member_role`` DB
    semantics).  The approach is *materialized*: we write direct
    ``user:{member_id}`` → ``member|admin`` → ``organization:{org_id}`` tuples
    rather than adding a ``group`` type to the authorization model.  This means
    the existing model is untouched (no live OpenFGA needed to validate a model
    change) and the required behavioral outcome — a member of an admin-role group
    ends up with ``admin`` on ``organization:{org_id}`` — is achieved via the
    same ``grant_org_membership`` helper already used for direct member backfill.

    Returns::

        {"groups": n_groups, "grants": n_member_grants}

    No-ops (returns all-zero counts) when OpenFGA is not configured.

    Known limitation: revocation is not covered here — removing a member from
    their sole admin group will not retract the FGA admin tuple.  That is out of
    scope for W2.3 (additive grants only) and should be addressed in a dedicated
    revoke pass.
    """
    if not settings_openfga_configured():
        return {"groups": 0, "grants": 0}

    from core.db import engine, reflect_table

    groups_tbl = await reflect_table("scim_groups")
    gm_tbl = await reflect_table("group_memberships")

    async with engine.begin() as conn:
        group_rows = (
            await conn.execute(
                select(groups_tbl.c.id, groups_tbl.c.role).where(
                    groups_tbl.c.organization_id == org_id
                )
            )
        ).fetchall()

    if not group_rows:
        return {"groups": 0, "grants": 0}

    # Collect per-member max role across all groups (mirrors recompute_member_role).
    # Use None as sentinel so a member in a single user-role group still gets granted.
    member_max_role: dict[str, str | None] = {}

    for group_row in group_rows:
        async with engine.begin() as conn:
            member_ids = (
                await conn.execute(
                    select(gm_tbl.c.member_id).where(
                        gm_tbl.c.organization_id == org_id,
                        gm_tbl.c.group_id == group_row.id,
                    )
                )
            ).scalars().all()
        for mid in member_ids:
            mid_str = str(mid)
            current = member_max_role.get(mid_str)
            if current is None or _rank(group_row.role) > _rank(current):
                member_max_role[mid_str] = group_row.role

    grant_count = 0
    for mid_str, role in member_max_role.items():
        if role is None:
            continue  # defensive: no group role resolved (should not happen)
        admin = role in {"admin", "owner"}
        await grant_org_membership(mid_str, org_id, admin=admin)
        grant_count += 1

    counts: dict[str, int] = {"groups": len(group_rows), "grants": grant_count}
    await audit.log(
        "authz_reconciled",
        None,
        "reconcile_org_groups",
        organization_id=org_id,
        payload=counts,
    )
    return counts
