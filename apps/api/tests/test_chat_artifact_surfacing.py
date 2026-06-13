"""Proof for chat/UI wiring of document & platform artifacts.

Two surfaces:
1. Output — a tool that creates an artifact and returns it as artifact_id /
   artifact_ids in ToolResult.data is surfaced as a chat artifact card by
   _execute_tool (e.g. a filled PDF from doc.fill_pdf, an authored deck, an
   image). Verified against the real _execute_tool with a stubbed broker.
2. Input — the attachment context block exposes each file's SOURCE artifact_id
   and tells the agent it can fill/detect/summarize via the doc__* tools.

emit/broker are stubbed so no Redis or DB is needed.
"""
from __future__ import annotations

import json

import pytest

import runtime.agent_loop as al
from core.models import AgentContext, ToolResult


def _agent():
    return AgentContext(id="a", org_id="org1", task_id="t1", member_id="m1")


_TASK = {"id": "t1", "depth": 0, "organization_id": "org1"}


# ── output: artifact cards ────────────────────────────────────────────────────


@pytest.fixture
def stub(monkeypatch):
    events: list[dict] = []

    async def fake_emit(task_id, ev):
        events.append(ev)

    async def fake_get_artifact(aid):
        return {
            "organization_id": "org1", "title": f"Filled {aid}", "kind": "file",
            "mime_type": "application/pdf", "size_bytes": 1234,
        }

    monkeypatch.setattr(al, "emit_activity", fake_emit)
    monkeypatch.setattr(al, "publish_activity", fake_emit)
    # _surface_result_artifacts imports get_artifact from core.artifacts.
    import core.artifacts as ca
    monkeypatch.setattr(ca, "get_artifact", fake_get_artifact)
    return events


async def _run_tool(monkeypatch, data: dict):
    async def fake_broker_execute(agent, broker_name, args):
        return ToolResult(data=data, summary="done")

    monkeypatch.setattr(al.tool_broker, "execute", fake_broker_execute)
    call = {"id": "c1", "name": "doc__fill_pdf", "args_str": json.dumps({"artifact_id": "src"})}
    return await al._execute_tool(call, _TASK, _agent())


@pytest.mark.asyncio
async def test_single_artifact_id_surfaced(stub, monkeypatch):
    msg = await _run_tool(monkeypatch, {"status": "success", "artifact_id": "out-1"})
    cards = msg["artifacts"]
    assert len(cards) == 1
    assert cards[0]["artifact_id"] == "out-1"
    assert cards[0]["mime_type"] == "application/pdf"
    # An artifact event was emitted for the live stream.
    assert any(e.get("type") == "artifact" and e.get("artifact_id") == "out-1" for e in stub)


@pytest.mark.asyncio
async def test_multiple_artifact_ids_surfaced(stub, monkeypatch):
    msg = await _run_tool(monkeypatch, {"status": "success", "artifact_ids": ["a", "b", "a"]})
    ids = [c["artifact_id"] for c in msg["artifacts"]]
    assert ids == ["a", "b"]  # deduped, order preserved


@pytest.mark.asyncio
async def test_no_artifact_when_absent(stub, monkeypatch):
    msg = await _run_tool(monkeypatch, {"status": "success", "items_placed": 3})
    assert msg["artifacts"] == []


@pytest.mark.asyncio
async def test_cross_org_artifact_not_surfaced(stub, monkeypatch):
    async def other_org(aid):
        return {"organization_id": "intruder", "title": "x", "kind": "file"}

    import core.artifacts as ca
    monkeypatch.setattr(ca, "get_artifact", other_org)
    msg = await _run_tool(monkeypatch, {"artifact_id": "out-x"})
    assert msg["artifacts"] == []


# ── input: attachment context ─────────────────────────────────────────────────


def test_attachment_context_exposes_source_id_and_guidance():
    block = al._format_attachments_context([
        {"filename": "worksheet.pdf", "attachment_id": "src-9",
         "parsed_artifact_id": "parsed-9", "preview": "Q1: ...", "truncated": True},
    ])
    assert "source artifact_id=src-9" in block
    assert "doc__" in block  # tells the agent it can fill/detect/summarize
    assert "worksheet.pdf" in block
