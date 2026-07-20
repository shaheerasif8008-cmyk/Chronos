"""Comments & mentions tests.

Two layers:
- Pure unit tests for mention parsing/resolution (core.comments).
- Router orchestration tests for access gating, mention scope-filtering, and the
  mention→notification emission that is the matrix acceptance proof
  ("Mention creates notification and access respects role").

Mocking mirrors test_project_sources.py: monkeypatch the module-level async
helpers / engine and assert on captured calls.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fastapi import HTTPException


def _make_member(member_id="author-1", org_id="default", role="user", name="Jane Doe"):
    from core.models import Member
    return Member(id=member_id, organization_id=org_id, email="jane@acme.com", role=role, name=name)


# ─── Pure: mention parsing ────────────────────────────────────────────────────

def test_parse_mention_tokens_handles_local_part_full_email_and_punctuation():
    from core import comments
    body = "thanks @jane, please loop in @bob@acme.com and @Carol! cc @jane"
    tokens = comments.parse_mention_tokens(body)
    # distinct, lower-cased, order-preserving, trailing punctuation stripped
    assert tokens == ["jane", "bob@acme.com", "carol"]


def test_parse_mention_tokens_empty():
    from core import comments
    assert comments.parse_mention_tokens("") == []
    assert comments.parse_mention_tokens(None) == []
    assert comments.parse_mention_tokens("no mentions here") == []


def test_match_token_by_email_local_and_name():
    from core import comments
    members = [
        {"id": "m1", "email": "jane@acme.com", "name": "Jane Doe"},
        {"id": "m2", "email": "bob@acme.com", "name": "Bob"},
    ]
    assert comments._match_token("jane", members) == "m1"          # local part
    assert comments._match_token("bob@acme.com", members) == "m2"  # full email
    assert comments._match_token("janedoe", members) == "m1"       # name w/o spaces
    assert comments._match_token("nobody", members) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["read_comment", "create_comment", "delete_comment"])
async def test_comment_actions_reach_canonical_target_acl(monkeypatch, action):
    """The permission seam must not pre-empt the router's scoped target ACL."""
    from core import permissions

    monkeypatch.setattr(permissions.audit, "log", AsyncMock())
    assert await permissions.check(
        _make_member(role="admin"), action, "project:proj-1"
    )


@pytest.mark.asyncio
async def test_resolve_mentions_dedupes_to_member_ids(monkeypatch):
    from core import comments
    members = [
        {"id": "m1", "email": "jane@acme.com", "name": "Jane Doe"},
        {"id": "m2", "email": "bob@acme.com", "name": "Bob"},
    ]
    monkeypatch.setattr(comments, "_org_members", AsyncMock(return_value=members))
    ids = await comments.resolve_mentions("default", "hi @jane and @bob and @jane again")
    assert ids == ["m1", "m2"]


# ─── Router: create with mention emits a notification (access respects role) ──

@pytest.mark.asyncio
async def test_create_comment_emits_notification_for_in_scope_mention(monkeypatch):
    from routers import comments as router
    from core import comments as core_comments

    member = _make_member()
    created_row = {
        "id": "c-1", "target_type": "project", "target_id": "proj-1",
        "author_member_id": "author-1", "body": "hey @bob",
        "mentions": ["m2"], "created_at": datetime.now(timezone.utc),
    }

    monkeypatch.setattr(router.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(core_comments, "member_can_access_target", AsyncMock(return_value=True))
    monkeypatch.setattr(core_comments, "resolve_mentions", AsyncMock(return_value=["m2"]))
    monkeypatch.setattr(core_comments, "create_comment", AsyncMock(return_value=created_row))
    emit = AsyncMock(return_value="notif-1")
    monkeypatch.setattr(router.notifications, "emit", emit)

    req = router.CreateCommentRequest(target_type="project", target_id="proj-1", body="hey @bob")
    out = await router.create_comment(req, member=member)

    assert out["id"] == "c-1"
    assert out["mentions"] == ["m2"]
    emit.assert_awaited_once()
    kwargs = emit.await_args.kwargs
    assert kwargs["type"] == "mention"
    assert kwargs["member_id"] == "m2"
    assert kwargs["resource_type"] == "project"
    assert kwargs["resource_id"] == "proj-1"


@pytest.mark.asyncio
async def test_create_comment_filters_out_of_scope_mention(monkeypatch):
    """A mentioned member who cannot see the target is neither recorded nor notified."""
    from routers import comments as router
    from core import comments as core_comments

    member = _make_member()

    async def access(org, mid, ttype, tid):
        # author can see it; the mentioned member m2 cannot.
        return mid == "author-1"

    captured = {}

    async def fake_create(**kwargs):
        captured.update(kwargs)
        return {**kwargs, "id": "c-2", "created_at": datetime.now(timezone.utc)}

    monkeypatch.setattr(router.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(core_comments, "member_can_access_target", access)
    monkeypatch.setattr(core_comments, "resolve_mentions", AsyncMock(return_value=["m2"]))
    monkeypatch.setattr(core_comments, "create_comment", fake_create)
    emit = AsyncMock()
    monkeypatch.setattr(router.notifications, "emit", emit)

    req = router.CreateCommentRequest(target_type="project", target_id="proj-1", body="hey @bob")
    out = await router.create_comment(req, member=member)

    assert out["mentions"] == []          # filtered out
    assert captured["mentions"] == []     # not persisted
    emit.assert_not_awaited()             # not notified


@pytest.mark.asyncio
async def test_create_comment_non_member_gets_404(monkeypatch):
    from routers import comments as router
    from core import comments as core_comments

    member = _make_member()
    monkeypatch.setattr(router.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(core_comments, "member_can_access_target", AsyncMock(return_value=False))
    create = AsyncMock()
    monkeypatch.setattr(core_comments, "create_comment", create)

    req = router.CreateCommentRequest(target_type="project", target_id="proj-1", body="hi")
    with pytest.raises(HTTPException) as exc:
        await router.create_comment(req, member=member)
    assert exc.value.status_code == 404
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_comment_invalid_target_type_422(monkeypatch):
    from routers import comments as router
    monkeypatch.setattr(router.permissions, "check", AsyncMock(return_value=True))
    req = router.CreateCommentRequest(target_type="wormhole", target_id="x", body="hi")
    with pytest.raises(HTTPException) as exc:
        await router.create_comment(req, member=_make_member())
    assert exc.value.status_code == 422


# ─── Router: delete authorization ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_comment_non_author_non_admin_403(monkeypatch):
    from routers import comments as router
    from core import comments as core_comments

    member = _make_member(member_id="someone-else", role="user")
    monkeypatch.setattr(
        core_comments, "get_comment",
        AsyncMock(return_value={"id": "c-1", "author_member_id": "author-1"}),
    )
    soft = AsyncMock()
    monkeypatch.setattr(core_comments, "soft_delete_comment", soft)

    with pytest.raises(HTTPException) as exc:
        await router.delete_comment("c-1", member=member)
    assert exc.value.status_code == 403
    soft.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_comment_author_succeeds(monkeypatch):
    from routers import comments as router
    from core import comments as core_comments

    member = _make_member(member_id="author-1")
    monkeypatch.setattr(
        core_comments, "get_comment",
        AsyncMock(return_value={"id": "c-1", "author_member_id": "author-1"}),
    )
    monkeypatch.setattr(router.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(core_comments, "soft_delete_comment", AsyncMock(return_value=1))
    monkeypatch.setattr(router.audit, "log", AsyncMock())

    out = await router.delete_comment("c-1", member=member)
    assert out["deleted"] is True


@pytest.mark.asyncio
async def test_delete_comment_admin_can_delete_others(monkeypatch):
    from routers import comments as router
    from core import comments as core_comments

    member = _make_member(member_id="admin-9", role="admin")
    monkeypatch.setattr(
        core_comments, "get_comment",
        AsyncMock(return_value={"id": "c-1", "author_member_id": "author-1"}),
    )
    monkeypatch.setattr(router.permissions, "check", AsyncMock(return_value=True))
    monkeypatch.setattr(core_comments, "soft_delete_comment", AsyncMock(return_value=1))
    monkeypatch.setattr(router.audit, "log", AsyncMock())

    out = await router.delete_comment("c-1", member=member)
    assert out["deleted"] is True
