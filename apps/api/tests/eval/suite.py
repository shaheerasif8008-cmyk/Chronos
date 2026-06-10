"""
Graded eval task suite for the Chronos agent loop.

12 tasks across 6 categories, each with structural rubric criteria that
run without needing an LLM judge. Score = fraction of criteria passed.
"""
from __future__ import annotations

from typing import Any

from tests.eval.harness import EvalTask, RubricItem


def _has_answer(result: dict, calls: list[str]) -> bool:
    answer = result.get("answer")
    return isinstance(answer, str) and len(answer.strip()) > 10


def _no_error(result: dict, calls: list[str]) -> bool:
    return "error" not in result or result.get("error") in (None, "")


def _used_tool(tool_substring: str):
    def check(result: dict, calls: list[str]) -> bool:
        return any(tool_substring in c for c in calls)
    check.__name__ = f"used_{tool_substring}"
    return check


def _answer_contains(keyword: str):
    def check(result: dict, calls: list[str]) -> bool:
        answer = str(result.get("answer") or "").lower()
        return keyword.lower() in answer
    check.__name__ = f"answer_contains_{keyword}"
    return check


def _answer_length_at_least(min_words: int):
    def check(result: dict, calls: list[str]) -> bool:
        answer = str(result.get("answer") or "")
        return len(answer.split()) >= min_words
    check.__name__ = f"answer_min_{min_words}_words"
    return check


def _iterations_at_most(max_iters: int):
    # Iteration count is on the result context, not the result dict.
    # The rubric check is applied post-run via EvalResult.iterations.
    # This returns True always — the harness checks iterations separately.
    def check(result: dict, calls: list[str]) -> bool:
        return True
    check.__name__ = f"iterations_le_{max_iters}"
    return check


def _did_not_use_tool(tool_substring: str):
    def check(result: dict, calls: list[str]) -> bool:
        return not any(tool_substring in c for c in calls)
    check.__name__ = f"no_{tool_substring}_call"
    return check


# ── T01: Simple factual answer — no tools required ────────────────────────────

T01 = EvalTask(
    id="T01_simple_math",
    category="assistant",
    goal="What is 1337 multiplied by 42? Give me just the number.",
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("answer_has_number", lambda r, c: any(ch.isdigit() for ch in str(r.get("answer") or ""))),
        RubricItem("no_tool_needed", _did_not_use_tool("browser")),
    ],
)


# ── T02: Current-events research — browser.search expected ────────────────────

T02 = EvalTask(
    id="T02_research_ai_companies",
    category="research",
    goal=(
        "What are the 3 most prominent AI model companies as of 2025? "
        "List them with one sentence about each."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("uses_search", _used_tool("browser__search")),
        RubricItem("lists_companies", _answer_length_at_least(30)),
    ],
)


# ── T03: Multi-source synthesis — multiple search calls expected ───────────────

T03 = EvalTask(
    id="T03_cloud_comparison",
    category="research",
    goal=(
        "Compare AWS, Azure, and Google Cloud on three dimensions: pricing model, "
        "AI/ML services, and geographic coverage. Output a markdown table."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("uses_search", _used_tool("browser")),
        RubricItem("answer_is_substantive", _answer_length_at_least(60)),
        RubricItem(
            "mentions_all_three",
            lambda r, c: all(
                name in str(r.get("answer") or "").lower()
                for name in ("aws", "azure", "google")
            ),
        ),
    ],
)


# ── T04: Code execution — code tool expected ──────────────────────────────────

T04 = EvalTask(
    id="T04_data_analysis",
    category="code",
    goal=(
        "Write and run a Python script that generates 5 rows of sample sales data "
        "(product, qty, unit_price) and computes total revenue. Show the output."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("uses_code_tool", lambda r, c: any("code" in x or "python" in x for x in c)),
        RubricItem("answer_references_revenue", lambda r, c: any(
            kw in str(r.get("answer") or "").lower()
            for kw in ("revenue", "total", "$", "price")
        )),
    ],
)


# ── T05: Email composition — answer should contain email-like content ──────────

T05 = EvalTask(
    id="T05_email_draft",
    category="composition",
    goal=(
        "Draft a professional follow-up email to client@example.com about our Q3 "
        "product proposal. Subject: Q3 Proposal Follow-Up. Keep it under 150 words."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("answer_mentions_subject", lambda r, c: "q3" in str(r.get("answer") or "").lower()),
        RubricItem("answer_has_greeting_or_closing", lambda r, c: any(
            kw in str(r.get("answer") or "").lower()
            for kw in ("dear", "hello", "regards", "sincerely", "best")
        )),
        RubricItem("answer_is_concise", lambda r, c: len(str(r.get("answer") or "").split()) <= 250),
    ],
)


# ── T06: Deep research + prose synthesis ──────────────────────────────────────

T06 = EvalTask(
    id="T06_ai_models_comparison",
    category="research",
    goal=(
        "Research Claude, GPT-4o, and Gemini 2.5. Write a 3-paragraph executive "
        "summary comparing their key strengths for enterprise use."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("uses_search", _used_tool("browser__search")),
        RubricItem("answer_is_substantive", _answer_length_at_least(100)),
        RubricItem(
            "mentions_claude",
            lambda r, c: "claude" in str(r.get("answer") or "").lower(),
        ),
        RubricItem(
            "mentions_gemini",
            lambda r, c: "gemini" in str(r.get("answer") or "").lower(),
        ),
    ],
)


# ── T07: Error recovery — tool returns empty/error ────────────────────────────

T07 = EvalTask(
    id="T07_empty_search_recovery",
    category="reliability",
    goal=(
        "Search for 'ZX9-Nonexistent-Product-Alpha-7' and summarize any findings. "
        "If nothing is found, say so clearly."
    ),
    tool_responses={
        "browser__search": lambda: {
            "summary": "No results found",
            "data": {"results": [], "result_count": 0},
        }
    },
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("honest_about_no_results", lambda r, c: any(
            kw in str(r.get("answer") or "").lower()
            for kw in ("no results", "nothing found", "couldn't find", "could not find",
                       "no information", "not found", "0 results", "unavailable")
        )),
    ],
)


# ── T08: Budget awareness — should not run forever ───────────────────────────

T08 = EvalTask(
    id="T08_budget_awareness",
    category="reliability",
    goal=(
        "Do a thorough research on the complete history of artificial intelligence "
        "from 1950 to today, covering all major milestones, researchers, and "
        "breakthroughs. Be comprehensive."
    ),
    max_iterations=20,
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("answer_is_substantive", _answer_length_at_least(50)),
        # Budget-awareness: should produce an answer, not just grind to cap
        RubricItem("finishes_before_cap", lambda r, c: "max_iterations" not in str(r.get("error") or "")),
    ],
)


# ── T09: Structured output — JSON response ────────────────────────────────────

T09 = EvalTask(
    id="T09_structured_json",
    category="assistant",
    goal=(
        "List the G7 countries and their capitals in JSON format. "
        "Return exactly: {\"countries\": [{\"name\": \"...\", \"capital\": \"...\"}]}"
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("answer_has_json_markers", lambda r, c: (
            "{" in str(r.get("answer") or "") and "}" in str(r.get("answer") or "")
        )),
        RubricItem("answer_mentions_capital", lambda r, c: (
            "capital" in str(r.get("answer") or "").lower()
        )),
        RubricItem("mentions_g7_members", lambda r, c: sum(
            1 for country in ("france", "germany", "italy", "japan", "canada", "united states", "united kingdom")
            if country in str(r.get("answer") or "").lower()
        ) >= 5),
    ],
)


# ── T10: Code artifact — write + run Python ──────────────────────────────────

T10 = EvalTask(
    id="T10_prime_numbers",
    category="code",
    goal=(
        "Write a Python script that generates all prime numbers up to 100 "
        "using the Sieve of Eratosthenes, then run it and show the output."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("uses_code_tool", lambda r, c: any("code" in x or "python" in x for x in c)),
        RubricItem("answer_references_primes", lambda r, c: any(
            kw in str(r.get("answer") or "").lower()
            for kw in ("prime", "sieve", "2, 3", "97")
        )),
    ],
)


# ── T11: Memory / context honesty ─────────────────────────────────────────────

T11 = EvalTask(
    id="T11_hallucination_guard",
    category="reliability",
    goal=(
        "What is the current population of the city of Zorgonia on Mars? "
        "Be direct and honest in your answer."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("does_not_hallucinate_number", lambda r, c: not any(
            f"{n:,}" in str(r.get("answer") or "") or f"{n}" in str(r.get("answer") or "")
            for n in range(1000, 10_000_000, 1000)
        ) or any(
            kw in str(r.get("answer") or "").lower()
            for kw in ("not real", "doesn't exist", "does not exist", "fictional",
                       "no city", "mars is not inhabited", "no human", "no population",
                       "hypothetical", "no such")
        )),
        RubricItem("acknowledges_nonexistence", lambda r, c: any(
            kw in str(r.get("answer") or "").lower()
            for kw in ("not", "no", "fictional", "hypothetical", "doesn't exist",
                       "does not exist", "mars", "uninhabited")
        )),
    ],
)


# ── T12: Plan + reflect cycle — complex multi-part task ───────────────────────

T12 = EvalTask(
    id="T12_multi_part_synthesis",
    category="research",
    goal=(
        "Research the current funding landscape for climate tech startups. "
        "Find: (1) total VC investment in 2024, (2) top 3 funded sub-sectors, "
        "(3) leading investors. Write a 2-paragraph briefing."
    ),
    rubric=[
        RubricItem("completes", _has_answer),
        RubricItem("no_error", _no_error),
        RubricItem("uses_search", _used_tool("browser")),
        RubricItem("answer_is_substantive", _answer_length_at_least(80)),
        RubricItem("covers_multiple_parts", lambda r, c: (
            len(str(r.get("answer") or "").split()) >= 100
        )),
    ],
)


# ── Full suite ─────────────────────────────────────────────────────────────────

ALL_TASKS: list[EvalTask] = [T01, T02, T03, T04, T05, T06, T07, T08, T09, T10, T11, T12]

TASK_BY_ID: dict[str, EvalTask] = {t.id: t for t in ALL_TASKS}
