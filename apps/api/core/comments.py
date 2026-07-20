"""Comments & mentions — the collaboration layer.

A comment attaches to a target entity (``project``, ``task``, or ``artifact``)
and may ``@mention`` org members. Mentions are resolved against the org member
directory and then **filtered to members who can already see the target**, so a
mention never notifies — or even records — someone outside the target's access
scope. Each surviving mention emits an in-app notification via ``core.notifications``.

This module owns the reusable, side-effect-light pieces (mention parsing,
resolution, target access checks, and the DB create/list/delete) so they can be
unit-tested directly. The router does auth + orchestration on top.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, insert, select, update

from core import audit
from core.config import settings as app_settings
from core.db import engine, reflect_table

VALID_TARGET_TYPES = {"project", "task", "artifact"}

# @handle where handle is an email local-part (``@jane``) or a full email
# (``@jane@acme.com``). Punctuation that commonly trails a mention (".", ",",
# ")", etc.) is excluded so "thanks @jane!" resolves to "jane".
_MENTION_RE = re.compile(r"@([A-Za-z0-9._+-]+(?:@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?)")


def parse_mention_tokens(body: str | None) -> list[str]:
    """Distinct, lower-cased mention tokens in *body*, order-preserving."""
    if not body:
        return []
    seen: dict[str, None] = {}
    for raw in _MENTION_RE.findall(body):
        tok = raw.lower().rstrip(".")
        if tok and tok not in seen:
            seen[tok] = None
    return list(seen.keys())


def _match_token(token: str, members: list[dict[str, Any]]) -> str | None:
    """Resolve a single mention *token* to a member id, or None.

    Matches on full email, email local-part, or a whitespace-stripped name —
    all case-insensitive. The first member in directory order wins on ties.
    """
    token = token.lower()
    for m in members:
        email = (m.get("email") or "").lower()
        if not email:
            continue
        local = email.split("@", 1)[0]
        name = (m.get("name") or "").lower().replace(" ", "")
        if token in (email, local, name):
            return str(m["id"])
    return None


async def _org_members(organization_id: str) -> list[dict[str, Any]]:
    members = await reflect_table("members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    members.c.id,
                    members.c.email,
                    members.c.name,
                    members.c.role,
                    members.c.region,
                    members.c.status,
                ).where(
                    members.c.organization_id == organization_id,
                    members.c.status == "active",
                )
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def _active_member(organization_id: str, member_id: str):
    """Load a minimal active member context without crossing the tenant."""

    from core.models import Member

    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    members.c.id,
                    members.c.organization_id,
                    members.c.region,
                    members.c.email,
                    members.c.role,
                    members.c.name,
                    members.c.status,
                ).where(
                    members.c.id == member_id,
                    members.c.organization_id == organization_id,
                    members.c.status == "active",
                )
            )
        ).mappings().first()
    return Member(**dict(row)) if row else None


async def resolve_mentions(organization_id: str, body: str | None) -> list[str]:
    """Resolve mention tokens in *body* to distinct member ids within the org.

    Returns only members that exist in the org. Access-scope filtering (whether
    each member can see the comment's target) is applied separately by the
    caller via :func:`member_can_access_target`.
    """
    tokens = parse_mention_tokens(body)
    if not tokens:
        return []
    members = await _org_members(organization_id)
    resolved: dict[str, None] = {}
    for tok in tokens:
        mid = _match_token(tok, members)
        if mid is not None:
            resolved[mid] = None
    return list(resolved.keys())


async def member_can_access_target(
    organization_id: str, member_id: str, target_type: str, target_id: str
) -> bool:
    """Whether *member_id* can see the comment target.

    - ``project``: must hold a ``project_members`` row for the project.
    - ``task``: creator-only, with the same org-admin break-glass policy as the
      task API.
    - ``artifact``: delegates to the canonical artifact visibility helper,
      including creator, parent task/conversation, project membership, and
      org-admin access.
    """
    if target_type == "project":
        project_members = await reflect_table("project_members")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(project_members.c.id).where(
                        and_(
                            project_members.c.project_id == target_id,
                            project_members.c.member_id == member_id,
                            project_members.c.organization_id == organization_id,
                        )
                    )
                )
            ).first()
        return row is not None

    member = await _active_member(organization_id, member_id)
    if member is None:
        return False

    if target_type == "task":
        from core.task_access import visible_task

        return await visible_task(member, target_id) is not None

    from core.artifact_access import artifact_access

    artifacts = await reflect_table("artifacts")
    conditions = [
        artifacts.c.id == target_id,
        artifacts.c.organization_id == organization_id,
    ]
    if "is_deleted" in artifacts.c:
        conditions.append(artifacts.c.is_deleted.is_(False))
    async with engine.begin() as conn:
        row = (
            await conn.execute(select(artifacts).where(*conditions))
        ).mappings().first()
    if row is None:
        return False
    visible, _writable = await artifact_access(member, dict(row))
    return visible


async def create_comment(
    *,
    organization_id: str,
    target_type: str,
    target_id: str,
    author_member_id: str,
    body: str,
    mentions: list[str],
) -> dict[str, Any]:
    """Insert a comment row and return it as a dict."""
    table = await reflect_table("comments")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                insert(table)
                .values(
                    organization_id=organization_id,
                    region=app_settings.region,
                    target_type=target_type,
                    target_id=target_id,
                    author_member_id=author_member_id,
                    body=body,
                    mentions=mentions,
                )
                .returning(
                    table.c.id,
                    table.c.target_type,
                    table.c.target_id,
                    table.c.author_member_id,
                    table.c.body,
                    table.c.mentions,
                    table.c.created_at,
                )
            )
        ).mappings().first()
    result = dict(row)
    await audit.log(
        "comment",
        author_member_id,
        "comment_created",
        organization_id=organization_id,
        resource_type=target_type,
        resource_id=target_id,
        payload={"comment_id": str(result["id"]), "mentions": mentions},
    )
    return result


async def list_comments(
    organization_id: str, target_type: str, target_id: str, *, limit: int = 200
) -> list[dict[str, Any]]:
    table = await reflect_table("comments")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table)
                .where(
                    and_(
                        table.c.organization_id == organization_id,
                        table.c.target_type == target_type,
                        table.c.target_id == target_id,
                        table.c.deleted_at.is_(None),
                    )
                )
                .order_by(table.c.created_at.asc())
                .limit(limit)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


async def get_comment(organization_id: str, comment_id: str) -> dict[str, Any] | None:
    table = await reflect_table("comments")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(
                    and_(
                        table.c.id == comment_id,
                        table.c.organization_id == organization_id,
                        table.c.deleted_at.is_(None),
                    )
                )
            )
        ).mappings().first()
    return dict(row) if row else None


async def soft_delete_comment(organization_id: str, comment_id: str) -> int:
    table = await reflect_table("comments")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        result = await conn.execute(
            update(table)
            .where(
                and_(
                    table.c.id == comment_id,
                    table.c.organization_id == organization_id,
                    table.c.deleted_at.is_(None),
                )
            )
            .values(deleted_at=now)
        )
    return result.rowcount or 0
