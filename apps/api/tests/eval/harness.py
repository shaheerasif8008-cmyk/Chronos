"""
Eval harness for the Chronos agent loop.

Runs tasks through the real run_loop() with:
  - Real LLM  (requires OPENROUTER_API_KEY or BACKUP_API_KEY)
  - Mocked tool execution (deterministic, no network)

Scoring is two-tier:
  - Structural (always): Python predicates on result + calls_made
  - Semantic  (optional): LLM judge invoked when RUN_EVAL_SEMANTIC=1

Typical usage:
  RUN_EVAL=1 pytest tests/eval/ -v --tb=short
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ── Availability gate ──────────────────────────────────────────────────────────

def eval_enabled() -> bool:
    return bool(
        os.getenv("RUN_EVAL")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("BACKUP_API_KEY")
    )


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class RubricItem:
    criterion: str
    # check(result_dict, calls_made_list) -> bool
    check: Callable[[dict[str, Any], list[str]], bool]
    weight: float = 1.0


@dataclass
class EvalTask:
    id: str
    goal: str
    rubric: list[RubricItem]
    category: str = "general"
    # Optional per-tool response overrides.  Key = tool name (agent_loop name
    # e.g. "browser__search"), value = callable() -> payload dict.
    tool_responses: dict[str, Callable[[], dict[str, Any]]] = field(default_factory=dict)
    max_iterations: int = 20


@dataclass
class EvalResult:
    task_id: str
    goal: str
    category: str
    result: dict[str, Any]
    calls_made: list[str]
    iterations: int
    elapsed_seconds: float
    rubric_scores: dict[str, bool] = field(default_factory=dict)
    error: str | None = None

    @property
    def score(self) -> float:
        if not self.rubric_scores:
            return 0.0
        return sum(1 for v in self.rubric_scores.values() if v) / len(self.rubric_scores)

    @property
    def passed(self) -> list[str]:
        return [k for k, v in self.rubric_scores.items() if v]

    @property
    def failed(self) -> list[str]:
        return [k for k, v in self.rubric_scores.items() if not v]

    @property
    def grade(self) -> str:
        s = self.score
        if s >= 0.9:
            return "A"
        if s >= 0.75:
            return "B"
        if s >= 0.6:
            return "C"
        if s >= 0.4:
            return "D"
        return "F"


# ── Default tool responses ─────────────────────────────────────────────────────

def _default_tool_response(tool_name: str) -> dict[str, Any]:
    name = tool_name.lower()
    if "search" in name:
        return {
            "summary": "Found 3 relevant results",
            "data": {
                "results": [
                    {
                        "title": "Comprehensive Guide to the Topic",
                        "url": "https://example.com/guide",
                        "snippet": "This article covers the key aspects in detail, with recent data.",
                    },
                    {
                        "title": "Analysis and Comparison",
                        "url": "https://example.com/analysis",
                        "snippet": "A side-by-side comparison of the main options with pros and cons.",
                    },
                    {
                        "title": "Latest Trends Report",
                        "url": "https://example.com/trends",
                        "snippet": "Recent survey data showing adoption patterns and key findings.",
                    },
                ],
                "result_count": 3,
            },
        }
    if "fetch" in name:
        return {
            "summary": "Page content retrieved",
            "data": {
                "content": (
                    "This page contains detailed information about the requested topic. "
                    "Key facts: the main entity was founded in 2010, has 500 employees, "
                    "and operates in 30 countries. Revenue grew 40% YoY to $2B in 2024."
                ),
                "url": "https://example.com/detail",
            },
        }
    if "python" in name or "code" in name:
        return {
            "summary": "Script executed successfully, output captured",
            "data": {"output": "Result: 42\nProcessed 5 rows\nTotal revenue: $1,250.00\n", "files": []},
        }
    if "draft" in name:
        return {
            "summary": "Draft created: Re: Follow-up",
            "data": {"draft_id": "draft-mock-001", "subject": "Re: Follow-up", "thread_id": None},
        }
    if "write" in name:
        return {"summary": "File written: output.txt (128 bytes)", "data": {"path": "output.txt"}}
    if "read" in name:
        return {
            "summary": "File read: 3 pages",
            "data": {"content": "Document content here. Section 1: Introduction. Section 2: Details."},
        }
    return {"summary": f"{tool_name}: completed", "data": {}}


# ── Runner ─────────────────────────────────────────────────────────────────────

async def run_eval_task(task: EvalTask, *, monkeypatch_obj: Any = None) -> EvalResult:
    """Run one eval task through the real run_loop() with mocked I/O."""
    from runtime import agent_loop

    calls_made: list[str] = []
    iterations_box: list[int] = [0]

    task_dict: dict[str, Any] = {
        "id": f"eval-{task.id}",
        "organization_id": "eval-org",
        "region": "us",
        "triggered_by_member_id": "eval-member",
        "workspace_id": None,
        "persona_id": None,
        "status": "pending",
        "goal": task.goal,
        "plan": {},
        "agent_state": {},
        "iteration_count": 0,
        "current_step": 0,
        "started_at": None,
        "depth": 0,
    }

    async def fake_save_task(task_id, **values):
        if "iteration_count" in values:
            iterations_box[0] = int(values["iteration_count"])

    async def fake_emit(task_id, event, actor_id="chronos"):
        pass

    async def fake_publish(task_id, event):
        pass

    async def fake_persist(task_arg, content, **kwargs):
        return "eval-msg-persisted"

    async def fake_execute_tool(call, task_arg, agent):
        calls_made.append(call["name"])
        override = task.tool_responses.get(call["name"])
        payload = override() if callable(override) else _default_tool_response(call["name"])
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "name": call["name"],
            "content": json.dumps(payload),
        }

    async def fake_is_cancelled(task_id):
        return False

    # Patch all I/O except the LLM call — that must be real.
    _patches = {
        "save_task": fake_save_task,
        "emit_activity": fake_emit,
        "publish_activity": fake_publish,
        "_persist_to_conversation": fake_persist,
        "_execute_tool": fake_execute_tool,
        "is_task_cancelled": fake_is_cancelled,
    }
    _originals = {k: getattr(agent_loop, k) for k in _patches}
    for k, v in _patches.items():
        setattr(agent_loop, k, v)

    error: str | None = None
    t0 = time.monotonic()
    try:
        result = await agent_loop.run_loop(task_dict)
    except Exception as exc:
        result = {"error": str(exc)}
        error = str(exc)
    finally:
        for k, v in _originals.items():
            setattr(agent_loop, k, v)
    elapsed = time.monotonic() - t0

    # Score rubric
    rubric_scores: dict[str, bool] = {}
    for item in task.rubric:
        try:
            rubric_scores[item.criterion] = bool(item.check(result, calls_made))
        except Exception:
            rubric_scores[item.criterion] = False

    return EvalResult(
        task_id=task.id,
        goal=task.goal,
        category=task.category,
        result=result,
        calls_made=calls_made,
        iterations=iterations_box[0],
        elapsed_seconds=elapsed,
        rubric_scores=rubric_scores,
        error=error,
    )


# ── Scorecard renderer ─────────────────────────────────────────────────────────

def render_scorecard(results: list[EvalResult]) -> str:
    lines: list[str] = ["# Chronos Agent-Loop Eval Scorecard\n"]

    # Summary table
    lines.append("| Task | Category | Grade | Score | Time | Iterations |")
    lines.append("|------|----------|-------|-------|------|------------|")
    for r in results:
        score_str = f"{r.score * 100:.0f}%"
        time_str = f"{r.elapsed_seconds:.1f}s"
        lines.append(
            f"| {r.task_id} | {r.category} | **{r.grade}** | {score_str} | {time_str} | {r.iterations} |"
        )

    if results:
        overall = sum(r.score for r in results) / len(results)
        passing = sum(1 for r in results if r.score >= 0.75)
        lines.append(
            f"\n**Overall: {overall * 100:.0f}%** — "
            f"{passing}/{len(results)} tasks at B or above\n"
        )

    # Per-task detail
    lines.append("## Detail\n")
    for r in results:
        lines.append(f"### {r.task_id} — {r.grade} ({r.score * 100:.0f}%)")
        lines.append(f"> {r.goal}")
        lines.append(f"\n- Tools used: `{', '.join(r.calls_made) or 'none'}`")
        lines.append(f"- Iterations: {r.iterations}  |  Time: {r.elapsed_seconds:.1f}s")
        if r.failed:
            lines.append(f"- **Failed criteria:** {', '.join(r.failed)}")
        if r.passed:
            lines.append(f"- Passed criteria: {', '.join(r.passed)}")
        if r.error:
            lines.append(f"- Error: `{r.error}`")
        lines.append("")

    return "\n".join(lines)
