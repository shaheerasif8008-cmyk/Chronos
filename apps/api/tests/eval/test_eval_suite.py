"""
Pytest integration for the Chronos agent-loop eval suite.

Requires a live model API key.  Skipped automatically when none is configured.
Run with:
    RUN_EVAL=1 pytest tests/eval/ -v --tb=short

Or with a specific task subset:
    RUN_EVAL=1 pytest tests/eval/ -k "T01 or T07" -v

Output: scorecard printed to stdout after all tasks complete.
Optional JSON file output: set EVAL_OUTPUT_PATH env var.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
import pytest_asyncio

from tests.eval.harness import EvalResult, eval_enabled, render_scorecard, run_eval_task
from tests.eval.suite import ALL_TASKS, TASK_BY_ID

pytestmark = pytest.mark.asyncio

# Skip the whole module unless an API key is configured.
if not eval_enabled():
    pytestmark = [
        pytest.mark.asyncio,
        pytest.mark.skip(reason="No model API key configured (set OPENROUTER_API_KEY or RUN_EVAL=1)"),
    ]

# Accumulate results across tests for the final scorecard.
_results: list[EvalResult] = []


@pytest.fixture(autouse=True)
def _collect_result(request):
    yield
    result = getattr(request.node, "_eval_result", None)
    if result is not None:
        _results.append(result)


def _make_test(task_obj):
    async def _test(request):
        result = await run_eval_task(task_obj)
        request.node._eval_result = result

        # Fail the pytest test if fewer than 60% of rubric criteria pass,
        # so CI goes red on regressions while allowing partial scoring.
        if result.score < 0.6:
            failed = ", ".join(result.failed)
            pytest.fail(
                f"{task_obj.id}: score {result.score * 100:.0f}% — failed: {failed}",
                pytrace=False,
            )

    _test.__name__ = f"test_{task_obj.id}"
    return _test


# Dynamically generate one test function per task so pytest -k filtering works.
for _task in ALL_TASKS:
    globals()[f"test_{_task.id}"] = _make_test(_task)
del _task


def pytest_sessionfinish(session, exitstatus):
    """Print and optionally write the scorecard after all eval tests run."""
    if not _results:
        return
    scorecard = render_scorecard(_results)
    print("\n\n" + scorecard)

    output_path = os.getenv("EVAL_OUTPUT_PATH")
    if output_path:
        p = pathlib.Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write both markdown and JSON
        p.with_suffix(".md").write_text(scorecard)
        p.with_suffix(".json").write_text(
            json.dumps(
                [
                    {
                        "task_id": r.task_id,
                        "category": r.category,
                        "score": r.score,
                        "grade": r.grade,
                        "iterations": r.iterations,
                        "elapsed_seconds": r.elapsed_seconds,
                        "passed": r.passed,
                        "failed": r.failed,
                        "error": r.error,
                    }
                    for r in _results
                ],
                indent=2,
            )
        )
