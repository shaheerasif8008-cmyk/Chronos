from __future__ import annotations
"""
Trust Ledger — the earned, evidenced standing behind Graduated Autonomy.

Two stores:
* ``trust_levels``  — current EWMA standing per (workspace scope x action_class).
* ``trust_events``  — append-only evidence trail (immutable, like audit_log).

Trust is earned slowly and lost instantly:
* auto_success raises it, approved raises it less (a human had to step in),
* rejected / incident / reverted collapse it and trip the circuit breaker
  (auto_threshold -> NULL), dropping the action_class back to human approval.

Hybrid graduation by risk tier:
* LOW    — auto-graduates once the bar is met (graduated_by='system').
* MEDIUM — earns the score but needs a named human to set graduated_by.
* HIGH   — never auto-graduates.

All DB access is wrapped: if the ledger is unavailable (migration not applied,
stubbed config in tests) the broker still works — cold-start defaults apply and
recording is a no-op. Trust can only *loosen* governance; never break it.
"""
import logging
from dataclasses import dataclass

from core.risk import SEEDED_AUTO_CLASSES, RiskScore, risk_tier

log = logging.getLogger(__name__)

# EWMA: recent behavior dominates (~last 15 events).
_ALPHA = 0.15
# Per-outcome contribution to the score.
_OUTCOME_VALUE = {
    "auto_success": 1.0,
    "approved": 0.7,     # positive, but a human had to intervene
    "rejected": 0.0,
    "incident": 0.0,
    "reverted": 0.0,
}
_NEGATIVE_OUTCOMES = {"rejected", "incident", "reverted"}

# Low-tier auto-graduation bar.
_GRADUATE_SCORE_BAR = 0.8
_GRADUATE_MIN_SAMPLE = 20
# Risk ceiling granted on auto-graduation (covers low-tier headroom).
_GRADUATED_THRESHOLD = 0.4
# Knockdown ceiling applied to trust_score when the breaker trips.
_KNOCKDOWN = 0.3


@dataclass(frozen=True)
class TrustLevel:
    action_class: str
    trust_score: float
    auto_threshold: float | None
    graduated_by: str | None
    successes: int
    rejections: int
    incidents: int


def scope_for(workspace_id: str | None) -> str:
    """Trust accumulates per workspace so autonomy never leaks across teams."""
    return f"workspace:{workspace_id or 'default'}"


def _seed(action_class: str) -> TrustLevel:
    """Cold-start standing for an action_class with no ledger row yet."""
    base_tool = action_class.split(":", 1)[0]
    threshold = SEEDED_AUTO_CLASSES.get(base_tool)
    return TrustLevel(
        action_class=action_class,
        trust_score=0.0,
        auto_threshold=threshold,
        graduated_by="seed" if threshold is not None else None,
        successes=0,
        rejections=0,
        incidents=0,
    )


async def get_trust_level(org_id: str, workspace_id: str | None, action_class: str) -> TrustLevel:
    scope = scope_for(workspace_id)
    try:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("trust_levels")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(
                        table.c.trust_score,
                        table.c.auto_threshold,
                        table.c.graduated_by,
                        table.c.successes,
                        table.c.rejections,
                        table.c.incidents,
                    ).where(
                        table.c.organization_id == org_id,
                        table.c.scope == scope,
                        table.c.action_class == action_class,
                    )
                )
            ).mappings().first()
    except Exception as exc:  # ledger unavailable -> cold-start defaults
        log.debug("trust ledger read degraded: %s", exc)
        return _seed(action_class)

    if not row:
        return _seed(action_class)
    return TrustLevel(
        action_class=action_class,
        trust_score=float(row["trust_score"]),
        auto_threshold=row["auto_threshold"],
        graduated_by=row["graduated_by"],
        successes=int(row["successes"]),
        rejections=int(row["rejections"]),
        incidents=int(row["incidents"]),
    )


def novelty_from_successes(successes: int, *, seen_floor: int = 20) -> float:
    """Map an action_class's track record to a novelty factor in [0,1].

    1.0 = never seen (cold start), decaying toward 0 as it becomes routine. The
    pricer weights novelty so unestablished actions price slightly higher.
    """
    if successes <= 0:
        return 1.0
    return max(0.0, 1.0 - min(1.0, successes / float(seen_floor)))


async def recent_event_count(
    org_id: str, workspace_id: str | None, action_class: str, *, window_seconds: int
) -> int:
    """Count ledger events for an action_class in the trailing window.

    Feeds the anomaly circuit-breaker: a sudden burst of a graduated action is
    treated as anomalous and re-gated to human approval. Degrades to 0 (no
    anomaly signal) when the ledger is unavailable.
    """
    scope = scope_for(workspace_id)
    try:
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import func, select

        from core.db import engine, reflect_table

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        events = await reflect_table("trust_events")
        async with engine.begin() as conn:
            count = (
                await conn.execute(
                    select(func.count()).where(
                        events.c.organization_id == org_id,
                        events.c.scope == scope,
                        events.c.action_class == action_class,
                        events.c.created_at >= cutoff,
                    )
                )
            ).scalar_one()
        return int(count or 0)
    except Exception as exc:
        log.debug("trust recent-count degraded: %s", exc)
        return 0


async def list_levels(org_id: str, workspace_id: str | None = None) -> list[dict]:
    """Trust dashboard data: current standing per action_class."""
    try:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("trust_levels")
        stmt = select(table).where(table.c.organization_id == org_id)
        if workspace_id is not None:
            stmt = stmt.where(table.c.scope == scope_for(workspace_id))
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt.order_by(table.c.trust_score.desc()))).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("trust list degraded: %s", exc)
        return []


async def list_proposals(org_id: str) -> list[dict]:
    """Action_classes whose track record meets the bar but await human graduation.

    These are the MEDIUM/HIGH (or not-yet-graduated) classes an admin can ratify.
    Annotated with the mean observed risk so the reviewer sees the tier.
    """
    levels = await list_levels(org_id)
    out = []
    for lvl in levels:
        ready = (
            lvl["graduated_by"] in (None, "seed")
            and lvl["trust_score"] >= _GRADUATE_SCORE_BAR
            and lvl["successes"] >= _GRADUATE_MIN_SAMPLE
            and lvl["rejections"] == 0
        )
        if ready:
            out.append(lvl)
    return out


async def set_graduation(
    org_id: str, scope: str, action_class: str, *, auto_threshold: float, graduated_by: str
) -> bool:
    """A named human ratifies graduation for an action_class (MEDIUM/HIGH path)."""
    from datetime import datetime, timezone

    return await _admin_update(
        org_id, scope, action_class,
        auto_threshold=max(0.0, min(1.0, auto_threshold)),
        graduated_by=graduated_by,
        graduated_at=datetime.now(timezone.utc),
        demoted_at=None,
    )


async def demote(org_id: str, scope: str, action_class: str) -> bool:
    """Manually revoke autonomy for an action_class."""
    from datetime import datetime, timezone

    return await _admin_update(
        org_id, scope, action_class,
        auto_threshold=None, graduated_by=None, demoted_at=datetime.now(timezone.utc),
    )


async def _admin_update(org_id: str, scope: str, action_class: str, **values) -> bool:
    try:
        from sqlalchemy import update

        from core.db import engine, reflect_table

        table = await reflect_table("trust_levels")
        async with engine.begin() as conn:
            result = await conn.execute(
                update(table)
                .where(
                    table.c.organization_id == org_id,
                    table.c.scope == scope,
                    table.c.action_class == action_class,
                )
                .values(**values)
            )
        return result.rowcount > 0
    except Exception as exc:
        log.debug("trust admin update degraded: %s", exc)
        return False


async def record_outcome(
    org_id: str,
    workspace_id: str | None,
    risk: RiskScore,
    outcome: str,
    *,
    region: str = "us",
    tool: str | None = None,
    actor_id: str | None = None,
    approval_id: str | None = None,
) -> None:
    """Append an immutable trust_event and fold the outcome into trust_levels.

    Best-effort: any failure degrades to a no-op so a missing ledger never blocks
    a tool call. The append-only trust_events table is the durable evidence.
    """
    scope = scope_for(workspace_id)
    value = _OUTCOME_VALUE.get(outcome, 0.0)
    tool = tool or risk.action_class.split(":", 1)[0]
    try:
        from datetime import datetime, timezone

        from sqlalchemy import insert, select, update

        from core.db import engine, reflect_table

        levels = await reflect_table("trust_levels")
        events = await reflect_table("trust_events")
        now = datetime.now(timezone.utc)

        async with engine.begin() as conn:
            await conn.execute(
                insert(events).values(
                    organization_id=org_id,
                    region=region,
                    scope=scope,
                    action_class=risk.action_class,
                    tool=tool,
                    risk_score=risk.value,
                    outcome=outcome,
                    approval_id=approval_id,
                    actor_id=actor_id,
                )
            )

            row = (
                await conn.execute(
                    select(levels).where(
                        levels.c.organization_id == org_id,
                        levels.c.scope == scope,
                        levels.c.action_class == risk.action_class,
                    )
                )
            ).mappings().first()

            prev_score = float(row["trust_score"]) if row else 0.0
            successes = int(row["successes"]) if row else 0
            rejections = int(row["rejections"]) if row else 0
            incidents = int(row["incidents"]) if row else 0
            auto_threshold = row["auto_threshold"] if row else None
            graduated_by = row["graduated_by"] if row else None
            graduated_at = row["graduated_at"] if row else None

            new_score = (1 - _ALPHA) * prev_score + _ALPHA * value
            if outcome in ("auto_success", "approved"):
                successes += 1
            elif outcome == "rejected":
                rejections += 1
            elif outcome in ("incident", "reverted"):
                incidents += 1

            demoted_at = row["demoted_at"] if row else None
            if outcome in _NEGATIVE_OUTCOMES:
                # Circuit breaker: collapse the score and revoke autonomy.
                new_score = min(new_score, _KNOCKDOWN)
                auto_threshold = None
                graduated_by = None
                graduated_at = None
                demoted_at = now
            elif (
                risk_tier(risk.value) == "low"
                and auto_threshold is None
                and new_score >= _GRADUATE_SCORE_BAR
                and successes >= _GRADUATE_MIN_SAMPLE
                and rejections == 0
            ):
                # Low-tier hybrid graduation: no human needed, fully audited.
                auto_threshold = _GRADUATED_THRESHOLD
                graduated_by = "system"
                graduated_at = now

            common = dict(
                trust_score=new_score,
                successes=successes,
                rejections=rejections,
                incidents=incidents,
                auto_threshold=auto_threshold,
                graduated_by=graduated_by,
                graduated_at=graduated_at,
                demoted_at=demoted_at,
                updated_at=now,
            )
            if row:
                await conn.execute(
                    update(levels)
                    .where(levels.c.id == row["id"])
                    .values(**common)
                )
            else:
                await conn.execute(
                    insert(levels).values(
                        organization_id=org_id,
                        region=region,
                        scope=scope,
                        action_class=risk.action_class,
                        **common,
                    )
                )
    except Exception as exc:  # never block a tool call on ledger trouble
        log.debug("trust ledger write degraded: %s", exc)
