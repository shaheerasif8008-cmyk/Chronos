from __future__ import annotations
"""
Graduated Autonomy admin/approver API.

Powers the trust dashboard, graduation proposals + ratification, learned-policy
confirmation, the risk-override registry, and signed evidence-bundle export. All
mutations are admin-gated and audited.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from core import audit, evidence, learned_policy, permissions, risk_registry, trust
from core.auth import get_current_member
from core.models import Member

router = APIRouter(prefix="/autonomy", tags=["autonomy"])


class GraduateRequest(BaseModel):
    scope: str
    action_class: str
    auto_threshold: float = 0.5


class DemoteRequest(BaseModel):
    scope: str
    action_class: str


class RiskOverrideRequest(BaseModel):
    tool: str
    blast_radius: float
    irreversibility: float


@router.get("/trust")
async def trust_dashboard(
    workspace_id: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict]:
    await permissions.check(member, "view_autonomy", member.organization_id)
    return await trust.list_levels(member.organization_id, workspace_id)


@router.get("/proposals")
async def graduation_proposals(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "view_autonomy", member.organization_id)
    return await trust.list_proposals(member.organization_id)


@router.post("/graduate")
async def graduate(req: GraduateRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "graduate_autonomy", f"{req.scope}:{req.action_class}")
    ok = await trust.set_graduation(
        member.organization_id, req.scope, req.action_class,
        auto_threshold=req.auto_threshold, graduated_by=member.id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No trust record for that scope/action_class")
    await audit.log(
        "autonomy_graduation", member.id, "graduate",
        organization_id=member.organization_id, resource_type="trust_level",
        resource_id=f"{req.scope}:{req.action_class}",
        payload={"auto_threshold": req.auto_threshold}, decision="granted",
    )
    return {"status": "graduated", "action_class": req.action_class, "by": member.id}


@router.post("/demote")
async def demote(req: DemoteRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "demote_autonomy", f"{req.scope}:{req.action_class}")
    ok = await trust.demote(member.organization_id, req.scope, req.action_class)
    if not ok:
        raise HTTPException(status_code=404, detail="No trust record for that scope/action_class")
    await audit.log(
        "autonomy_demotion", member.id, "demote",
        organization_id=member.organization_id, resource_type="trust_level",
        resource_id=f"{req.scope}:{req.action_class}", decision="revoked",
    )
    return {"status": "demoted", "action_class": req.action_class}


@router.get("/learned-policies")
async def list_learned_policies(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "view_autonomy", member.organization_id)
    return await learned_policy.list_policies(member.organization_id)


@router.post("/learned-policies/{policy_id}/confirm")
async def confirm_learned_policy(policy_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "confirm_learned_policy", policy_id)
    ok = await learned_policy.confirm(member.organization_id, policy_id, ratified_by=member.id)
    if not ok:
        raise HTTPException(status_code=404, detail="Policy not found")
    await audit.log(
        "learned_policy_confirm", member.id, "confirm",
        organization_id=member.organization_id, resource_type="learned_policy",
        resource_id=policy_id, decision="ratified",
    )
    return {"status": "enabled", "policy_id": policy_id}


@router.post("/learned-policies/{policy_id}/disable")
async def disable_learned_policy(policy_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "disable_learned_policy", policy_id)
    ok = await learned_policy.disable(member.organization_id, policy_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Policy not found")
    await audit.log(
        "learned_policy_disable", member.id, "disable",
        organization_id=member.organization_id, resource_type="learned_policy",
        resource_id=policy_id, decision="disabled",
    )
    return {"status": "disabled", "policy_id": policy_id}


@router.get("/risk-overrides")
async def list_risk_overrides(member: Member = Depends(get_current_member)) -> list[dict]:
    await permissions.check(member, "view_autonomy", member.organization_id)
    return await risk_registry.list_overrides(member.organization_id)


@router.put("/risk-overrides")
async def upsert_risk_override(req: RiskOverrideRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "set_risk_override", req.tool)
    await risk_registry.upsert(
        member.organization_id, member.region, req.tool, req.blast_radius, req.irreversibility
    )
    await audit.log(
        "risk_override", member.id, "upsert",
        organization_id=member.organization_id, resource_type="risk_override",
        resource_id=req.tool,
        payload={"blast_radius": req.blast_radius, "irreversibility": req.irreversibility},
    )
    return {"status": "saved", "tool": req.tool}


@router.get("/evidence")
async def evidence_bundle(
    scope: str = Query(...),
    action_class: str = Query(...),
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "export_evidence", f"{scope}:{action_class}")
    bundle = await evidence.build_bundle(member.organization_id, scope, action_class)
    await audit.log(
        "evidence_export", member.id, "export",
        organization_id=member.organization_id, resource_type="trust_events",
        resource_id=f"{scope}:{action_class}",
        payload={"event_count": bundle["event_count"], "chain_head": bundle["chain_head"]},
    )
    return bundle
