"""Standalone research executor — orchestrates a deep-research run end-to-end.

Cooperative cancellation via DB status checks. All events are durably appended to
research_events before being published to Redis (Redis publish failures never crash the
run). Artifacts are saved via core.artifacts; project linking is done via
set_artifact_project when the run has a project_id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from core import research, tool_broker
from core.artifacts import read_artifact_content, save_artifact, set_artifact_project
from core.config import settings
from core.llm import complete_json, complete_text
from core.models import AgentContext, RequesterContext
from core.redis import redis_client
from memory.source_retrieval import retrieve_source_chunks

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _depth_config(depth: str) -> dict:
    """Return per-depth gathering limits.

    Args:
        depth: One of "quick", "standard", "exhaustive", "trusted".  Unknown
            values fall back to "standard".

    Returns:
        Dict with keys max_queries, results_per_query, rounds.
    """
    configs: dict[str, dict] = {
        "quick":      {"max_queries": 2, "results_per_query": 3, "rounds": 1},
        "standard":   {"max_queries": 4, "results_per_query": 4, "rounds": 1},
        "exhaustive": {"max_queries": 6, "results_per_query": 5, "rounds": 2},
        "trusted":    {"max_queries": 4, "results_per_query": 4, "rounds": 1},
    }
    return configs.get(depth, configs["standard"])


async def _is_cancelled(run_id: str, org_id: str) -> bool:
    """Return True if the run's current DB status is 'cancelled'.

    Args:
        run_id: The research run UUID.
        org_id: Tenant scope.
    """
    return await research.get_status(run_id, org_id) == "cancelled"


async def _emit(run_id: str, org_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """Append event to DB (durable) then publish to Redis (best-effort).

    Args:
        run_id: The research run UUID.
        org_id: Tenant scope.
        event_type: Short label for the event (e.g. "research_planning").
        payload: Structured event data.
    """
    seq = await research.append_event(run_id, org_id, event_type, payload)
    try:
        message = json.dumps(
            {
                "type": event_type,
                "run_id": run_id,
                "seq": seq,
                "ts": now_utc().isoformat(),
                **payload,
            },
            default=str,
        )
        await redis_client.publish(f"research:{run_id}", message)
    except Exception:
        # Redis publish is best-effort; DB is the source of truth.
        logger.warning("Redis publish failed for run %s event %s", run_id, event_type)


def _host(url: str) -> str:
    """Extract the hostname from a URL string."""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------

async def run_research(run_id: str, org_id: str) -> None:
    """Execute a deep-research run to completion, failure, or cancellation.

    This is fire-and-forget: the caller should wrap it in asyncio.create_task.
    All unhandled exceptions are caught, the run is marked "failed", and the
    error is persisted — no exception propagates to the caller.

    Args:
        run_id: The UUID of the research_runs row to execute.
        org_id: Tenant scope — must match the run's organization_id.
    """
    try:
        await _run_research_inner(run_id, org_id)
    except Exception as exc:
        logger.exception("research run %s failed: %s", run_id, exc)
        try:
            await research.update_run(
                run_id, org_id,
                status="failed",
                error=str(exc),
                completed_at=now_utc(),
            )
            await _emit(run_id, org_id, "research_failed", {"error": str(exc)})
        except Exception:
            logger.exception("Failed to persist error for run %s", run_id)


async def _run_research_inner(run_id: str, org_id: str) -> None:
    """Inner research execution — may raise; callers must handle exceptions."""
    # --- 1. Load run ---
    run = await research.get_run(run_id, org_id)
    if run is None:
        return

    question: str = run["question"] or ""
    depth: str = run.get("depth") or "standard"
    source_scopes: dict = run.get("source_scopes") or {}
    project_id: str | None = run.get("project_id")
    member_id: str | None = run.get("member_id")
    time_budget_seconds: int | None = run.get("time_budget_seconds")

    cfg = _depth_config(depth)

    allowed_domains: list[str] = source_scopes.get("allowed_domains") or []
    disallowed_domains: list[str] = source_scopes.get("disallowed_domains") or []

    # --- 1.5. Early cancel check (BEFORE any status write) ---
    if await _is_cancelled(run_id, org_id):
        await _emit(run_id, org_id, "research_cancelled", {})
        return

    # --- 1.6. Idempotency: clear any prior citations/events so a recovered or
    # re-invoked run rebuilds evidence cleanly instead of duplicating markers. ---
    await research.reset_run_progress(run_id, org_id)

    # --- 2. Start planning ---
    t0 = time.monotonic()
    await research.update_run(run_id, org_id, status="planning", started_at=now_utc())
    await _emit(run_id, org_id, "research_planning", {"question": question})

    # --- 3. Cancel check post-update ---
    if await _is_cancelled(run_id, org_id):
        await _emit(run_id, org_id, "research_cancelled", {})
        return

    # --- 4. Plan: ask LLM for search queries ---
    plan_prompt = (
        f"You are a research assistant. Generate a JSON object with the key "
        f'"queries" containing a list of up to {cfg["max_queries"]} focused, '
        f"specific search queries for the following research question. "
        f"Return ONLY valid JSON.\n\nQuestion: {question}"
    )
    try:
        plan_json = await complete_json(plan_prompt)
        parsed = json.loads(plan_json)
        queries: list[str] = parsed.get("queries") or [question]
    except Exception:
        queries = [question]

    queries = queries[: cfg["max_queries"]]
    await research.update_run(run_id, org_id, plan={"queries": queries, "rounds": cfg["rounds"]})
    await _emit(run_id, org_id, "research_plan", {"queries": queries})

    # --- 5. Set up gathering state ---
    await research.update_run(run_id, org_id, status="running")
    limitations: list[str] = []
    n = 1  # citation marker counter (S1, S2, ...)
    web_fallback_warned = False
    skipped_scopes: set[str] = set()
    connector_citations_found = False

    # Build AgentContext for web tool calls (no project_id field on AgentContext)
    agent = AgentContext(
        id=f"research:{run_id}",
        org_id=org_id,
        member_id=member_id or "chronos",
    )

    # --- 6. Gather loop ---
    for query in queries:
        # Time-budget check
        if time_budget_seconds is not None and time.monotonic() - t0 > time_budget_seconds:
            limitations.append("Time budget exceeded; gathering stopped early.")
            break

        # Cancel check per query
        if await _is_cancelled(run_id, org_id):
            await _emit(run_id, org_id, "research_cancelled", {})
            return

        await _emit(run_id, org_id, "research_query", {"query": query})

        # --- Web source ---
        use_web = bool(source_scopes.get("web")) and not (
            depth == "trusted" and not allowed_domains
        )
        if use_web:
            try:
                search_result = await tool_broker.execute(
                    agent, "browser.search",
                    {"query": query, "max_results": cfg["results_per_query"]},
                )
                data = search_result.data
                is_fallback = data.get("is_fallback") or data.get("tier") in {"fixture", "demo"}
                if is_fallback:
                    if not web_fallback_warned:
                        limitations.append(
                            "Live web search was unavailable; web results were not used."
                        )
                        web_fallback_warned = True
                else:
                    items = data.get("results") or []
                    fetched_count = 0
                    for item in items:
                        if fetched_count >= cfg["results_per_query"]:
                            break
                        url = item.get("url")
                        if not url:
                            continue
                        # Domain filtering
                        host = _host(url)
                        if allowed_domains and host not in allowed_domains:
                            continue
                        if disallowed_domains and host in disallowed_domains:
                            continue
                        # Fetch full content
                        try:
                            fetch_result = await tool_broker.execute(
                                agent, "browser.fetch", {"url": url}
                            )
                            fetched_data = fetch_result.data
                        except Exception:
                            fetched_data = {}

                        snippet = item.get("snippet") or ""
                        if not snippet:
                            snippet = (fetched_data.get("content") or "")[:600]
                        if not snippet.strip():
                            continue  # no citation without snippet

                        await research.add_citation(
                            run_id, org_id,
                            marker=f"S{n}",
                            source_type="web",
                            snippet=snippet,
                            url=url,
                            source_title=item.get("title"),
                            metadata={"untrusted_content": fetched_data.get("untrusted_content")},
                        )
                        await _emit(
                            run_id, org_id, "research_citation",
                            {"marker": f"S{n}", "source_type": "web", "url": url},
                        )
                        n += 1
                        fetched_count += 1
            except Exception as exc:
                logger.warning("Web search failed for query %r: %s", query, exc)

        # --- Indexed project and connector sources ---
        indexed_scopes_enabled = bool(source_scopes.get("project") or source_scopes.get("connector"))
        if indexed_scopes_enabled and project_id:
            rc = RequesterContext(
                org_id=org_id,
                member_id=member_id or "chronos",
                project_id=project_id,
            )
            try:
                chunks = await retrieve_source_chunks(query, rc, limit=cfg["results_per_query"])
                for chunk in chunks:
                    if not chunk.snippet.strip():
                        continue
                    source_type = getattr(chunk, "source_type", "project") or "project"
                    if source_type == "connector":
                        if not source_scopes.get("connector"):
                            continue
                        connector_citations_found = True
                    elif not source_scopes.get("project"):
                        continue
                    await research.add_citation(
                        run_id, org_id,
                        marker=f"S{n}",
                        source_type=source_type,
                        snippet=chunk.snippet,
                        source_id=chunk.source_id,
                        source_title=chunk.source_title,
                        distance=chunk.distance,
                    )
                    await _emit(
                        run_id, org_id, "research_citation",
                        {"marker": f"S{n}", "source_type": source_type, "source_id": chunk.source_id},
                    )
                    n += 1
            except Exception as exc:
                logger.warning("Indexed source retrieval failed for query %r: %s", query, exc)

        if source_scopes.get("connector") and not connector_citations_found and "connector" not in skipped_scopes:
            skipped_scopes.add("connector")
            await _emit(
                run_id, org_id, "research_source_skipped",
                {"scope": "connector", "reason": "no indexed connector sources available"},
            )
            limitations.append("Connector sources are not yet indexed and were not used.")

        # --- Unsupported scope types: upload, mcp ---
        for scope in ("upload", "mcp"):
            if source_scopes.get(scope) and scope not in skipped_scopes:
                skipped_scopes.add(scope)
                await _emit(
                    run_id, org_id, "research_source_skipped",
                    {"scope": scope, "reason": "no index available"},
                )
                limitations.append(
                    f"{scope} sources are not yet indexed and were not used."
                )

    # No citations at all
    citations_so_far = await research.list_citations(run_id, org_id)
    if not citations_so_far:
        limitations.append("No sources could be gathered for this question.")

    # --- 7. Cancel check before synthesis ---
    if await _is_cancelled(run_id, org_id):
        await _emit(run_id, org_id, "research_cancelled", {})
        return

    # --- 8. Synthesize ---
    citations = await research.list_citations(run_id, org_id)
    citation_lines = "\n".join(
        f"[{c['marker']}] {c.get('source_title') or c.get('url') or 'Source'}: {c['snippet']}"
        for c in citations
    )
    synthesis_prompt = (
        f"You are a research analyst. Write a comprehensive markdown report answering the "
        f"following research question using the provided sources. Cite sources inline using "
        f"their [S#] markers. Be honest about gaps and uncertainties.\n\n"
        f"Research Question: {question}\n\n"
        f"Sources:\n{citation_lines or '(none)'}\n\n"
        f"Write a well-structured markdown report:"
    )
    try:
        model_report = await complete_text(synthesis_prompt)
    except Exception as exc:
        model_report = f"Report generation failed: {exc}"

    # Limitations section
    if limitations:
        limitations_md = "## Limitations\n" + "\n".join(f"- {lim}" for lim in limitations)
    else:
        limitations_md = "## Limitations\n- None noted."

    appendix = research.build_source_appendix(citations)
    report_md = model_report + "\n\n" + limitations_md
    if appendix:
        report_md = report_md + "\n\n" + appendix

    await research.update_run(
        run_id, org_id,
        findings={"summary": model_report[:500], "citation_count": len(citations)},
        limitations="\n".join(limitations) if limitations else None,
    )

    # --- 9. Save artifact ---
    artifact_id = await save_artifact(
        report_md,
        kind="markdown",
        title=f"Research: {question[:80]}",
        org_id=org_id,
        region=settings.region,
        created_by=member_id or "chronos",
    )
    if project_id:
        try:
            await set_artifact_project(artifact_id, project_id=project_id, org_id=org_id)
        except Exception as exc:
            logger.warning("set_artifact_project failed for artifact %s: %s", artifact_id, exc)

    await research.update_run(run_id, org_id, report_artifact_id=artifact_id)
    await _emit(run_id, org_id, "research_report", {"artifact_id": artifact_id})

    # --- 10. Complete ---
    await research.update_run(run_id, org_id, status="complete", completed_at=now_utc())
    await _emit(
        run_id, org_id, "research_complete",
        {"artifact_id": artifact_id, "citation_count": len(citations)},
    )


async def start_research(run_id: str, org_id: str) -> None:
    """Fire-and-forget wrapper: schedule run_research as an asyncio task.

    Args:
        run_id: The UUID of the research_runs row to execute.
        org_id: Tenant scope.
    """
    asyncio.create_task(run_research(run_id, org_id))
