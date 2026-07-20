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
_ROLE_RANK = {
    "viewer": 0,
    "user": 2,       # legacy standard-member role; equivalent to operator
    "operator": 2,
    "approver": 2,
    "manager": 3,
    "admin": 4,
    "owner": 5,
}


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
_WORKSPACE_CONTEXT_READ_ACTIONS = {"list_connector_tools"}
_WORKSPACE_CONTEXT_WRITE_ACTIONS = {
    "create_task",
    "create_workflow",
    "create_connector_plan",
    "execute_connector_plan",
    "execute_connector_tool_call",
}

# action → relation on task:{resource}
_TASK_VIEW_ACTIONS = {
    "view_task",
    "view_task_events",
    "stream_task",
    "view_task_assignment",
}
_TASK_EDIT_ACTIONS = {"handoff_task"}
_TASK_MANAGE_ACTIONS = {
    "cancel_task",
    "retry_task",
    "pause_task",
    "resume_task",
    "assign_task",
    "unassign_task",
}

# action → relation on conversation:{resource}. Database ACL checks remain the
# canonical non-enumerating boundary; OpenFGA mirrors them when enabled so a
# policy outage or stale client can never broaden access.
_CONVERSATION_VIEW_ACTIONS = {"view_conversation"}
_CONVERSATION_EDIT_ACTIONS = {
    "rename_conversation",
    "pin_message",
    "unpin_message",
    "edit_message",
    "branch_conversation",
    "save_to_memory",
    "convert_to_task",
    "convert_to_workflow",
    "regenerate_message",
    "retry_from_message",
}
_CONVERSATION_MANAGE_ACTIONS = {
    "delete_conversation",
    "share_conversation",
}

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
    "export_memory",
    "list_audit_log",
    "export_audit_log",
    "export_compliance",
    "deliver_notifications",
    "view_admin_console",
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
    "set_tool_permission",
    "manage_custom_http_connectors",
    "manage_webhook_endpoints",
    "manage_native_groups",
    "manage_workspaces",
    "transfer_organization_ownership",
    "manage_organization_api_keys",
    "manage_agent_publications",
    "manage_file_quarantine",
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
    "update_agent",
    "delete_agent",
    "run_agent",
    "create_browser_session",
    "create_computer_session",
    # Comment routers apply the canonical, tenant-scoped target ACL after this
    # audited seam (project membership, task visibility, or artifact access).
    # Keep these actions explicit: leaving them unmapped makes every legitimate
    # comment read/write fail closed before the target ACL can run.
    "create_comment",
    "create_dataset",
    "create_desktop_session",
    "create_desktop_pair_code",
    "create_local_computer_grant",
    "create_memory",
    "create_monitor",
    "create_project",
    "create_research",
    "create_schedule",
    "create_task",
    "create_workflow",
    "create_workflow_trigger",
    "delete_comment",
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
    "list_accessible_workspaces",
    "list_agents",
    "list_approvals",
    "list_browser_sessions",
    "list_chat_models",
    "list_chat_modes",
    "list_collaboration_members",
    "list_computer_sessions",
    "list_context_suggestions",
    "list_conversations",
    "list_datasets",
    "list_desktop_sessions",
    "list_desktop_devices",
    "list_desktop_device_grants",
    "list_local_computer_grants",
    "list_monitor_alerts",
    "list_monitors",
    "list_projects",
    "list_schedule_runs",
    "list_schedules",
    "list_skills",
    "list_tasks",
    "list_workflow_runs",
    "list_workflow_triggers",
    "list_workflows",
    "pause_workflow_run",
    "publish_agent",
    "invoke_agent_publication",
    "read_settings",
    "read_comment",
    "recover_workflows",
    "reject_context_suggestion",
    "request_browser_takeover",
    "resume_workflow_run",
    "resolve_connector_approval",
    "revoke_browser_session",
    "revoke_desktop_session",
    "revoke_desktop_device",
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
    "list_tool_permissions",
    "execute_connector_tool_call",
    "list_mcp_servers",
    "list_connector_policies",
    # Conversation and task collaboration actions are relationship-mapped
    # below; listing stays generic because it is filtered by canonical SQL ACLs.
    "artifact.create",
    "artifact.read",
    "artifact.edit",
    "artifact.delete",
    "artifact.publish",
}

# Actors that represent the system itself, not a human — they bypass FGA.
_INTERNAL_ACTOR_IDS = {"chronos", "source_sync", "system", "scheduler"}
_INTERNAL_ROLES = {"agent", "system"}

# Absolute minimum roles for high-impact generic actions that do not yet have a
# dedicated OpenFGA resource type. Organization settings may further restrict
# these actions, but can never lower this floor.
_MANAGER_FLOOR_ACTIONS = {
    "create_agent",
    "update_agent",
    "delete_agent",
    "skill.write",
    "create_schedule",
    "list_schedules",
    "update_schedule",
    "delete_schedule",
    "create_workflow",
    "create_workflow_trigger",
    "create_monitor",
    "update_monitor",
    "evaluate_monitor",
}
_ADMIN_FLOOR_ACTIONS = {
    "create_project",
    "publish_agent",
    "artifact.publish",
    "generate_context_suggestion",
    "list_context_suggestions",
    "apply_context_suggestion",
    "reject_context_suggestion",
}
_OPERATOR_FLOOR_ACTIONS = {
    "analyze_dataset",
    "create_dataset",
    "create_browser_session",
    "create_computer_session",
    "create_desktop_session",
    "create_desktop_pair_code",
    "create_local_computer_grant",
    "list_browser_sessions",
    "list_computer_sessions",
    "list_desktop_sessions",
    "list_desktop_devices",
    "list_desktop_device_grants",
    "list_local_computer_grants",
    "view_browser_session",
    "view_browser_session_events",
    "view_computer_session_events",
    "view_desktop_session_events",
    "view_local_computer_events",
    "request_browser_takeover",
    "approve_browser_sensitive_site",
    "hand_back_browser_session",
    "revoke_browser_session",
    "close_browser_session",
    "revoke_desktop_session",
    "revoke_desktop_device",
    "close_desktop_session",
    "revoke_local_computer_grant",
    "list_approvals",
    "view_approval",
    "list_connectors",
    "list_connector_tools",
    "list_connector_actions",
    "execute_connector_tool_call",
    "execute_connector_action",
    "list_skills",
    "skill.run_script",
    "list_memory",
    "create_memory",
    "update_memory",
    "delete_memory",
    "undo_memory",
    "stream_memory_events",
    "save_to_memory",
    "artifact.create",
    "run_agent",
}

_CAPABILITY_ACTIONS = {
    "workspace": _MANAGER_FLOOR_ACTIONS
    | _ADMIN_FLOOR_ACTIONS
    | {
        "list_workflows",
        "list_workflow_runs",
        "list_workflow_triggers",
        "start_workflow_run",
        "pause_workflow_run",
        "resume_workflow_run",
        "cancel_workflow_run",
        "complete_workflow_step",
        "dispatch_workflow_event",
        "recover_workflows",
        "run_schedule",
        "list_schedule_runs",
        "list_monitors",
        "list_monitor_alerts",
    },
    "employee": {"create_agent", "update_agent", "delete_agent", "skill.write"},
    "tools": _OPERATOR_FLOOR_ACTIONS
    | {
        "connect_gmail",
        "list_tool_permissions",
        "list_connector_policies",
        "list_mcp_servers",
    },
    "approvals": _APPROVAL_DECISION_ACTIONS | {"list_approvals", "view_approval"},
    "memory": {
        "list_memory",
        "create_memory",
        "update_memory",
        "delete_memory",
        "undo_memory",
        "stream_memory_events",
        "save_to_memory",
    },
    "audit": {"list_audit_log", "export_audit_log", "export_compliance"},
}


def _minimum_rank(action: str) -> int:
    if action in _ADMIN_FLOOR_ACTIONS:
        return _ROLE_RANK["admin"]
    if action in _MANAGER_FLOOR_ACTIONS:
        return _ROLE_RANK["manager"]
    if action in _OPERATOR_FLOOR_ACTIONS:
        return _ROLE_RANK["operator"]
    if action.startswith("use_tool:") or action.startswith("connect_") or action.startswith("disconnect_"):
        return _ROLE_RANK["operator"]
    return _ROLE_RANK["viewer"]


def _capability_for(action: str) -> str | None:
    if action.startswith("use_tool:") or action.startswith("connect_") or action.startswith("disconnect_"):
        return "tools"
    # A few high-impact employee actions also sit in the broader workspace
    # floor.  Resolve the narrower capability first so the saved role matrix
    # cannot accidentally treat manager-level employee administration as a
    # generic workspace mutation (which managers deny by default).
    for capability in ("employee", "approvals", "memory", "audit", "tools", "workspace"):
        actions = _CAPABILITY_ACTIONS[capability]
        if action in actions:
            return capability
    return None


async def _role_policy_allows(actor: Member, action: str) -> bool:
    """Apply the persisted role matrix as a further-restrictive policy layer."""

    capability = _capability_for(action)
    if capability is None or actor.role in _ADMIN_ROLES:
        return True
    role = "operator" if actor.role in {"user", "approver"} else actor.role
    try:
        from core.settings_store import DEFAULTS, get_settings_doc

        document = await get_settings_doc(actor, "permissions")
        roles = document.get("roles") or DEFAULTS["permissions"]["roles"]
    except Exception:
        # The immutable defaults remain the fail-closed source if the settings
        # store is temporarily unavailable.
        from core.settings_store import DEFAULTS

        roles = DEFAULTS["permissions"]["roles"]
    decision = str((roles.get(role) or {}).get(capability, "deny"))
    return decision in {"allow", "approval_required"}


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
    if action in _TASK_EDIT_ACTIONS:
        return "can_edit", "task"
    if action in _TASK_MANAGE_ACTIONS:
        return "can_manage", "task"
    if action in _CONVERSATION_VIEW_ACTIONS:
        return "can_view", "conversation"
    if action in _CONVERSATION_EDIT_ACTIONS:
        return "can_edit", "conversation"
    if action in _CONVERSATION_MANAGE_ACTIONS:
        return "can_manage", "conversation"
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
    if not _is_internal(actor) and actor.auth_type == "api_key":
        scopes = set(actor.api_key_scopes)
        if action in _ADMIN_ACTIONS:
            required_scope = "admin"
        elif action.startswith(("read", "list", "get", "view", "search", "stream")):
            required_scope = "read"
        else:
            required_scope = "write"
        scope_allowed = (
            "admin" in scopes
            or required_scope in scopes
            or (required_scope == "read" and "write" in scopes)
        )
        if not scope_allowed:
            await audit.log(
                "permission_check",
                actor.id,
                action,
                organization_id=actor.organization_id,
                resource_type="organization_api_key",
                resource_id=actor.api_key_id or resource,
                decision="denied_api_key_scope",
                payload={"required_scope": required_scope, "scopes": sorted(scopes)},
            )
            raise PermissionDenied(actor.id, action, resource)

    if action in _WORKSPACE_CONTEXT_READ_ACTIONS | _WORKSPACE_CONTEXT_WRITE_ACTIONS:
        from core.workspace_access import require_workspace_access

        access = "read" if action in _WORKSPACE_CONTEXT_READ_ACTIONS else "write"
        try:
            await require_workspace_access(actor, resource, access=access)
        except PermissionDenied:
            await audit.log(
                "permission_check",
                actor.id,
                action,
                organization_id=actor.organization_id,
                resource_type="workspace",
                resource_id=resource,
                decision="denied_workspace_membership_or_state",
            )
            raise

    if not _is_internal(actor):
        required_rank = _minimum_rank(action)
        if _rank(actor.role) < required_rank:
            await audit.log(
                "permission_check",
                actor.id,
                action,
                organization_id=actor.organization_id,
                resource_type="role_floor",
                resource_id=resource,
                decision="denied_role_floor",
                payload={"role": actor.role, "required_rank": required_rank},
            )
            raise PermissionDenied(actor.id, action, resource)
        if not await _role_policy_allows(actor, action):
            await audit.log(
                "permission_check",
                actor.id,
                action,
                organization_id=actor.organization_id,
                resource_type="role_policy",
                resource_id=resource,
                decision="denied_role_policy",
                payload={"role": actor.role, "capability": _capability_for(action)},
            )
            raise PermissionDenied(actor.id, action, resource)

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

    # Explicit generic actions are granted by the audited deterministic
    # allowlist above; this is a real policy decision, not a placeholder.
    decision = "granted_allowlist"
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


async def _delete_tuple_idempotently(tuple_key: tuple[str, str, str]) -> None:
    """Delete one tuple while tolerating the already-absent case only."""

    try:
        await authz.delete_tuples([tuple_key])
    except AuthzUnavailable as exc:
        message = str(exc).lower()
        if (
            "tuple to be deleted was not found" in message
            or "cannot delete a tuple which does not exist" in message
        ):
            return
        raise


async def sync_org_membership(
    member_id: str,
    org_id: str,
    *,
    role: str,
    active: bool = True,
) -> None:
    """Converge one member's materialized org tuple to DB desired state.

    SCIM removals and deactivation are security-sensitive: an additive grant
    pass can leave a former admin authorized indefinitely. Remove every
    relation that is no longer desired before writing the current one.
    """

    if not settings_openfga_configured():
        return
    user = f"user:{member_id}"
    organization = f"organization:{org_id}"
    desired = "admin" if role in {"admin", "owner"} else "member"
    relations_to_delete = (
        ("member", "admin")
        if not active
        else tuple(
            relation for relation in ("member", "admin") if relation != desired
        )
    )
    for relation in relations_to_delete:
        await _delete_tuple_idempotently((user, relation, organization))
    if active:
        await _write_tuples_idempotently([(user, desired, organization)])


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


async def sync_project_visibility(project_id: str, org_id: str, visibility: str) -> None:
    """Mirror organization-wide project view visibility into OpenFGA."""
    if not settings_openfga_configured():
        return
    value = str(visibility or "private").strip().lower()
    if value not in {"private", "organization"}:
        raise ValueError("invalid project visibility")
    relationship = (
        f"organization:{org_id}#member",
        "organization_viewer",
        f"project:{project_id}",
    )
    if value == "organization":
        await _write_tuples_idempotently([relationship])
    else:
        await _delete_tuple_idempotently(relationship)


async def grant_workspace_role(member_id: str, role: str, workspace_id: str, org_id: str) -> None:
    """Seed a workspace tuple for a member plus the workspace→org link."""
    if not settings_openfga_configured():
        return
    relation = role if role in {"owner", "editor", "viewer"} else "viewer"
    await _write_tuples_idempotently(
        [
            (f"user:{member_id}", relation, f"workspace:{workspace_id}"),
            (f"organization:{org_id}", "org", f"workspace:{workspace_id}"),
        ]
    )


async def revoke_workspace_role(member_id: str, role: str, workspace_id: str) -> None:
    if not settings_openfga_configured():
        return
    relation = role if role in {"owner", "editor", "viewer"} else "viewer"
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
    await _delete_tuple_idempotently(
        (f"user:{member_id}", relation, f"task:{task_id}")
    )


async def grant_conversation_role(
    member_id: str,
    role: str,
    conversation_id: str,
    org_id: str,
) -> None:
    """Mirror one durable conversation ACL row into OpenFGA."""

    if not settings_openfga_configured():
        return
    relation = role if role in {"owner", "editor", "viewer"} else "viewer"
    await _write_tuples_idempotently(
        [
            (f"user:{member_id}", relation, f"conversation:{conversation_id}"),
            (f"organization:{org_id}", "org", f"conversation:{conversation_id}"),
        ]
    )


async def revoke_conversation_role(
    member_id: str,
    role: str,
    conversation_id: str,
) -> None:
    if not settings_openfga_configured():
        return
    relation = role if role in {"owner", "editor", "viewer"} else "viewer"
    await _delete_tuple_idempotently(
        (f"user:{member_id}", relation, f"conversation:{conversation_id}")
    )


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

    Returns a dict with the count of durable rows mirrored per category::

        {"members": n, "projects": n, "workspaces": n,
         "tasks": n, "conversations": n}

    No-ops (returns all-zero counts) when OpenFGA is not configured.
    """
    if not settings_openfga_configured():
        return {
            "members": 0,
            "projects": 0,
            "workspaces": 0,
            "tasks": 0,
            "conversations": 0,
        }

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

    # ── Project members + organization visibility ───────────────────────────
    pm_tbl = await reflect_table("project_members")
    projects_tbl = await reflect_table("projects")
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
        visible_project_rows = (
            await conn.execute(
                select(projects_tbl.c.id, projects_tbl.c.visibility).where(
                    projects_tbl.c.organization_id == org_id,
                    projects_tbl.c.visibility == "organization",
                )
            )
        ).fetchall()

    project_count = 0
    for row in pm_rows:
        await grant_project_role(row.member_id, row.role, row.project_id, org_id)
        project_count += 1
    for row in visible_project_rows:
        await sync_project_visibility(str(row.id), org_id, str(row.visibility))

    # ── Workspace membership + org links ────────────────────────────────────
    workspace_count = 0
    try:
        ws_tbl = await reflect_table("workspaces")
        wm_tbl = await reflect_table("workspace_members")
        async with engine.begin() as conn:
            ws_rows = (
                await conn.execute(
                    select(ws_tbl.c.id).where(ws_tbl.c.organization_id == org_id)
                )
            ).fetchall()
            wm_rows = (
                await conn.execute(
                    select(
                        wm_tbl.c.member_id,
                        wm_tbl.c.role,
                        wm_tbl.c.workspace_id,
                    ).where(wm_tbl.c.organization_id == org_id)
                )
            ).fetchall()
        ws_tuples = [
            (f"organization:{org_id}", "org", f"workspace:{row.id}") for row in ws_rows
        ]
        if ws_tuples:
            await _write_tuples_idempotently(ws_tuples)
        for row in wm_rows:
            await grant_workspace_role(
                str(row.member_id), str(row.role), str(row.workspace_id), org_id
            )
        workspace_count = len(wm_rows)
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
                select(
                    tasks_tbl.c.id,
                    tasks_tbl.c.triggered_by_member_id,
                    tasks_tbl.c.assignee_member_id,
                ).where(
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
            assignee_id = str(row.assignee_member_id) if row.assignee_member_id else None
            if assignee_id and assignee_id != member_id:
                await grant_task_role(assignee_id, "editor", task_id, org_id)
            task_count += 1
    except NoSuchTableError:
        task_count = 0

    # ── Conversation owner/share ACL tuples ─────────────────────────────────
    conversation_count = 0
    try:
        conversations_tbl = await reflect_table("conversations")
        conversation_members_tbl = await reflect_table("conversation_members")
        async with engine.begin() as conn:
            conversation_rows = (
                await conn.execute(
                    select(
                        conversation_members_tbl.c.conversation_id,
                        conversation_members_tbl.c.member_id,
                        conversation_members_tbl.c.role,
                    )
                    .join(
                        conversations_tbl,
                        conversations_tbl.c.id
                        == conversation_members_tbl.c.conversation_id,
                    )
                    .where(
                        conversation_members_tbl.c.organization_id == org_id,
                        conversations_tbl.c.organization_id == org_id,
                    )
                )
            ).fetchall()
        for row in conversation_rows:
            await grant_conversation_role(
                str(row.member_id),
                str(row.role),
                str(row.conversation_id),
                org_id,
            )
            conversation_count += 1
    except NoSuchTableError:
        conversation_count = 0

    counts: dict[str, int] = {
        "members": member_count,
        "projects": project_count,
        "workspaces": workspace_count,
        "tasks": task_count,
        "conversations": conversation_count,
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
    """Converge materialized OpenFGA roles to SCIM/DB desired state.

    Member roles are recomputed from group memberships before this function is
    called. Reconcile every member, not only current group members, so removal
    from a final admin group deletes the stale ``admin`` tuple. Deactivated
    members receive no organization tuple.

    Returns::

        {"groups": n_groups, "grants": n_member_grants}

    No-ops (returns all-zero counts) when OpenFGA is not configured.
    """
    if not settings_openfga_configured():
        return {"groups": 0, "grants": 0}

    from core.db import engine, reflect_table

    groups_tbl = await reflect_table("scim_groups")
    members_tbl = await reflect_table("members")

    async with engine.begin() as conn:
        group_rows = (
            await conn.execute(
                select(groups_tbl.c.id, groups_tbl.c.role).where(
                    groups_tbl.c.organization_id == org_id
                )
            )
        ).fetchall()
        member_rows = (
            await conn.execute(
                select(
                    members_tbl.c.id,
                    members_tbl.c.role,
                    members_tbl.c.status,
                ).where(members_tbl.c.organization_id == org_id)
            )
        ).mappings().all()

    grant_count = 0
    for member_row in member_rows:
        active = str(member_row.get("status") or "active") == "active"
        await sync_org_membership(
            str(member_row["id"]),
            org_id,
            role=str(member_row.get("role") or "user"),
            active=active,
        )
        if active:
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
