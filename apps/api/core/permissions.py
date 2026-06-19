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

Actions that don't map to either layer (generic checks, ``use_tool:*``) are
allowed and audited. Internal/system actors (the agent runtime, schedulers, sync
jobs) bypass FGA; the broker's own safety limits still apply to their tool calls.
"""
from __future__ import annotations

from core import audit, authz
from core.authz import AuthzUnavailable
from core.exceptions import PermissionDenied
from core.models import Member

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

# Deciding an approval (approving/rejecting a risky write) is the core enterprise
# governance gate. It is enforced deterministically by role — independent of
# OpenFGA — so the guarantee "an unauthorized user cannot approve" holds even
# when no policy engine is configured. Only these roles may decide.
_APPROVAL_DECISION_ACTIONS = {"decide_approval"}
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
}
_ADMIN_ROLES = {"admin", "owner"}

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
    return None


def _is_internal(actor: Member) -> bool:
    return actor.id in _INTERNAL_ACTOR_IDS or actor.role in _INTERNAL_ROLES


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

    mapped = _resource_for(action)
    relation, object_type = mapped if mapped else (None, None)
    enforce = authz.is_enabled() and relation is not None and not _is_internal(actor)

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
