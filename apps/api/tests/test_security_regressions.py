from __future__ import annotations

import pytest

from core.exceptions import ApprovalRequired, PermissionDenied
from core.models import Member


def _member(role: str = "user") -> Member:
    return Member(id=f"{role}-1", organization_id="default", email=f"{role}@example.com", role=role)


@pytest.fixture(autouse=True)
def _no_db_audit(monkeypatch):
    from core import permissions

    async def _noop(*_args, **_kwargs):
        return "audit-id"

    monkeypatch.setattr(permissions.audit, "log", _noop)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        "list_memory",
        "export_memory",
        "list_audit_log",
        "export_audit_log",
        "register_mcp_server",
        "discover_mcp_server",
        "create_connector_policy",
        "delete_connector_policy",
        "install_connector",
        "grant_connector_permission",
        "resolve_connector_approval",
    ],
)
async def test_sensitive_org_actions_are_not_generic_allowed(action):
    from core import permissions

    with pytest.raises(PermissionDenied):
        await permissions.check(_member("user"), action, "default")


@pytest.mark.asyncio
async def test_connector_approval_resolution_requires_approver_role():
    from core import permissions

    with pytest.raises(PermissionDenied):
        await permissions.check(_member("user"), "resolve_connector_approval", "approval-1")

    assert await permissions.check(_member("approver"), "resolve_connector_approval", "approval-1") is True


@pytest.mark.asyncio
async def test_save_message_checks_conversation_ownership_before_insert(monkeypatch):
    from fastapi import HTTPException
    from routers import chat

    executed: list[str] = []

    class _Clause:
        def __init__(self, kind: str):
            self.kind = kind

        def where(self, *_args, **_kwargs):
            return self

        def values(self, **_kwargs):
            return self

    class _FakeResult:
        def mappings(self):
            class _Mappings:
                def first(self):
                    return None

            return _Mappings()

    class _FakeConn:
        async def execute(self, stmt):
            executed.append(stmt.kind)
            return _FakeResult()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _FakeEngine:
        def begin(self):
            return _FakeConn()

    class _Col:
        def __eq__(self, _other):
            return True

    class _Table:
        class c:
            id = _Col()
            member_id = _Col()
            organization_id = _Col()

    async def _reflect(_name):
        return _Table()

    monkeypatch.setattr(chat, "engine", _FakeEngine())
    monkeypatch.setattr(chat, "reflect_table", _reflect)
    monkeypatch.setattr(chat, "select", lambda *_args, **_kwargs: _Clause("select"))
    monkeypatch.setattr(chat, "insert", lambda *_args, **_kwargs: _Clause("insert"))
    monkeypatch.setattr(chat, "update", lambda *_args, **_kwargs: _Clause("update"))

    with pytest.raises(HTTPException) as exc:
        await chat._save_message("foreign-conv", "user", "exfiltrate", _member_id="member-1", _org_id="default")

    assert exc.value.status_code == 404
    assert executed == ["select"]


@pytest.mark.asyncio
async def test_browser_navigation_rejects_internal_targets():
    from connectors.browser_operator import BrowserOperator

    operator = BrowserOperator()
    created = await operator.create_session(
        organization_id="org-1",
        member_id="member-1",
        task_id="task-1",
        consent={"purpose": "test", "allowed_domains": ["127.0.0.1"]},
    )

    with pytest.raises(ValueError, match="not allowed|unsafe|internal|private"):
        await operator.execute(
            "browser.navigate",
            {
                "session_id": created["id"],
                "url": "http://127.0.0.1:8000/admin",
                "__org_id": "org-1",
                "__task_id": "task-1",
                "__member_id": "member-1",
            },
        )


@pytest.mark.asyncio
async def test_browser_sensitive_approval_does_not_bypass_allowed_domains():
    from connectors.browser_operator import BrowserOperator

    operator = BrowserOperator()
    created = await operator.create_session(
        organization_id="org-1",
        member_id="member-1",
        task_id="task-1",
        consent={"purpose": "public browsing", "allowed_domains": ["example.test"]},
    )
    await operator.approve_sensitive_site(
        created["id"],
        organization_id="org-1",
        member_id="member-1",
        domain="bank.example",
        approval_id="approval-1",
    )

    with pytest.raises(ApprovalRequired, match="outside the consented"):
        await operator.execute(
            "browser.navigate",
            {
                "session_id": created["id"],
                "url": "https://bank.example/login",
                "__org_id": "org-1",
                "__task_id": "task-1",
                "__member_id": "member-1",
            },
        )


@pytest.mark.asyncio
async def test_browser_upload_path_is_task_workspace_jailed(tmp_path, monkeypatch):
    from connectors.browser_operator import BrowserOperator

    monkeypatch.setattr("core.workspace.WORKSPACE_ROOT", tmp_path)
    operator = BrowserOperator()
    created = await operator.create_session(
        organization_id="org-1",
        member_id="member-1",
        task_id="task-1",
        consent={"purpose": "test", "allowed_domains": ["example.test"]},
    )

    with pytest.raises(ValueError, match="escapes|absolute|workspace"):
        await operator.execute(
            "browser.upload",
            {
                "session_id": created["id"],
                "selector": "input[type=file]",
                "path": "/etc/passwd",
                "__org_id": "org-1",
                "__task_id": "task-1",
                "__member_id": "member-1",
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,module_name,code,unsafe_message",
    [
        ("code.python", "connectors.code", "from pathlib import Path\nprint(Path('/etc/passwd').read_text())", "code.python"),
        ("data.run", "connectors.data_analysis", "from pathlib import Path\nprint(Path('/etc/passwd').read_text())", "data.run"),
    ],
)
async def test_python_execution_rejects_absolute_host_file_reads(tool, module_name, code, unsafe_message):
    module = __import__(module_name, fromlist=["_validate_code", "_validate_data_code"])
    validator = getattr(module, "_validate_code", None) or getattr(module, "_validate_data_code")

    with pytest.raises(ValueError, match=unsafe_message):
        validator(code)


def test_web_does_not_accept_fragment_bearer_tokens():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "web/app/login/callback/page.tsx").read_text()

    assert "access_token=" not in source
    assert "localStorage.setItem(\"chronos_token\"" not in source


def test_chat_artifact_open_does_not_navigate_raw_active_blob():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "web/app/chat/page.tsx").read_text()

    assert "tab.location.href = url" not in source
    assert "window.open(url, \"_blank\"" not in source
