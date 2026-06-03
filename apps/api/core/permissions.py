"""Permission seam.

The signature is frozen: ``check(actor, action, resource) -> bool``. 200+ call
sites depend on it. It always audits, and on success returns True. Enforcement
is OFF by default (Phase-1 allow-all stub behaviour); when an operator sets
``permissions_enforce`` and configures OpenFGA, project-scoped actions are
checked against the authorization model and a denial RAISES ``PermissionDenied``
(no call site reads the bool, so raising is the only way to actually block).

Actions that don't map to the model (generic checks, ``use_tool:*``) are allowed
— enforcement is targeted at project resources where relationships are seeded.
Internal/system actors (the agent runtime, schedulers, sync jobs) bypass FGA;
the broker's own safety limits still apply to their tool calls.
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

# Actors that represent the system itself, not a human — they bypass FGA.
_INTERNAL_ACTOR_IDS = {"chronos", "source_sync", "system", "scheduler"}
_INTERNAL_ROLES = {"agent", "system"}


def _relation_for(action: str) -> str | None:
    if action in _VIEW_ACTIONS:
        return "can_view"
    if action in _EDIT_ACTIONS:
        return "can_edit"
    if action in _MANAGE_ACTIONS:
        return "can_manage"
    return None


def _is_internal(actor: Member) -> bool:
    return actor.id in _INTERNAL_ACTOR_IDS or actor.role in _INTERNAL_ROLES


async def check(actor: Member, action: str, resource: str) -> bool:
    """Authorize ``actor`` to perform ``action`` on ``resource``.

    Returns True when allowed. Raises ``PermissionDenied`` when enforcement is on
    and the authorization model denies the action (or OpenFGA is unreachable —
    enforcement fails closed).
    """
    relation = _relation_for(action)
    enforce = authz.is_enabled() and relation is not None and not _is_internal(actor)

    decision = "granted_stub"
    if enforce:
        try:
            allowed = await authz.check(f"user:{actor.id}", relation, f"project:{resource}")
        except AuthzUnavailable:
            # Operator chose enforcement but the server is down → fail closed.
            await audit.log(
                "permission_check",
                actor.id,
                action,
                organization_id=actor.organization_id,
                resource_type="project",
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
                resource_type="project",
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
        resource_type="project" if relation else "generic",
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
    await authz.write_tuples([(f"user:{member_id}", relation, f"organization:{org_id}")])


async def grant_project_role(member_id: str, role: str, project_id: str, org_id: str) -> None:
    """Seed a project tuple for a member plus the project→org link.

    Maps DB roles to model relations: owner→owner, anything else→editor.
    """
    if not settings_openfga_configured():
        return
    relation = "owner" if role == "owner" else "editor"
    await authz.write_tuples(
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


def settings_openfga_configured() -> bool:
    """Tuples are only written when an OpenFGA server is configured."""
    from core.config import settings

    return bool(settings.openfga_api_url)
