import pytest


@pytest.fixture(autouse=True)
def _disable_live_browserbase(monkeypatch):
    """Unit tests must never consume configured production browser sessions."""
    monkeypatch.setattr(
        "connectors.browser_operator.settings.browserbase_operator_enabled", False
    )


def test_manual_browser_session_requires_bounded_explicit_consent():
    from datetime import datetime, timedelta, timezone

    from pydantic import ValidationError

    from routers.browser_sessions import CreateBrowserSessionRequest

    with pytest.raises(ValidationError):
        CreateBrowserSessionRequest(consent={"purpose": "browse", "allowed_domains": []})

    request = CreateBrowserSessionRequest(
        consent={
            "purpose": "Review the client portal",
            "allowed_domains": ["portal.example.com", "portal.example.com"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
            "confirmed_by_user": True,
        }
    )
    assert request.consent["allowed_domains"] == ["portal.example.com"]


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
async def test_browser_login_task_opens_site_and_requests_takeover(monkeypatch):
    from connectors.browser_operator import BrowserOperator

    monkeypatch.setattr("connectors.browser_operator.assert_safe_url", lambda _url: _url)

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

    monkeypatch.setattr("connectors.browser_operator.assert_safe_url", lambda _url: _url)
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
async def test_browser_sensitive_navigation_requires_session_consent_and_approval(monkeypatch):
    from core.exceptions import ApprovalRequired
    from connectors.browser_operator import BrowserOperator

    monkeypatch.setattr("connectors.browser_operator.assert_safe_url", lambda _url: _url)

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

    with pytest.raises(ApprovalRequired, match="outside the consented"):
        await operator.execute(
            "browser.navigate",
            {
                "session_id": created["id"],
                "url": "https://bank.example/login",
                "__org_id": "org-1",
                "__task_id": "task-1",
            },
        )

    same_sensitive_domain = await operator.create_session(
        organization_id="org-1",
        member_id="member-1",
        task_id="task-2",
        consent={"purpose": "bank login", "allowed_domains": ["bank.example"]},
    )
    approved = await operator.approve_sensitive_site(
        same_sensitive_domain["id"],
        organization_id="org-1",
        member_id="member-1",
        domain="bank.example",
        approval_id="approval-1",
    )
    assert approved["sensitive_site_approvals"][0]["domain"] == "bank.example"

    result = await operator.execute(
        "browser.navigate",
        {
            "session_id": same_sensitive_domain["id"],
            "url": "https://bank.example/login",
            "__org_id": "org-1",
            "__task_id": "task-1",
        },
    )
    assert result.data["session"]["current_url"] == "https://bank.example/login"


@pytest.mark.asyncio
async def test_browser_session_rejects_actions_after_consent_expiry():
    from datetime import datetime, timedelta, timezone

    from connectors.browser_operator import BrowserOperator
    from core.exceptions import ApprovalRequired

    operator = BrowserOperator()
    created = await operator.create_session(
        organization_id="org-expired",
        member_id="member-1",
        consent={
            "purpose": "time bounded browsing",
            "allowed_domains": ["example.test"],
            "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        },
    )

    with pytest.raises(ApprovalRequired, match="consent has expired"):
        await operator.execute(
            "browser.get_state",
            {"session_id": created["id"], "__org_id": "org-expired"},
        )


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


@pytest.mark.asyncio
async def test_browserbase_context_session_payload_is_persistent_and_tenant_pseudonymous(monkeypatch):
    from connectors.browser_operator import _BrowserbaseClient

    client = _BrowserbaseClient(
        api_key="secret-key",
        project_id="project-1",
        region="us-east-1",
    )
    calls = []

    async def fake_request(method, path, *, operation, json=None):
        calls.append((method, path, operation, json))
        if path == "/contexts":
            return {"id": "context-1"}
        return {"id": "remote-1", "connectUrl": "wss://connect.browserbase.com/test"}

    monkeypatch.setattr(client, "_request", fake_request)

    context_id = await client.create_context()
    await client.create_session(
        context_id=context_id,
        chronos_session_id="chronos-1",
        organization_id="client-acme",
        timeout_seconds=3600,
    )

    assert calls[0][3] == {"projectId": "project-1"}
    session_payload = calls[1][3]
    assert session_payload["keepAlive"] is True
    assert session_payload["browserSettings"]["context"] == {
        "id": "context-1",
        "persist": True,
    }
    assert session_payload["region"] == "us-east-1"
    assert session_payload["userMetadata"]["chronosSessionId"] == "chronos-1"
    assert session_payload["userMetadata"]["tenantHash"] != "client-acme"
    assert "client-acme" not in str(session_payload)


@pytest.mark.asyncio
async def test_browserbase_runtime_rehydrates_remote_session_after_process_restart(monkeypatch):
    from connectors.browser_operator import BrowserOperator

    monkeypatch.setattr(
        "connectors.browser_operator.settings.browserbase_api_key",
        "bb-test",
    )
    monkeypatch.setattr(
        "connectors.browser_operator.settings.browserbase_project_id",
        "project-test",
    )

    class FakePage:
        def set_default_timeout(self, timeout):
            self.timeout = timeout

    page = FakePage()

    class FakeContext:
        pages = [page]

    class FakeBrowser:
        contexts = [FakeContext()]

    class FakeChromium:
        def __init__(self):
            self.urls = []

        async def connect_over_cdp(self, url, timeout):
            self.urls.append((url, timeout))
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        async def stop(self):
            return None

    runtimes = []

    class FakeFactory:
        async def start(self):
            runtime = FakePlaywright()
            runtimes.append(runtime)
            return runtime

    class FakeBrowserbase:
        def __init__(self):
            self.gets = []
            self.created_context = False
            self.created_session = False

        async def get_session(self, session_id):
            self.gets.append(session_id)
            return {
                "id": session_id,
                "status": "RUNNING",
                "connectUrl": "wss://connect.browserbase.com/signed-secret",
            }

        async def create_context(self):
            self.created_context = True
            return "unexpected"

        async def create_session(self, **_kwargs):
            self.created_session = True
            return {"id": "unexpected"}

    remote = FakeBrowserbase()
    session = {
        "id": "chronos-session-1",
        "organization_id": "org-1",
        "runtime_provider": "browserbase",
        "remote_context_id": "context-1",
        "remote_session_id": "remote-1",
        "status": "active",
        "consent": {},
        "storage_state": {"cookies": [{"name": "must-never-load"}]},
    }

    async def noop(*_args, **_kwargs):
        return None

    for _ in range(2):
        operator = BrowserOperator()
        monkeypatch.setattr(operator, "_browserbase_client", lambda: remote)
        monkeypatch.setattr(operator, "_playwright_api_or_none", lambda _session: noop())
        # Return a fresh async_playwright-like factory without importing or
        # launching a process-local Chromium binary.
        async def fake_api(_session):
            return lambda: FakeFactory()

        monkeypatch.setattr(operator, "_playwright_api_or_none", fake_api)
        monkeypatch.setattr(operator, "_save_session", noop)
        monkeypatch.setattr(operator, "_record_event", noop)
        assert await operator._runtime_page(dict(session)) is page

    assert remote.gets == ["remote-1", "remote-1"]
    assert remote.created_context is False
    assert remote.created_session is False
    assert all(runtime.chromium.urls for runtime in runtimes)


@pytest.mark.asyncio
async def test_browser_capture_and_download_are_durable_without_exposing_local_paths(
    monkeypatch,
    tmp_path,
):
    from connectors.browser_operator import BrowserOperator, _RuntimeHandle, _public_session
    from core.file_security import FileScanResult

    download_path = tmp_path / "client-report.pdf"
    download_path.write_bytes(b"pdf-bytes")

    class FakeDownload:
        suggested_filename = "client-report.pdf"

        async def path(self):
            return str(download_path)

    class FakeDownloadInfo:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @property
        async def value(self):
            return FakeDownload()

    class FakePage:
        async def title(self):
            return "Client portal"

        async def screenshot(self, full_page=False):
            return b"png-bytes"

        def expect_download(self):
            return FakeDownloadInfo()

        async def click(self, _selector):
            return None

    operator = BrowserOperator()
    session = {
        "id": "session-1",
        "organization_id": "org-1",
        "status": "active",
        "downloads": [],
        "history": [],
    }
    page = FakePage()
    operator._pages["session-1"] = _RuntimeHandle(None, None, None, page, "browserbase")
    uploads = []

    async def fake_put(key, body, content_type):
        uploads.append((key, body, content_type))

    async def noop(*_args, **_kwargs):
        return None

    async def clean_scan(body):
        return FileScanResult(
            verdict="clean",
            sha256="test-clean-sha256",
            size_bytes=len(body),
            engine_version="ClamAV test fixture",
        )

    monkeypatch.setattr("connectors.browser_operator.put_object", fake_put)
    monkeypatch.setattr("connectors.browser_operator.scan_file_bytes", clean_scan)
    monkeypatch.setattr(
        "connectors.browser_operator.record_file_security_event_if_available",
        noop,
    )
    monkeypatch.setattr(operator, "_save_session", noop)
    monkeypatch.setattr(operator, "_record_action", noop)

    await operator._capture_state(session, "navigate")
    result = await operator._download(session, {"selector": "a.download"})

    assert uploads[0][0].startswith("browser-sessions/")
    assert uploads[0][1:] == (b"png-bytes", "image/png")
    assert uploads[1][1:] == (b"pdf-bytes", "application/pdf")
    assert session["screenshot_object_path"] == uploads[0][0]
    assert "path" not in result.data["download"]
    assert "object_path" not in result.data["download"]
    assert result.data["download"]["download_url"].endswith("/downloads/0")
    public = _public_session({**session, "storage_state": {"cookies": ["secret"]}})
    assert "storage_state" not in public
    assert public["screenshot_url"].endswith("/screenshot")


@pytest.mark.asyncio
async def test_production_browser_operator_refuses_local_runtime(monkeypatch):
    from connectors.browser_operator import BrowserOperator

    monkeypatch.setattr("connectors.browser_operator.settings.environment", "production")
    monkeypatch.setattr(
        "connectors.browser_operator.settings.browserbase_operator_enabled",
        False,
    )
    operator = BrowserOperator()
    with pytest.raises(RuntimeError, match="forbidden|requires Browserbase"):
        await operator._runtime_page(
            {
                "id": "session-1",
                "organization_id": "org-1",
                "runtime_provider": "local",
            }
        )


def test_browser_remote_state_migration_purges_legacy_plaintext_credentials():
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/0057_browser_operator_remote_state.py"
    ).read_text()

    assert 'down_revision = "0056_notification_delivery"' in migration
    assert "storage_state = '{}'::jsonb" in migration
    assert "cookies_ref = NULL" in migration
    assert 'sa.Column("remote_context_id"' in migration
    assert 'sa.Column("remote_session_id"' in migration
