"""End-to-end backend proof for explicit collaboration boundaries."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from core import comments
from core.db import engine, reflect_table
from core.models import Member
from routers import chat, settings as settings_router, tasks as task_router
from tests.workspace_fixtures import ensure_default_workspace


async def _seed() -> tuple[Member, Member, Member, Member, str, str]:
    org_id = str(uuid.uuid4())
    foreign_org_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    editor_id = str(uuid.uuid4())
    next_id = str(uuid.uuid4())
    foreign_id = str(uuid.uuid4())
    conversation_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    conversations = await reflect_table("conversations")
    messages = await reflect_table("messages")
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        await conn.execute(
            organizations.insert(),
            [
                {
                    "id": org_id,
                    "slug": f"collab-{org_id[:8]}",
                    "name": "Collaboration Org",
                },
                {
                    "id": foreign_org_id,
                    "slug": f"foreign-{foreign_org_id[:8]}",
                    "name": "Foreign Org",
                },
            ],
        )
        await conn.execute(
            members.insert(),
            [
                {
                    "id": owner_id,
                    "organization_id": org_id,
                    "email": f"{owner_id[:8]}@collab.test",
                    "role": "operator",
                    "status": "active",
                },
                {
                    "id": editor_id,
                    "organization_id": org_id,
                    "email": f"{editor_id[:8]}@collab.test",
                    "role": "operator",
                    "status": "active",
                },
                {
                    "id": next_id,
                    "organization_id": org_id,
                    "email": f"{next_id[:8]}@collab.test",
                    "role": "operator",
                    "status": "active",
                },
                {
                    "id": foreign_id,
                    "organization_id": foreign_org_id,
                    "email": f"{foreign_id[:8]}@foreign.test",
                    "role": "operator",
                    "status": "active",
                },
            ],
        )
    workspace_id = await ensure_default_workspace(
        org_id, [owner_id, editor_id, next_id]
    )
    await ensure_default_workspace(foreign_org_id, [foreign_id])
    async with engine.begin() as conn:
        await conn.execute(
            conversations.insert().values(
                id=conversation_id,
                organization_id=org_id,
                member_id=owner_id,
                title="Private launch plan",
                workspace_id=workspace_id,
            )
        )
        await conn.execute(
            messages.insert().values(
                organization_id=org_id,
                conversation_id=conversation_id,
                role="user",
                content="Initial private context",
            )
        )
        await conn.execute(
            tasks.insert().values(
                id=task_id,
                organization_id=org_id,
                triggered_by="manual",
                triggered_by_member_id=owner_id,
                status="queued",
                goal="Prepare the client handoff",
                plan={},
                result={},
            )
        )

    def _member(member_id: str, organization_id: str, domain: str) -> Member:
        return Member(
            id=member_id,
            organization_id=organization_id,
            email=f"{member_id[:8]}@{domain}",
            role="operator",
        )

    return (
        _member(owner_id, org_id, "collab.test"),
        _member(editor_id, org_id, "collab.test"),
        _member(next_id, org_id, "collab.test"),
        _member(foreign_id, foreign_org_id, "foreign.test"),
        conversation_id,
        task_id,
    )


@pytest.mark.asyncio
async def test_conversation_private_default_share_roles_and_revoke():
    owner, collaborator, _next, foreign, conversation_id, _task_id = await _seed()

    owner_rows = await chat.list_conversations(owner)
    collaborator_rows = await chat.list_conversations(collaborator)
    assert conversation_id in {str(row["id"]) for row in owner_rows}
    assert conversation_id not in {str(row["id"]) for row in collaborator_rows}
    with pytest.raises(HTTPException) as hidden:
        await chat.list_messages(conversation_id, collaborator)
    assert hidden.value.status_code == 404

    viewer = await chat.put_conversation_member(
        conversation_id,
        collaborator.id,
        chat.ConversationMemberRequest(role="viewer"),
        owner,
    )
    assert viewer["role"] == "viewer"
    assert conversation_id in {
        str(row["id"]) for row in await chat.list_conversations(collaborator)
    }
    assert len(await chat.list_messages(conversation_id, collaborator)) == 1
    with pytest.raises(HTTPException) as read_only:
        await chat._save_message(
            conversation_id,
            "user",
            "viewer must not write",
            _member_id=collaborator.id,
            _org_id=collaborator.organization_id,
            _member_role=collaborator.role,
        )
    assert read_only.value.status_code == 403

    editor = await chat.put_conversation_member(
        conversation_id,
        collaborator.id,
        chat.ConversationMemberRequest(role="editor"),
        owner,
    )
    assert editor["role"] == "editor"
    await chat._save_message(
        conversation_id,
        "user",
        "editor contribution",
        _member_id=collaborator.id,
        _org_id=collaborator.organization_id,
        _member_role=collaborator.role,
    )
    assert [row["content"] for row in await chat.list_messages(conversation_id, owner)][-1] == (
        "editor contribution"
    )

    with pytest.raises(HTTPException) as cross_tenant:
        await chat.put_conversation_member(
            conversation_id,
            foreign.id,
            chat.ConversationMemberRequest(role="viewer"),
            owner,
        )
    assert cross_tenant.value.status_code == 404

    removed = await chat.delete_conversation_member(
        conversation_id, collaborator.id, owner
    )
    assert removed["removed"] is True
    with pytest.raises(HTTPException) as revoked:
        await chat.list_messages(conversation_id, collaborator)
    assert revoked.value.status_code == 404

    notifications = await reflect_table("notifications")
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        notice_count = (
            await conn.execute(
                select(notifications.c.id).where(
                    notifications.c.organization_id == owner.organization_id,
                    notifications.c.member_id == collaborator.id,
                    notifications.c.type == "conversation_shared",
                    notifications.c.resource_id == conversation_id,
                )
            )
        ).all()
        actions = (
            await conn.execute(
                select(audit_log.c.action).where(
                    audit_log.c.organization_id == owner.organization_id,
                    audit_log.c.resource_id == conversation_id,
                    audit_log.c.action.in_(
                        ["chat.share_conversation", "chat.unshare_conversation"]
                    ),
                )
            )
        ).scalars().all()
    assert notice_count
    assert {"chat.share_conversation", "chat.unshare_conversation"} <= set(actions)


@pytest.mark.asyncio
async def test_member_directory_is_active_tenant_scoped_and_minimal_for_ordinary_members():
    owner, collaborator, next_member, foreign, _conversation_id, _task_id = await _seed()
    inactive_id = str(uuid.uuid4())
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(
            members.insert().values(
                id=inactive_id,
                organization_id=owner.organization_id,
                email=f"{inactive_id[:8]}@collab.test",
                name="Former teammate",
                role="operator",
                status="inactive",
            )
        )

    result = await settings_router.member_directory(owner)

    rows = result["members"]
    assert {row["id"] for row in rows} == {
        owner.id,
        collaborator.id,
        next_member.id,
    }
    assert inactive_id not in {row["id"] for row in rows}
    assert foreign.id not in {row["id"] for row in rows}
    assert all(set(row) == {"id", "name", "email", "role"} for row in rows)


@pytest.mark.asyncio
async def test_task_assignment_handoff_history_and_visibility():
    owner, first_assignee, next_assignee, foreign, _conversation_id, task_id = await _seed()

    assigned = await task_router.assign_task(
        task_id,
        task_router.TaskAssignmentRequest(
            member_id=first_assignee.id,
            note="Please own the client review",
        ),
        owner,
    )
    assert assigned["task"]["assignee_member_id"] == first_assignee.id
    assert assigned["assignment_event"]["event_type"] == "assigned"
    assert str((await task_router.get_task_detail(task_id, first_assignee))["id"]) == task_id
    assert task_id in {
        str(row["id"])
        for row in await task_router.list_tasks(
            status=None,
            dead_letter=None,
            include_children=False,
            limit=50,
            offset=0,
            member=first_assignee,
        )
    }
    assert await comments.member_can_access_target(
        owner.organization_id, first_assignee.id, "task", task_id
    )

    handed_off = await task_router.handoff_task(
        task_id,
        task_router.TaskAssignmentRequest(
            member_id=next_assignee.id,
            note="Coverage changes at noon",
        ),
        first_assignee,
    )
    assert handed_off["task"]["assignee_member_id"] == next_assignee.id
    assert handed_off["assignment_event"]["event_type"] == "handoff"
    with pytest.raises(HTTPException) as previous_hidden:
        await task_router.get_task_detail(task_id, first_assignee)
    assert previous_hidden.value.status_code == 404
    assert str((await task_router.get_task_detail(task_id, next_assignee))["id"]) == task_id
    # Immutable creator ownership survives every handoff.
    assert str((await task_router.get_task_detail(task_id, owner))["id"]) == task_id

    history = await task_router.get_task_assignment_history(task_id, owner)
    assert [row["event_type"] for row in history] == ["assigned", "handoff"]
    assert history[1]["from_member_id"] == first_assignee.id
    assert history[1]["to_member_id"] == next_assignee.id

    with pytest.raises(HTTPException) as cross_tenant:
        await task_router.assign_task(
            task_id,
            task_router.TaskAssignmentRequest(member_id=foreign.id),
            owner,
        )
    assert cross_tenant.value.status_code == 404

    unassigned = await task_router.unassign_task(task_id, owner)
    assert unassigned["task"]["assignee_member_id"] is None
    with pytest.raises(HTTPException) as no_longer_visible:
        await task_router.get_task_detail(task_id, next_assignee)
    assert no_longer_visible.value.status_code == 404
    history = await task_router.get_task_assignment_history(task_id, owner)
    assert [row["event_type"] for row in history] == [
        "assigned",
        "handoff",
        "unassigned",
    ]

    notifications = await reflect_table("notifications")
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        recipients = (
            await conn.execute(
                select(notifications.c.member_id).where(
                    notifications.c.organization_id == owner.organization_id,
                    notifications.c.type == "task_assignment",
                    notifications.c.resource_id == task_id,
                )
            )
        ).scalars().all()
        assignment_audits = (
            await conn.execute(
                select(audit_log.c.action).where(
                    audit_log.c.organization_id == owner.organization_id,
                    audit_log.c.resource_id == task_id,
                    audit_log.c.event_type == "task_assignment",
                )
            )
        ).scalars().all()
    assert {first_assignee.id, next_assignee.id} <= set(recipients)
    assert {"tasks.assigned", "tasks.handoff", "tasks.unassigned"} <= set(
        assignment_audits
    )
