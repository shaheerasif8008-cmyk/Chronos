"""Unit tests for the agent-loop cognitive layer (pure, no model/network)."""
from __future__ import annotations

import json

from runtime.cognition import (
    LoopBudget,
    ProgressSignal,
    ProgressTracker,
    Verdict,
    build_plan_prompt,
    build_reflection_prompt,
    call_signature,
    parse_plan,
    parse_verdict,
    plan_directive,
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


def test_build_plan_prompt_includes_goal():
    assert "Summarize the report" in build_plan_prompt("Summarize the report")


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
