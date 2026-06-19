from __future__ import annotations
"""
Learned policies — guardrails synthesized from approval rejections.

When a human rejects an action with a note ("never email anyone at
competitor.com"), that note is the highest-signal training data the system has.
We extract a structured matcher from it (LLM, best-effort) and store a *proposed*
learned_policies row. It is NOT enforced until a named human ratifies it
(``ratified_by`` set, ``enabled=true``) — at which point the Autonomy Gate
applies it *before* any earned trust, and an auditor gets a complete answer:
this person, this rejection, this date, these words.
"""
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

_SYNTHESIS_PROMPT = """A human reviewer just REJECTED an automated action. Turn their reason into a
reusable guardrail so the same kind of action is caught next time.

Action: {action_class}
Action arguments (redacted): {args}
Reviewer's rejection note: "{note}"

Return JSON only:
{{
  "matcher": {{ "<field>": "<substring that must be present in the args to match>" }},
  "decision": "deny" | "require_approval",
  "rationale": "one short sentence"
}}

Rules:
- The matcher should capture the SPECIFIC thing the reviewer objected to (a domain,
  a recipient, a keyword), not the whole action. Use lowercase substrings.
- Use "deny" only when the note is an absolute prohibition ("never", "not allowed").
  Otherwise use "require_approval".
- If you cannot extract a concrete matcher, return {{"matcher": {{}}, "decision": "require_approval", "rationale": "..."}}.
"""


def matcher_fits(matcher: dict, args: dict) -> bool:
    """A matcher fits if every substring it asserts appears in the args."""
    if not matcher:
        return False
    blob = repr(args).lower()
    return all(str(v).lower() in blob for v in matcher.values())


async def synthesize_from_rejection(
    *,
    org_id: str,
    region: str,
    action_class: str,
    args: dict,
    note: str | None,
    source_approval_id: str | None = None,
) -> str | None:
    """Propose a learned policy from a rejection note. Returns the new row id or None.

    Best-effort: a missing model, unparseable output, or missing ledger all degrade
    to "no proposal" rather than raising into the approval-decision request.
    """
    if not note or not note.strip():
        return None
    try:
        from core import llm

        raw = await llm.complete_json(
            _SYNTHESIS_PROMPT.format(
                action_class=action_class,
                args=_redact(args),
                note=note.strip(),
            )
        )
        parsed = json.loads(raw)
    except Exception as exc:
        log.debug("learned policy synthesis skipped: %s", exc)
        return None

    matcher = parsed.get("matcher") or {}
    decision = parsed.get("decision") or "require_approval"
    if not isinstance(matcher, dict) or not matcher or decision not in ("deny", "require_approval"):
        return None

    try:
        from sqlalchemy import insert

        from core.db import engine, reflect_table

        table = await reflect_table("learned_policies")
        async with engine.begin() as conn:
            result = await conn.execute(
                insert(table)
                .values(
                    organization_id=org_id,
                    region=region,
                    action_class=action_class,
                    matcher=matcher,
                    decision=decision,
                    source_approval_id=source_approval_id,
                    derived_from_note=note.strip(),
                    ratified_by=None,   # proposed; not enforced until a human confirms
                    enabled=False,
                )
                .returning(table.c.id)
            )
            return str(result.scalar_one())
    except Exception as exc:
        log.debug("learned policy persist skipped: %s", exc)
        return None


def _redact(args: dict) -> dict:
    """Drop obviously bulky/credential-ish fields before sending to the model."""
    out = {}
    for k, v in (args or {}).items():
        if k.startswith("__"):
            continue
        if isinstance(v, str) and len(v) > 200:
            v = v[:200] + "…"
        out[k] = v
    return out


async def list_policies(org_id: str, *, include_proposed: bool = True) -> list[dict[str, Any]]:
    try:
        from sqlalchemy import select

        from core.db import engine, reflect_table

        table = await reflect_table("learned_policies")
        stmt = select(table).where(table.c.organization_id == org_id)
        if not include_proposed:
            stmt = stmt.where(table.c.enabled.is_(True))
        async with engine.begin() as conn:
            rows = (await conn.execute(stmt.order_by(table.c.created_at.desc()))).mappings().all()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.debug("learned policy list degraded: %s", exc)
        return []


async def confirm(org_id: str, policy_id: str, *, ratified_by: str) -> bool:
    """Ratify a proposed policy: a named human enables enforcement."""
    return await _set(org_id, policy_id, ratified_by=ratified_by, enabled=True)


async def disable(org_id: str, policy_id: str) -> bool:
    return await _set(org_id, policy_id, enabled=False)


async def _set(org_id: str, policy_id: str, **values) -> bool:
    try:
        from sqlalchemy import update

        from core.db import engine, reflect_table

        table = await reflect_table("learned_policies")
        async with engine.begin() as conn:
            result = await conn.execute(
                update(table)
                .where(table.c.organization_id == org_id, table.c.id == policy_id)
                .values(**values)
            )
        return result.rowcount > 0
    except Exception as exc:
        log.debug("learned policy update degraded: %s", exc)
        return False
