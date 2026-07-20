"""Member-level privacy proofs for tasks, activity, and approval feeds."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from core.db import engine, reflect_table
from core.models import Member
from routers.activity import get_activity_actions
from routers.approvals import list_approvals
from routers.tasks import get_task_detail, list_tasks


async def _seed() -> tuple[Member, Member, str, str]:
    org_id = str(uuid.uuid4())
    first_id = str(uuid.uuid4())
    second_id = str(uuid.uuid4())
    first_task = str(uuid.uuid4())
    second_task = str(uuid.uuid4())
    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    tasks = await reflect_table("tasks")
    approvals = await reflect_table("approvals")
    audit_log = await reflect_table("audit_log")
    async with engine.begin() as conn:
        await conn.execute(
            organizations.insert().values(
                id=org_id, slug=f"task-{org_id[:8]}", name="Task Privacy Org"
            )
        )
        await conn.execute(
            members.insert(),
            [
                {
                    "id": first_id,
                    "organization_id": org_id,
                    "email": f"{first_id[:8]}@privacy.test",
                    "role": "operator",
                },
                {
                    "id": second_id,
                    "organization_id": org_id,
                    "email": f"{second_id[:8]}@privacy.test",
                    "role": "operator",
                },
            ],
        )
        for task_id, member_id, goal in (
            (first_task, first_id, "first member private goal"),
            (second_task, second_id, "second member private goal"),
        ):
            await conn.execute(
                tasks.insert().values(
                    id=task_id,
                    organization_id=org_id,
                    triggered_by="manual",
                    triggered_by_member_id=member_id,
                    status="queued",
                    goal=goal,
                    plan={},
                    result={},
                )
            )
            await conn.execute(
                approvals.insert().values(
                    organization_id=org_id,
                    task_id=task_id,
                    step_id="step-1",
                    action_type="gmail.send",
                    action_payload={"to": f"{member_id[:8]}@client.test"},
                    status="pending",
                )
            )
            await conn.execute(
                audit_log.insert().values(
                    organization_id=org_id,
                    event_type="activity",
                    actor_id="chronos",
                    action="tool_call",
                    resource_type="task",
                    resource_id=task_id,
                    payload={"type": "tool_call", "task_id": task_id, "tool": "gmail.search"},
                )
            )
    return (
        Member(id=first_id, organization_id=org_id, email=f"{first_id[:8]}@privacy.test", role="operator"),
        Member(id=second_id, organization_id=org_id, email=f"{second_id[:8]}@privacy.test", role="operator"),
        first_task,
        second_task,
    )


@pytest.mark.asyncio
async def test_task_activity_and_approval_feeds_do_not_leak_org_peer_data():
    first, second, first_task, second_task = await _seed()

    first_tasks = await list_tasks(
        status=None, dead_letter=None, include_children=False, limit=50, offset=0,
        member=first,
    )
    assert {str(row["id"]) for row in first_tasks} == {first_task}
    assert str((await get_task_detail(first_task, member=first))["id"]) == first_task
    with pytest.raises(HTTPException) as hidden:
        await get_task_detail(second_task, member=first)
    assert hidden.value.status_code == 404

    activity = await get_activity_actions(
        type=None, status=None, task_id=None, tool=None, query=None,
        limit=100, offset=0, member=first,
    )
    assert {row["task_id"] for row in activity} == {first_task}

    approvals = await list_approvals(status="pending", limit=100, offset=0, member=first)
    assert {str(row["task_id"]) for row in approvals} == {first_task}

    # An explicit organization approver is the intentional shared-queue role.
    approver = Member(
        id=str(uuid.uuid4()),
        organization_id=first.organization_id,
        email="approver@privacy.test",
        role="approver",
    )
    approver_rows = await list_approvals(status="pending", limit=100, offset=0, member=approver)
    assert {str(row["task_id"]) for row in approver_rows} == {first_task, second_task}


@pytest.mark.asyncio
async def test_comment_targets_reuse_private_task_and_artifact_visibility():
    """Knowing a same-org private target id must not expose its comment thread."""

    from core import comments
    from core.artifacts import save_artifact

    first, second, first_task, second_task = await _seed()
    artifact_id = await save_artifact(
        "first member private artifact",
        kind="markdown",
        title="Private",
        org_id=first.organization_id,
        created_by=f"member:{first.id}",
    )

    assert await comments.member_can_access_target(
        first.organization_id, first.id, "task", first_task
    )
    assert not await comments.member_can_access_target(
        first.organization_id, second.id, "task", first_task
    )
    assert not await comments.member_can_access_target(
        first.organization_id, first.id, "task", second_task
    )
    assert await comments.member_can_access_target(
        first.organization_id, first.id, "artifact", artifact_id
    )
    assert not await comments.member_can_access_target(
        first.organization_id, second.id, "artifact", artifact_id
    )
