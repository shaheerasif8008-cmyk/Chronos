"""RULE 7 regression guard: the ToolBroker must never write raw tool args into
audit payloads (they may contain credentials). It logs an args_hash instead.

We run a non-gated tool whose args carry a distinctive secret and capture every
audit.log call; the secret must appear in none of them.
"""
from __future__ import annotations

import pytest

from core import tool_broker
from core.models import AgentContext

SECRET = "SEKRET-TOKEN-XYZ-9876"


@pytest.mark.asyncio
async def test_broker_never_logs_raw_args_to_audit(monkeypatch):
    captured: list[dict] = []

    async def fake_audit_log(event_type, actor_id, action, **kwargs):
        captured.append({"event_type": event_type, "action": action, **kwargs})

    # Capture audit at the broker's import site.
    monkeypatch.setattr(tool_broker.audit, "log", fake_audit_log)

    agent = AgentContext(id="task:redaction", org_id="default", member_id="m1")
    # fs.read is non-gated, so it passes the approval gate and reaches the
    # tool_call audit (step 6). Execution may fail on a bogus path — irrelevant;
    # the audit payload is what we assert on.
    try:
        await tool_broker.execute(agent, "fs.read", {"path": "/nonexistent", "api_key": SECRET})
    except Exception:
        pass

    assert captured, "expected at least one audit.log call (tool_call)"
    blob = repr(captured)
    assert SECRET not in blob, f"RULE 7 violation: raw secret leaked into audit payload: {blob}"
