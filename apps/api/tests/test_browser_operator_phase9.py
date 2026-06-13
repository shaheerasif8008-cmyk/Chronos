import pytest


def test_browser_operator_tools_are_registered_for_agent_and_inline_use():
    from runtime.tool_registry import ALL_TOOLS, INLINE_CHAT_TOOLS, tool_name

    all_names = {tool_name(tool) for tool in ALL_TOOLS}
    inline_names = {tool_name(tool) for tool in INLINE_CHAT_TOOLS}

    expected = {
        "browser__navigate",
        "browser__login_task",
        "browser__click",
        "browser__type",
        "browser__select",
        "browser__scroll",
        "browser__wait",
        "browser__extract",
        "browser__screenshot",
        "browser__download",
        "browser__upload",
        "browser__read_dom",
        "browser__get_state",
        "browser__close",
        "browser__request_takeover",
    }

    assert expected <= all_names
    assert expected <= inline_names


@pytest.mark.asyncio
async def test_browser_login_task_opens_site_and_requests_takeover():
    from connectors.browser_operator import BrowserOperator

    operator = BrowserOperator()
    result = await operator.execute(
        "browser.login_task",
        {
            "login_url": "https://app.example.test/login",
            "task": "Download the latest invoice",
            "__org_id": "org-1",
            "__task_id": "task-1",
            "__member_id": "member-1",
        },
    )

    session = result.data["session"]
    assert session["current_url"] == "https://app.example.test/login"
    assert session["takeover_state"] == "requested"
    assert "credentials" in session["takeover_reason"].lower()
    assert session["consent"]["allowed_domains"] == ["app.example.test"]
    assert "storage_state" not in session
    assert result.data["next_step"] == "user_takeover_required"
    assert "browser.get_state" in result.data["resume_instructions"]


@pytest.mark.asyncio
async def test_browser_session_manager_persists_state_takeover_and_revocation(monkeypatch):
    from connectors.browser_operator import BrowserOperator

    events = []

    async def fake_log(event_type, actor_id, action, **kwargs):
        events.append((event_type, actor_id, action, kwargs))
        return f"audit-{len(events)}"

    monkeypatch.setattr("connectors.browser_operator.audit.log", fake_log)

    operator = BrowserOperator()
    created = await operator.create_session(
        organization_id="org-1",
        member_id="member-1",
        task_id="task-1",
        consent={"purpose": "test login", "allowed_domains": ["example.test"]},
    )

    navigated = await operator.execute(
        "browser.navigate",
        {
            "session_id": created["id"],
            "url": "https://example.test/form",
            "__org_id": "org-1",
            "__task_id": "task-1",
        },
    )
    assert navigated.data["session"]["current_url"] == "https://example.test/form"
    assert navigated.data["session"]["status"] == "active"

    takeover = await operator.execute(
        "browser.request_takeover",
        {
            "session_id": created["id"],
            "reason": "MFA required",
            "__org_id": "org-1",
            "__task_id": "task-1",
        },
    )
    assert takeover.data["session"]["takeover_state"] == "requested"

    handed_back = await operator.hand_back(
        created["id"],
        organization_id="org-1",
        member_id="member-1",
        summary="MFA complete",
    )
    assert handed_back["takeover_state"] == "released"
    assert handed_back["takeover_summary"] == "MFA complete"

    revoked = await operator.revoke_session(
        created["id"],
        organization_id="org-1",
        member_id="member-1",
        reason="done",
    )
    assert revoked["status"] == "revoked"
    assert revoked["revoked_at"] is not None
    assert any(event[2] == "browser_takeover_requested" for event in events)
    assert any(event[2] == "browser_session_revoked" for event in events)


@pytest.mark.asyncio
async def test_browser_sensitive_navigation_requires_session_consent_and_approval():
    from core.exceptions import ApprovalRequired
    from connectors.browser_operator import BrowserOperator

    operator = BrowserOperator()
    created = await operator.create_session(
        organization_id="org-1",
        member_id="member-1",
        task_id="task-1",
        consent={"purpose": "public browsing", "allowed_domains": ["example.test"]},
    )

    with pytest.raises(ApprovalRequired):
        await operator.execute(
            "browser.navigate",
            {
                "session_id": created["id"],
                "url": "https://bank.example/login",
                "__org_id": "org-1",
                "__task_id": "task-1",
            },
        )

    approved = await operator.approve_sensitive_site(
        created["id"],
        organization_id="org-1",
        member_id="member-1",
        domain="bank.example",
        approval_id="approval-1",
    )
    assert approved["sensitive_site_approvals"][0]["domain"] == "bank.example"

    result = await operator.execute(
        "browser.navigate",
        {
            "session_id": created["id"],
            "url": "https://bank.example/login",
            "__org_id": "org-1",
            "__task_id": "task-1",
        },
    )
    assert result.data["session"]["current_url"] == "https://bank.example/login"


def test_browser_activity_event_is_normalized_as_action():
    from datetime import datetime, timezone

    from core.activity_events import normalize_audit_event

    event = normalize_audit_event(
        {
            "id": "audit-browser-1",
            "event_type": "activity",
            "action": "browser_action",
            "actor_id": "chronos",
            "resource_id": "task-1",
            "payload": {
                "type": "browser_action",
                "task_id": "task-1",
                "session_id": "session-1",
                "action": "navigate",
                "current_url": "https://example.test/form",
            },
            "created_at": datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        },
        tasks_by_id={"task-1": {"id": "task-1", "goal": "Fill a form", "status": "running"}},
        approvals_by_id={},
        artifacts_by_id={},
    )

    assert event["type"] == "browser_action"
    assert event["status"] == "running"
    assert event["summary"] == "Browser navigate: https://example.test/form"
    assert event["payload"]["session_id"] == "session-1"
