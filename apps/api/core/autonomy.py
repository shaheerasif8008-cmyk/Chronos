from __future__ import annotations
"""
Autonomy Gate — turns a risk price + earned trust into an allow / require-approval
decision. This is the one new decision point in the ToolBroker.

It is strictly *additive to safety*:
* It can let an *earned* action skip the settings-policy approval gate
  (graduated autonomy), but it never lowers the broker's hard floor or safety
  ceiling — those run before the gate and are absolute.
* It can *tighten* a lenient policy when a human-ratified learned policy matches
  the call (a guardrail synthesized from a past rejection).

Order of checks (supervised workspaces):
  1. full_auto short-circuit (legacy collapse of the settings gate).
  2. learned policy match (enforced human-ratified rules win).
  3. if the settings policy doesn't require approval -> ALLOW (unchanged baseline).
  4. HIGH risk tier -> always approval.
  5. earned graduation: risk.value <= trust.auto_threshold (MEDIUM also needs a
     human-set graduated_by) -> ALLOW.
  6. otherwise -> require approval.
"""
import logging
from dataclasses import dataclass

from core import learned_policy, trust
from core.risk import RiskScore

log = logging.getLogger(__name__)

# Anomaly circuit-breaker: a graduated action_class seeing more than this many
# calls in the trailing window is treated as an anomalous burst — re-gated to
# human approval and recorded as an incident (which trips the trust breaker).
_ANOMALY_WINDOW_SECONDS = 300
_ANOMALY_BURST_THRESHOLD = 30


@dataclass(frozen=True)
class GateDecision:
    allow: bool
    reason: str
    action_class: str


async def _matching_learned_policy(org_id: str, action_class: str, args: dict) -> dict | None:
    """Return an enabled, ratified learned policy whose matcher fits the args."""
    try:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("learned_policies")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(table.c.matcher, table.c.decision).where(
                        table.c.organization_id == org_id,
                        table.c.action_class == action_class,
                        table.c.enabled.is_(True),
                        table.c.ratified_by.isnot(None),
                    )
                )
            ).mappings().all()
    except Exception as exc:
        log.debug("learned policy read degraded: %s", exc)
        return None

    for row in rows:
        if learned_policy.matcher_fits(row["matcher"] or {}, args):
            return {"decision": row["decision"]}
    return None


async def evaluate(
    org_id: str,
    workspace_id: str | None,
    risk: RiskScore,
    args: dict,
    policy: dict,
    autonomy: str,
) -> GateDecision:
    klass = risk.action_class

    # 1. full_auto collapses the settings-policy gate (legacy behavior). The hard
    #    floor + safety limits already ran in the broker and still apply.
    if autonomy == "full_auto":
        return GateDecision(True, "full_auto", klass)

    # 2. Human-ratified learned guardrails win over earned trust.
    learned = await _matching_learned_policy(org_id, klass, args)
    if learned is not None:
        return GateDecision(False, f"learned policy ({learned['decision']})", klass)

    # 3. Baseline: if settings don't require approval, nothing to graduate past.
    if policy.get("approval_required") is not True:
        return GateDecision(True, "policy allows", klass)

    # 4. High-risk actions never auto-graduate.
    if risk.tier == "high":
        return GateDecision(False, "high-risk action requires approval", klass)

    # 5. Earned graduation.
    level = await trust.get_trust_level(org_id, workspace_id, klass)
    if level.auto_threshold is not None and risk.value <= level.auto_threshold:
        if risk.tier == "medium" and level.graduated_by in (None, "seed"):
            # Medium tier needs a *named human* to ratify graduation.
            return GateDecision(False, "medium-risk graduation needs human ratification", klass)

        # 5b. Anomaly circuit-breaker: a graduated action seeing a sudden burst is
        #     re-gated to a human and recorded as an incident (trips the breaker so
        #     the next call is also gated until trust is re-earned).
        burst = await trust.recent_event_count(
            org_id, workspace_id, klass, window_seconds=_ANOMALY_WINDOW_SECONDS
        )
        if burst >= _ANOMALY_BURST_THRESHOLD:
            await trust.record_outcome(
                org_id, workspace_id, risk, "incident", tool=klass.split(":", 1)[0]
            )
            return GateDecision(False, f"anomalous burst ({burst} in window) — re-gated", klass)

        return GateDecision(
            True,
            f"graduated (trust={level.trust_score:.2f}, by={level.graduated_by})",
            klass,
        )

    return GateDecision(False, "not yet graduated for auto-execution", klass)
