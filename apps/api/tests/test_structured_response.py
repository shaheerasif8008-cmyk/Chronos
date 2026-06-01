import pytest
from sqlalchemy import insert, select
from core.config import settings


@pytest.mark.asyncio
async def test_messages_table_has_structured_response_column():
    from core.db import engine, reflect_table

    messages = await reflect_table("messages")
    assert "structured_response" in messages.c, (
        "structured_response column missing — run alembic upgrade head"
    )

    conversations = await reflect_table("conversations")
    async with engine.begin() as conn:
        conv_id = (
            await conn.execute(
                insert(conversations)
                .values(organization_id=settings.org_id, region=settings.region,
                        member_id="member-1", title="t")
                .returning(conversations.c.id)
            )
        ).scalar_one()
        envelope = {"response_type": "direct_answer", "status": "complete", "summary": "hi"}
        await conn.execute(
            insert(messages).values(
                organization_id=settings.org_id, region=settings.region,
                conversation_id=conv_id, role="assistant", content="hi",
                structured_response=envelope,
            )
        )
        row = (
            await conn.execute(
                select(messages.c.structured_response).where(messages.c.conversation_id == conv_id)
            )
        ).scalar_one()
    assert row == envelope


def test_envelope_serializes_with_defaults():
    from core.structured_response import StructuredResponse

    env = StructuredResponse(response_type="direct_answer", status="complete", summary="Paris.")
    dumped = env.model_dump()
    assert dumped["response_type"] == "direct_answer"
    assert dumped["status"] == "complete"
    assert dumped["key_findings"] == []
    assert dumped["artifacts"] == []
    assert dumped["actions"] == []
    assert dumped["approval_status"] is None


def test_envelope_rejects_unknown_status():
    import pytest as _pytest
    from core.structured_response import StructuredResponse

    with _pytest.raises(ValueError):
        StructuredResponse(response_type="task_complete", status="banana", summary="x")


def test_build_runtime_facts_extracts_artifacts_and_status():
    from core.structured_response import build_runtime_facts

    facts = build_runtime_facts(
        result={"answer": "done", "artifacts": [
            {"id": "a1", "title": "Risk Memo", "kind": "memo"}]},
        task_status="complete",
        tool_summaries=["browser__search done", "gmail.draft created draft"],
        approval_exists=True,
    )
    assert facts.status == "complete"
    assert [a.id for a in facts.artifacts] == ["a1"]
    # gmail.draft → "drafted" verb; never "sent" without a send tool-result
    assert any(a.verb == "drafted" for a in facts.actions)
    assert all(a.verb != "sent" for a in facts.actions)
    assert facts.approval_status == "drafted_not_sent"


def test_derive_response_type_distinguishes_answer_from_work():
    from core.structured_response import build_runtime_facts, derive_response_type

    # No tools, no artifacts, no approval → plain answer even via the loop.
    plain = build_runtime_facts(result={"answer": "Paris."}, task_status="complete",
                                tool_summaries=[], approval_exists=False)
    assert derive_response_type(plain) == "direct_answer"

    # Used a tool → real work.
    worked = build_runtime_facts(result={"answer": "done"}, task_status="complete",
                                 tool_summaries=["browser__search: done"], approval_exists=False)
    assert derive_response_type(worked) == "task_complete"


def test_truth_guard_blocks_unverified_send():
    from core.structured_response import build_runtime_facts, apply_truth_guard, StructuredResponse

    facts = build_runtime_facts(
        result={"answer": "I sent the email."},
        task_status="complete",
        tool_summaries=["gmail.draft created draft"],   # NO send result
        approval_exists=False,
    )
    # Model prose claims it was sent; runtime has no send action.
    env = StructuredResponse(
        response_type="task_complete", status="complete",
        summary="I sent the email to the client.",
    )
    guarded = apply_truth_guard(env, facts)
    # No "sent" action is fabricated; an advisory is appended to risks.
    assert all(a.verb != "sent" for a in guarded.actions)
    assert any("not sent" in r.lower() or "draft" in r.lower() for r in guarded.risks)


@pytest.mark.asyncio
async def test_compose_merges_prose_and_truth(monkeypatch):
    import json as _json
    from core import structured_response as sr

    async def fake_complete_json(prompt, *, model=None):
        return _json.dumps({
            "summary": "Contract review complete. Indemnity is the main risk.",
            "key_findings": ["Uncapped indemnity", "Weak termination rights"],
            "assumptions": ["Reviewed as a general commercial agreement"],
            "risks": ["Jurisdiction-specific enforceability not verified"],
            "next_action": "Review the memo and approve the email draft.",
            "confidence": "medium",
        })

    monkeypatch.setattr(sr, "complete_json", fake_complete_json)

    facts = sr.build_runtime_facts(
        result={"answer": "done", "artifacts": [{"id": "a1", "title": "Risk Memo", "kind": "memo"}]},
        task_status="complete",
        tool_summaries=["gmail.draft created draft"],
        approval_exists=True,
    )
    env = await sr.compose(
        response_type="task_complete",
        answer_text="Contract review complete...",
        facts=facts,
        verbosity="detailed",
    )
    assert env.response_type == "task_complete"
    assert env.status == "complete"                       # runtime
    assert env.approval_status == "drafted_not_sent"      # runtime
    assert [a.id for a in env.artifacts] == ["a1"]        # runtime
    assert "Uncapped indemnity" in env.key_findings       # model prose
    assert env.next_action and "approve" in env.next_action.lower()


@pytest.mark.asyncio
async def test_compose_concise_drops_secondary_sections(monkeypatch):
    import json as _json
    from core import structured_response as sr

    async def fake_complete_json(prompt, *, model=None):
        return _json.dumps({
            "summary": "Done.", "key_findings": ["x"], "assumptions": ["y"],
            "risks": ["z"], "next_action": "Do the thing.", "confidence": "high",
        })

    monkeypatch.setattr(sr, "complete_json", fake_complete_json)
    facts = sr.build_runtime_facts(result={"answer": "done"}, task_status="complete",
                                   tool_summaries=[], approval_exists=False)
    env = await sr.compose(response_type="direct_answer", answer_text="Done.",
                           facts=facts, verbosity="concise")
    # concise: keep summary + next_action, drop findings/assumptions/risks
    assert env.summary == "Done."
    assert env.key_findings == []
    assert env.assumptions == []
    assert env.risks == []
