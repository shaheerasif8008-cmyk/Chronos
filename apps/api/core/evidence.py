from __future__ import annotations
"""
Evidence bundles — portable, tamper-evident proof of an action_class's autonomy
history. Exports the append-only trust_events for a (scope x action_class),
computes a hash chain over them, and signs the chain head with the deployment
key. An auditor can recompute the chain offline and verify the signature to prove
the record wasn't altered after export.

This is the Graduated-Autonomy contribution to the broader Provable-Governance
pillar: "why was the AI allowed to do this unattended?" answered with signed,
replayable evidence.
"""
import hashlib
import hmac
import json
import logging
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)


def _signing_key() -> bytes:
    return (settings.vault_encryption_key or "chronos-dev-key").encode()


def _row_hash(prev_hash: str, event: dict[str, Any]) -> str:
    canonical = json.dumps(event, sort_keys=True, default=str)
    return hashlib.sha256(f"{prev_hash}:{canonical}".encode()).hexdigest()


def chain(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Return events annotated with running hashes plus the chain head hash."""
    prev = "genesis"
    chained: list[dict[str, Any]] = []
    for ev in events:
        h = _row_hash(prev, ev)
        chained.append({**ev, "_hash": h, "_prev": prev})
        prev = h
    return chained, prev


def sign(head_hash: str) -> str:
    return hmac.new(_signing_key(), head_hash.encode(), hashlib.sha256).hexdigest()


async def build_bundle(org_id: str, scope: str, action_class: str) -> dict[str, Any]:
    """Build a signed evidence bundle for an action_class within a scope."""
    events = await _load_events(org_id, scope, action_class)
    chained, head = chain(events)
    return {
        "organization_id": org_id,
        "scope": scope,
        "action_class": action_class,
        "event_count": len(events),
        "events": chained,
        "chain_head": head,
        "signature": sign(head),
        "algorithm": "sha256-chain + hmac-sha256",
    }


def verify(bundle: dict[str, Any]) -> bool:
    """Recompute the chain and signature; True iff the bundle is intact."""
    raw = [
        {k: v for k, v in ev.items() if k not in ("_hash", "_prev")}
        for ev in bundle.get("events", [])
    ]
    _, head = chain(raw)
    if head != bundle.get("chain_head"):
        return False
    return hmac.compare_digest(sign(head), bundle.get("signature", ""))


async def _load_events(org_id: str, scope: str, action_class: str) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("trust_events")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(
                        table.c.id,
                        table.c.action_class,
                        table.c.tool,
                        table.c.risk_score,
                        table.c.outcome,
                        table.c.approval_id,
                        table.c.actor_id,
                        table.c.created_at,
                    )
                    .where(
                        table.c.organization_id == org_id,
                        table.c.scope == scope,
                        table.c.action_class == action_class,
                    )
                    .order_by(table.c.created_at.asc(), table.c.id.asc())
                )
            ).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("evidence load degraded: %s", exc)
        return []
