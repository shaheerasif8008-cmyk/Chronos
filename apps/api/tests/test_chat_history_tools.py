import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine


def _make_member(org_id: str = "default", member_id: str = "member-1"):
    from core.models import Member

    return Member(id=member_id, organization_id=org_id, email="admin@test.com", role="owner")


def _make_agent(org_id: str = "default", member_id: str = "member-1"):
    from core.models import AgentContext

    return AgentContext(id="agent-1", org_id=org_id, member_id=member_id)


def test_chat_history_tools_are_available_to_inline_chat_and_manifest():
    from core.tool_manifest import available_tool_names
    from runtime.tool_registry import INLINE_CHAT_TOOLS, tool_name

    inline_names = {tool_name(schema) for schema in INLINE_CHAT_TOOLS}
    manifest_names = set(available_tool_names())

    assert "chat_history__search" in inline_names
    assert "chat_history__recent" in inline_names
    assert "chat_history__search" in manifest_names
    assert "chat_history__recent" in manifest_names


async def _sqlite_chat_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    meta = sa.MetaData()
    conversations = sa.Table(
        "conversations",
        meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("member_id", sa.String, nullable=False),
        sa.Column("title", sa.String),
        sa.Column("created_at", sa.String),
        sa.Column("updated_at", sa.String),
    )
    messages = sa.Table(
        "messages",
        meta,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("organization_id", sa.String, nullable=False),
        sa.Column("conversation_id", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("content", sa.String),
        sa.Column("created_at", sa.String),
    )
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        await conn.execute(
            conversations.insert(),
            [
                {
                    "id": "conv-new",
                    "organization_id": "default",
                    "member_id": "member-1",
                    "title": "summarize the last 10 chats",
                    "created_at": "2026-06-07T14:00:00Z",
                    "updated_at": "2026-06-07T14:05:00Z",
                },
                {
                    "id": "conv-old",
                    "organization_id": "default",
                    "member_id": "member-1",
                    "title": "artifact workspace scan",
                    "created_at": "2026-06-06T14:00:00Z",
                    "updated_at": "2026-06-06T14:05:00Z",
                },
                {
                    "id": "conv-other-member",
                    "organization_id": "default",
                    "member_id": "member-2",
                    "title": "private other member chat",
                    "created_at": "2026-06-07T15:00:00Z",
                    "updated_at": "2026-06-07T15:05:00Z",
                },
                {
                    "id": "conv-other-org",
                    "organization_id": "other-org",
                    "member_id": "member-1",
                    "title": "private other org chat",
                    "created_at": "2026-06-07T16:00:00Z",
                    "updated_at": "2026-06-07T16:05:00Z",
                },
            ],
        )
        await conn.execute(
            messages.insert(),
            [
                {
                    "id": "msg-new-user",
                    "organization_id": "default",
                    "conversation_id": "conv-new",
                    "role": "user",
                    "content": "summarize the last 10 chats",
                    "created_at": "2026-06-07T14:00:00Z",
                },
                {
                    "id": "msg-new-assistant",
                    "organization_id": "default",
                    "conversation_id": "conv-new",
                    "role": "assistant",
                    "content": "I cannot access previous chat transcripts yet.",
                    "created_at": "2026-06-07T14:01:00Z",
                },
                {
                    "id": "msg-old-user",
                    "organization_id": "default",
                    "conversation_id": "conv-old",
                    "role": "user",
                    "content": "Finish the binary artifact scan behind your workspace.",
                    "created_at": "2026-06-06T14:00:00Z",
                },
                {
                    "id": "msg-other-member",
                    "organization_id": "default",
                    "conversation_id": "conv-other-member",
                    "role": "user",
                    "content": "secret matching artifact text",
                    "created_at": "2026-06-07T15:00:00Z",
                },
                {
                    "id": "msg-other-org",
                    "organization_id": "other-org",
                    "conversation_id": "conv-other-org",
                    "role": "user",
                    "content": "secret matching artifact text",
                    "created_at": "2026-06-07T16:00:00Z",
                },
            ],
        )
    return engine, {"conversations": conversations, "messages": messages}


@pytest.mark.asyncio
async def test_chat_history_recent_returns_member_scoped_transcripts(monkeypatch):
    from connectors import chat_history

    sqlite_engine, tables = await _sqlite_chat_db()

    async def fake_reflect_table(name: str):
        return tables[name]

    monkeypatch.setattr(chat_history, "engine", sqlite_engine)
    monkeypatch.setattr(chat_history, "reflect_table", fake_reflect_table)

    result = await chat_history.chat_history_connector.execute(
        "chat_history.recent",
        {"limit": 10},
        _make_agent(),
    )

    ids = [c["id"] for c in result.data["conversations"]]
    assert ids == ["conv-new", "conv-old"]
    assert "private other member" not in str(result.data)
    assert "private other org" not in str(result.data)
    assert result.data["conversations"][0]["url"] == "/chat?c=conv-new"

    await sqlite_engine.dispose()


@pytest.mark.asyncio
async def test_chat_history_search_matches_titles_and_messages_without_leaks(monkeypatch):
    from connectors import chat_history

    sqlite_engine, tables = await _sqlite_chat_db()

    async def fake_reflect_table(name: str):
        return tables[name]

    monkeypatch.setattr(chat_history, "engine", sqlite_engine)
    monkeypatch.setattr(chat_history, "reflect_table", fake_reflect_table)

    result = await chat_history.chat_history_connector.execute(
        "chat_history.search",
        {"query": "artifact", "limit": 10},
        _make_agent(),
    )

    ids = {c["id"] for c in result.data["conversations"]}
    assert ids == {"conv-old"}
    assert "secret matching artifact text" not in str(result.data)

    await sqlite_engine.dispose()


@pytest.mark.asyncio
async def test_tool_broker_routes_chat_history_through_permission_and_audit(monkeypatch):
    from core import tool_broker
    from core.models import ToolResult

    calls: list[tuple[str, str]] = []

    async def fake_permissions_check(actor, action, resource):
        calls.append(("permission", action))
        return True

    async def fake_audit_log(event_type, actor_id, action, **kwargs):
        calls.append(("audit", event_type))
        return "audit-1"

    async def fake_rate_limit(org_id):
        return None

    async def fake_token_budget(org_id):
        return None

    async def fake_loop(org_id, tool, args_hash):
        return None

    async def fake_tool_policy(org_id, provider):
        return {}

    async def fake_connector_tier(provider):
        return "live"

    async def fake_route(agent, tool, args, vault_ref, tier="live"):
        assert tool == "chat_history.recent"
        assert vault_ref == "live"
        return ToolResult(summary="1 prior chat", data={"conversations": []})

    monkeypatch.setattr(tool_broker.permissions, "check", fake_permissions_check)
    monkeypatch.setattr(tool_broker.audit, "log", fake_audit_log)
    monkeypatch.setattr(tool_broker, "_check_rate_limit", fake_rate_limit)
    monkeypatch.setattr(tool_broker, "_check_token_budget", fake_token_budget)
    monkeypatch.setattr(tool_broker, "_check_loop", fake_loop)
    monkeypatch.setattr(tool_broker, "tool_policy", fake_tool_policy)
    monkeypatch.setattr(tool_broker, "connector_tier", fake_connector_tier)
    monkeypatch.setattr(tool_broker, "_route", fake_route)

    result = await tool_broker.tool_broker.execute(_make_agent(), "chat_history.recent", {"limit": 10})

    assert result.summary == "1 prior chat"
    assert ("permission", "use_tool:chat_history.recent") in calls
    assert ("audit", "tool_call") in calls
    assert ("audit", "tool_result") in calls


@pytest.mark.asyncio
async def test_list_messages_rejects_foreign_conversation(monkeypatch):
    from routers import chat

    sqlite_engine, tables = await _sqlite_chat_db()

    async def fake_reflect_table(name: str):
        return tables[name]

    async def fake_permission(*args, **kwargs):
        return True

    monkeypatch.setattr(chat, "reflect_table", fake_reflect_table)
    monkeypatch.setattr(chat, "engine", sqlite_engine)
    monkeypatch.setattr(chat.permissions, "check", fake_permission)

    with pytest.raises(HTTPException) as exc:
        await chat.list_messages("conv-other-member", _make_member())

    assert exc.value.status_code == 404
    await sqlite_engine.dispose()
