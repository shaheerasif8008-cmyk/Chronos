"""Unit tests for the agent-loop cognitive layer (pure, no model/network)."""
from __future__ import annotations

import json

from runtime.cognition import (
    LoopBudget,
    PlanComplianceSignal,
    ProgressSignal,
    ProgressTracker,
    Verdict,
    assess_plan_compliance,
    build_evidence_verification_prompt,
    build_plan_prompt,
    build_reflection_prompt,
    build_task_ledger,
    call_signature,
    derive_trace_learning_notes,
    parse_plan,
    parse_verdict,
    plan_directive,
    render_task_ledger,
    score_task_trace,
    summarize_tools,
    subagent_orchestration_directive,
    tool_batching_directive,
)


# ── Budget ──────────────────────────────────────────────────────────────────


def test_budget_directive_escalates_as_steps_run_out():
    b = LoopBudget(max_iterations=40)
    assert b.directive() is None  # plenty of room early

    b.used = 36  # 4 left → soft window (40//3 ≈ 13)
    assert "Prioritize" in (b.directive() or "")

    b.used = 38  # 2 left → hard
    assert "only 2 tool-step" in (b.directive() or "")

    b.used = 40
    assert "no tool-steps remain" in (b.directive() or "")
    assert b.exhausted is True
    assert b.remaining == 0


def test_budget_reflection_and_replan_caps():
    b = LoopBudget(max_iterations=10, max_reflections=2, max_replans=1)
    assert b.can_reflect and b.can_replan
    b.reflections_used = 2
    b.replans_used = 1
    assert not b.can_reflect and not b.can_replan


# ── Progress / loop detection ───────────────────────────────────────────────


def _ok_tool_msg(name="browser__search", payload=None):
    return {"role": "tool", "name": name, "content": json.dumps(payload or {"summary": "ok"})}


def _err_tool_msg(name="browser__search", error="boom"):
    return {"role": "tool", "name": name, "content": json.dumps({"error": error})}


def test_call_signature_is_stable_and_args_sensitive():
    a = call_signature("t", {"q": "x", "n": 1})
    b = call_signature("t", {"n": 1, "q": "x"})  # key order independent
    c = call_signature("t", {"q": "y", "n": 1})
    assert a == b
    assert a != c


def test_repeated_identical_call_is_detected():
    pt = ProgressTracker(repeat_threshold=2)
    call = {"name": "browser__search", "args": {"q": "same"}}
    assert pt.record([call], [_ok_tool_msg()]) == ProgressSignal.OK
    assert pt.record([call], [_ok_tool_msg()]) == ProgressSignal.OK
    # third identical call crosses the threshold
    assert pt.record([call], [_ok_tool_msg()]) == ProgressSignal.REPEAT_CALL
    assert pt.directive(ProgressSignal.REPEAT_CALL).startswith("Controller observation")


def test_repeated_identical_error_is_detected():
    pt = ProgressTracker(repeat_threshold=2)
    # vary args so it's not flagged as a repeat *call*, only a repeat error
    for i in range(3):
        sig = pt.record(
            [{"name": "browser__search", "args": {"q": f"q{i}"}}],
            [_err_tool_msg(error="rate limited")],
        )
    assert sig == ProgressSignal.REPEAT_ERROR


def test_stall_after_unproductive_window():
    pt = ProgressTracker(stall_window=3)
    sig = ProgressSignal.OK
    for i in range(3):
        sig = pt.record(
            [{"name": "browser__search", "args": {"q": f"q{i}"}}],
            [_err_tool_msg(error=f"different-{i}")],
        )
    assert sig in (ProgressSignal.STALLED, ProgressSignal.REPEAT_ERROR)
    # a productive iteration resets the streak
    pt.unproductive_streak = 5
    pt.record([{"name": "x", "args": {"a": 1}}], [_ok_tool_msg(name="x")])
    assert pt.unproductive_streak == 0


def test_should_force_finish_after_enough_breaks():
    pt = ProgressTracker()
    for _ in range(3):
        pt.directive(ProgressSignal.STALLED)
    assert pt.should_force_finish is True


# ── Plan ─────────────────────────────────────────────────────────────────────


def test_parse_plan_produces_ui_step_shape():
    raw = json.dumps(
        {
            "steps": [
                {"description": "Research competitors", "tool_hint": "browser"},
                {"description": "Draft the memo", "tool_hint": None},
            ]
        }
    )
    steps = parse_plan(raw)
    assert steps is not None and len(steps) == 2
    assert steps[0] == {
        "id": "step-1",
        "action": "think",
        "description": "Research competitors",
        "tool": "browser",
    }
    assert steps[1]["tool"] is None


def test_parse_plan_rejects_garbage():
    assert parse_plan("not json") is None
    assert parse_plan(json.dumps({"steps": []})) is None
    assert parse_plan(json.dumps({"nope": 1})) is None


def test_parse_plan_caps_steps():
    raw = json.dumps({"steps": [{"description": f"s{i}"} for i in range(20)]})
    steps = parse_plan(raw, max_steps=4)
    assert len(steps) == 4


def test_plan_directive_marks_current_step():
    steps = parse_plan(json.dumps({"steps": [{"description": "a"}, {"description": "b"}, {"description": "c"}]}))
    text = plan_directive(steps, current_step=1)
    assert "→ 2. b" in text
    assert "✓ 1. a" in text
    assert "· 3. c" in text


def test_assess_plan_compliance_reports_tool_mismatch_and_blocked_steps():
    steps = parse_plan(json.dumps({"steps": [{"description": "Research", "tool_hint": "browser"}]}))
    mismatch = assess_plan_compliance(
        steps,
        current_step=0,
        calls=[{"name": "gmail__search"}],
        tool_messages=[],
    )
    assert mismatch.signal == PlanComplianceSignal.TOOL_MISMATCH
    assert "browser" in mismatch.directive

    blocked = assess_plan_compliance(
        steps,
        current_step=0,
        calls=[{"name": "browser__search"}],
        tool_messages=[_err_tool_msg(error="rate limited")],
    )
    assert blocked.signal == PlanComplianceSignal.STEP_BLOCKED
    assert "work around" in blocked.directive

    on_plan = assess_plan_compliance(
        steps,
        current_step=0,
        calls=[{"name": "browser__search"}],
        tool_messages=[_ok_tool_msg()],
    )
    assert on_plan.signal == PlanComplianceSignal.ON_PLAN
    assert on_plan.should_advance is True


def test_build_plan_prompt_includes_goal():
    assert "Summarize the report" in build_plan_prompt("Summarize the report")


def test_summarize_tools_groups_by_family():
    tools = [
        {"type": "function", "function": {"name": "browser__search"}},
        {"type": "function", "function": {"name": "browser__fetch"}},
        {"type": "function", "function": {"name": "gmail__draft"}},
    ]
    text = summarize_tools(tools)
    assert "browser: search, fetch" in text
    assert "gmail: draft" in text
    # Empty / missing input degrades to an empty summary (no crash).
    assert summarize_tools(None) == ""
    assert summarize_tools([]) == ""


def test_build_plan_prompt_embeds_tools_and_availability():
    prompt = build_plan_prompt(
        "Find leads",
        tools_summary="- browser: search",
        availability_note="- gmail: demo storage, not real",
    )
    assert "Find leads" in prompt
    assert "browser: search" in prompt
    assert "demo storage, not real" in prompt
    # Without the kwargs the prompt stays clean (backward compatible).
    assert "Tools available" not in build_plan_prompt("Find leads")


# ── Reflection ───────────────────────────────────────────────────────────────


def test_parse_verdict_complete_and_incomplete():
    v = parse_verdict(json.dumps({"complete": True, "missing": [], "directive": ""}))
    assert isinstance(v, Verdict) and v.complete is True

    v2 = parse_verdict(
        json.dumps({"complete": False, "missing": ["no sources"], "directive": "cite sources"})
    )
    assert v2.complete is False
    assert v2.missing == ["no sources"]
    assert v2.directive == "cite sources"


def test_parse_verdict_rejects_garbage():
    assert parse_verdict("nope") is None
    assert parse_verdict(json.dumps({"missing": []})) is None  # no 'complete' key


def test_build_reflection_prompt_includes_goal_answer_evidence():
    p = build_reflection_prompt("Goal X", "Answer Y", ["did A", "did B"])
    assert "Goal X" in p and "Answer Y" in p and "did A" in p


# ── Controller ledger / verification / strategy ─────────────────────────────


def test_task_ledger_renders_compact_state_without_losing_evidence():
    history = [
        {"role": "user", "content": "Find sources"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "browser__search"}}]},
        _ok_tool_msg("browser__search", {"summary": "Found sources", "data": {"artifact_id": "art-1"}}),
        _err_tool_msg("browser__fetch", "timeout"),
    ]
    ledger = build_task_ledger(
        "Find sources",
        history,
        plan_steps=[{"description": "Search web", "tool": "browser"}],
        current_plan_step=0,
        token_usage={"total_tokens": 1234},
    )
    assert ledger["goal"] == "Find sources"
    assert ledger["attempted_tools"] == ["browser__search", "browser__fetch"]
    assert ledger["open_errors"][0]["tool"] == "browser__fetch"
    assert ledger["artifacts"] == ["art-1"]
    text = render_task_ledger(ledger)
    assert "# Controller Task Ledger" in text
    assert "Found sources" in text
    assert "timeout" in text


def test_build_evidence_verification_prompt_uses_ledger_and_tool_summaries():
    ledger = {
        "goal": "Write a sourced memo",
        "recent_evidence": ["- browser__search; summary=Found sources"],
        "open_errors": [],
        "artifacts": ["memo.md"],
    }
    prompt = build_evidence_verification_prompt("Write a sourced memo", "Done", ledger, ["browser: found"])
    assert "Return ONLY JSON" in prompt
    assert "memo.md" in prompt
    assert "browser: found" in prompt


def test_strategy_directives_cover_subagents_and_code_batching():
    steps = parse_plan(
        json.dumps(
            {
                "steps": [
                    {"description": "Research market", "tool_hint": "browser"},
                    {"description": "Analyze data", "tool_hint": "code"},
                    {"description": "Draft memo", "tool_hint": None},
                ]
            }
        )
    )
    subagent = subagent_orchestration_directive(
        "Compare all competitors",
        steps,
        available_tool_names=["spawn__subagent", "browser__search"],
    )
    assert subagent and "parallel sub-agents" in subagent

    batching = tool_batching_directive(["code__python", "browser__search", "browser__fetch", "fs__read"])
    assert batching and "batch" in batching.lower()


def test_trace_scoring_and_learning_notes_surface_runtime_risks():
    history = [
        {"role": "assistant", "tool_calls": [{"function": {"name": "browser__search"}}]},
        _err_tool_msg("browser__search", "rate limited"),
        {"role": "assistant", "tool_calls": [{"function": {"name": "browser__search"}}]},
        _ok_tool_msg("browser__search", {"summary": "Found evidence"}),
        {"role": "assistant", "content": "Final answer"},
    ]
    metrics = score_task_trace(history, {"answer": "Final answer"})
    assert metrics["tool_calls"] == 2
    assert metrics["tool_errors"] == 1
    assert metrics["final_answer_present"] is True

    notes = derive_trace_learning_notes(metrics, {"open_errors": [{"tool": "browser__search"}]})
    assert any("recovery playbook" in note for note in notes)


# ── Dynamic model routing ────────────────────────────────────────────────────


def test_stronger_agent_model_escalates_up_the_ladder():
    from core.llm import stronger_agent_model

    assert stronger_agent_model("openrouter/deepseek/deepseek-v4-flash") == "openrouter/openai/gpt-5.4-nano"
    assert stronger_agent_model("openrouter/deepseek/deepseek-v4-pro") == "openrouter/openai/gpt-5.4-mini"
    # already strongest → no escalation
    assert stronger_agent_model("openrouter/openai/gpt-5.4-mini") is None
    # the ':free' suffix is ignored when ranking
    assert stronger_agent_model("openrouter/deepseek/deepseek-v4-flash:free") == "openrouter/openai/gpt-5.4-nano"
    # unknown model escalates to the strongest known tier
    assert stronger_agent_model("some/unknown-model") == "openrouter/openai/gpt-5.4-mini"
