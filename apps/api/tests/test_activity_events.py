from datetime import datetime, timezone


def test_activity_event_normalizes_tool_approval_artifact_and_task_links():
    from core.activity_events import normalize_audit_event

    created_at = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    event = normalize_audit_event(
        {
            "id": "audit-1",
            "event_type": "activity",
            "action": "tool_call",
            "actor_id": "chronos",
            "resource_id": "task-1",
            "payload": {
                "type": "tool_call",
                "task_id": "task-1",
                "tool": "fs__write",
                "args_preview": {"path": "brief.md", "content": "[omitted]"},
            },
            "created_at": created_at,
        },
        tasks_by_id={"task-1": {"id": "task-1", "goal": "Write a brief", "status": "running"}},
        approvals_by_id={},
        artifacts_by_id={},
    )

    assert event["id"] == "audit-1"
    assert event["type"] == "tool_call"
    assert event["status"] == "running"
    assert event["tool"] == "fs.write"
    assert event["summary"] == "Calling fs.write"
    assert event["task_id"] == "task-1"
    assert event["task_goal"] == "Write a brief"
    assert event["task_status"] == "running"
    assert event["created_at"] == created_at.isoformat()


def test_activity_event_links_approval_and_artifact_records():
    from core.activity_events import normalize_audit_event

    approval_event = normalize_audit_event(
        {
            "id": "audit-2",
            "event_type": "activity",
            "action": "awaiting_approval",
            "actor_id": "chronos",
            "resource_id": "task-1",
            "payload": {"type": "awaiting_approval", "task_id": "task-1", "approval_ids": ["approval-1"]},
            "created_at": datetime(2026, 5, 24, 12, 1, tzinfo=timezone.utc),
        },
        tasks_by_id={"task-1": {"id": "task-1", "goal": "Send email", "status": "awaiting_approval"}},
        approvals_by_id={"approval-1": {"id": "approval-1", "status": "pending", "action_type": "gmail.send"}},
        artifacts_by_id={},
    )
    artifact_event = normalize_audit_event(
        {
            "id": "audit-3",
            "event_type": "activity",
            "action": "artifact",
            "actor_id": "chronos",
            "resource_id": "task-1",
            "payload": {
                "type": "artifact",
                "task_id": "task-1",
                "artifact_id": "artifact-1",
                "title": "brief.md",
                "kind": "markdown",
            },
            "created_at": datetime(2026, 5, 24, 12, 2, tzinfo=timezone.utc),
        },
        tasks_by_id={"task-1": {"id": "task-1", "goal": "Write brief", "status": "complete"}},
        approvals_by_id={"approval-1": {"id": "approval-1", "status": "pending", "action_type": "gmail.send"}},
        artifacts_by_id={"artifact-1": {"id": "artifact-1", "title": "brief.md", "kind": "markdown"}},
    )

    assert approval_event["status"] == "approval_pending"
    assert approval_event["approval_id"] == "approval-1"
    assert approval_event["approval_status"] == "pending"
    assert approval_event["summary"] == "Waiting for approval"
    assert artifact_event["status"] == "complete"
    assert artifact_event["artifact_id"] == "artifact-1"
    assert artifact_event["artifact_title"] == "brief.md"
    assert artifact_event["artifact_kind"] == "markdown"
