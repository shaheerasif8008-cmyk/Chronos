"""Proof for native platform-tool injection in the agent loop.

Validates the two integration points added to runtime.agent_loop._execute_tool:

1. A call to an injected platform-action tool (name from platform.actions) is
   transparently rewritten into a governed ``platform.invoke`` broker call.
2. A ``platform.actions`` result surfaces its native tool schemas/routes
   out-of-band (``_pf_tools`` / ``_pf_routes``) so run_loop can inject them,
   and those keys never leak into the LLM-visible tool message content.

emit/publish/broker are stubbed so no Redis or DB is needed.
"""
from __future__ import annotations

import json

import pytest

import runtime.agent_loop as al
from core.models import AgentContext, ToolResult


@pytest.fixture
def stub_loop(monkeypatch):
    captured: dict = {}

    async def fake_emit(*a, **k):
        return None

    async def fake_broker_execute(agent, broker_name, args):
        captured["broker_name"] = broker_name
        captured["args"] = args
        if broker_name == "platform.invoke":
            return ToolResult(data={"status": "success", "result": {"ok": True}}, summary="invoked")
        if broker_name == "platform.actions":
            return ToolResult(
                data={
                    "tool_schemas": [{"type": "function", "function": {"name": "pcall_abc", "parameters": {}}}],
                    "tool_routes": [{"name": "pcall_abc", "platform_id": "mcp:s1", "action": "create_file", "kind": "mcp"}],
                },
                summary="2 actions",
            )
        return ToolResult(data={}, summary="x")

    monkeypatch.setattr(al, "emit_activity", fake_emit)
    monkeypatch.setattr(al, "publish_activity", fake_emit)
    monkeypatch.setattr(al.tool_broker, "execute", fake_broker_execute)
    return captured


def _agent():
    return AgentContext(id="a", org_id="org1", task_id="t1", member_id="m1")


_TASK = {"id": "t1", "depth": 0, "organization_id": "org1"}


@pytest.mark.asyncio
async def test_injected_tool_rewritten_to_platform_invoke(stub_loop):
    routes = {"pcall_abc": {"name": "pcall_abc", "platform_id": "mcp:s1",
                            "action": "create_file", "kind": "mcp"}}
    call = {"id": "c1", "name": "pcall_abc", "args_str": json.dumps({"title": "Lab"})}

    msg = await al._execute_tool(call, _TASK, _agent(), routes)

    assert stub_loop["broker_name"] == "platform.invoke"
    assert stub_loop["args"]["platform_id"] == "mcp:s1"
    assert stub_loop["args"]["action"] == "create_file"
    assert stub_loop["args"]["action_args"] == {"title": "Lab"}
    assert msg["name"] == "platform__invoke"


@pytest.mark.asyncio
async def test_platform_actions_surfaces_schemas_out_of_band(stub_loop):
    call = {"id": "c2", "name": "platform__actions", "args_str": json.dumps({"platform_id": "mcp:s1"})}

    msg = await al._execute_tool(call, _TASK, _agent(), {})

    assert msg["_pf_tools"] and msg["_pf_routes"]
    assert msg["_pf_routes"][0]["name"] == "pcall_abc"
    # The out-of-band keys must never enter the LLM-visible content.
    assert "_pf_tools" not in msg["content"]
    assert "_pf_routes" not in msg["content"]


@pytest.mark.asyncio
async def test_non_injected_tool_is_untouched(stub_loop):
    call = {"id": "c3", "name": "platform__list", "args_str": "{}"}
    await al._execute_tool(call, _TASK, _agent(), {"pcall_abc": {"platform_id": "mcp:s1"}})
    # platform.list is not in the route map → passes through unrewritten.
    assert stub_loop["broker_name"] == "platform.list"
