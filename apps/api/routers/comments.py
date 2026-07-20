"""Comments & mentions router — the collaboration layer.

Endpoints to comment on a ``project``, ``task``, or ``artifact``, list a target's
thread, and delete your own comments. ``@mentions`` are resolved to org members,
filtered to those who can see the target, recorded on the comment, and surfaced
to each mentioned member as an in-app notification.

Access model:
- Reading/commenting requires the caller to be able to see the target (project
  membership for projects; canonical member-level privacy for tasks/artifacts).
  Non-access returns 404 so target existence is not leaked.
- Deletion is limited to the comment author or an org ``admin``/``owner``.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from core import audit, comments, notifications, permissions
from core.auth import get_current_member
from core.models import Member

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/comments", tags=["comments"])

_ADMIN_ROLES = {"admin", "owner"}


def _snippet(text: str, *, max_len: int = 140) -> str:
    text = text.strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")


async def _require_target_access(member: Member, target_type: str, target_id: str) -> None:
    """Raise 404 unless *member* can see the target (don't leak existence)."""
    if target_type not in comments.VALID_TARGET_TYPES:
        raise HTTPException(status_code=422, detail="Invalid target_type")
    allowed = await comments.member_can_access_target(
        member.organization_id, member.id, target_type, target_id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail=f"{target_type} not found")


class CreateCommentRequest(BaseModel):
    target_type: str
    target_id: str
    body: str

    @field_validator("body")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("body must not be empty")
        return v


@router.post("")
async def create_comment(
    req: CreateCommentRequest, member: Member = Depends(get_current_member)
) -> dict:
    await permissions.check(member, "create_comment", f"{req.target_type}:{req.target_id}")
    await _require_target_access(member, req.target_type, req.target_id)

    # Resolve mentions to org members, then keep only those who can see the
    # target — a mention must never leak the target to someone out of scope.
    candidate_ids = await comments.resolve_mentions(member.organization_id, req.body)
    mention_ids: list[str] = []
    for mid in candidate_ids:
        if await comments.member_can_access_target(
            member.organization_id, mid, req.target_type, req.target_id
        ):
            mention_ids.append(mid)

    row = await comments.create_comment(
        organization_id=member.organization_id,
        target_type=req.target_type,
        target_id=req.target_id,
        author_member_id=member.id,
        body=req.body,
        mentions=mention_ids,
    )
    comment_id = str(row["id"])

    # Notify each mentioned member (never self). Best-effort: a notification
    # failure must not fail the comment write.
    author_label = member.name or member.email
    for mid in mention_ids:
        if mid == member.id:
            continue
        try:
            await notifications.emit(
                organization_id=member.organization_id,
                type="mention",
                title=f"{author_label} mentioned you",
                body=_snippet(req.body),
                severity="info",
                member_id=mid,
                resource_type=req.target_type,
                resource_id=req.target_id,
                created_by=member.id,
            )
        except Exception:  # noqa: BLE001 — best-effort delivery
            logger.warning("mention notification failed for member %s", mid, exc_info=True)

    return {
        "id": comment_id,
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "author_member_id": row["author_member_id"],
        "body": row["body"],
        "mentions": list(row["mentions"] or []),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


@router.get("")
async def list_comments(
    target_type: str, target_id: str, member: Member = Depends(get_current_member)
) -> list[dict]:
    await permissions.check(member, "read_comment", f"{target_type}:{target_id}")
    await _require_target_access(member, target_type, target_id)
    rows = await comments.list_comments(member.organization_id, target_type, target_id)
    return [
        {
            "id": str(r["id"]),
            "target_type": r["target_type"],
            "target_id": r["target_id"],
            "author_member_id": r["author_member_id"],
            "body": r["body"],
            "mentions": list(r["mentions"] or []),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        }
        for r in rows
    ]


@router.delete("/{comment_id}")
async def delete_comment(
    comment_id: str, member: Member = Depends(get_current_member)
) -> dict:
    row = await comments.get_comment(member.organization_id, comment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    is_author = str(row["author_member_id"]) == str(member.id)
    if not is_author and member.role not in _ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Only the author or an admin can delete")
    await permissions.check(member, "delete_comment", f"comment:{comment_id}")
    deleted = await comments.soft_delete_comment(member.organization_id, comment_id)
    await audit.log(
        "comment",
        member.id,
        "comment_deleted",
        organization_id=member.organization_id,
        resource_type="comments",
        resource_id=comment_id,
    )
    return {"deleted": deleted > 0}
