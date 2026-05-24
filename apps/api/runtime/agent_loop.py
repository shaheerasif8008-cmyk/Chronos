"""
Native agent loop — mirrors Claude Code's approach.

No upfront plan. The LLM emits tool_use blocks, Chronos executes them
via the ToolBroker, results come back as tool messages, and the LLM
decides the next move. Loop continues until the LLM emits a final text
response (no tool calls) or MAX_ITERATIONS is hit.

Multiple tool calls in a single LLM response are executed in parallel
via asyncio.gather(), matching Claude Code's native parallel tool use.

State stored in tasks.agent_state:
    {
        "agent_history": [...],   # full message list for resume
        "iteration_count": int,
        "pending_approval_calls": [...],   # set while awaiting_approval
    }

The tasks table still exists for persistence and observability — it stores
loop state (messages + tool results) rather than a pre-generated JSON plan.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import litellm
from sqlalchemy import insert, select, update

from core import audit, tool_broker
from core.config import settings
from core.db import engine, reflect_table
from core.exceptions import ApprovalRequired
from core.llm import _message_content, _message, _tool_calls, _with_retry, complete_text, model_kwargs
from core.models import AgentContext
from core.redis import redis_client
from core.token_budget import compact_agent_history, estimate_tokens, estimate_messages_tokens
from runtime.tool_registry import (
    ALL_TOOLS,
    ALWAYS_APPROVAL_TOOL_NAMES,
    SUBAGENT_TOOLS,
    _SUBAGENT_TOOL_NAME,
    to_broker_name,
)

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 40
MAX_DEPTH = 3


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_task(task_id: str) -> dict[str, Any] | None:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
    return dict(row) if row else None


async def save_task(task_id: str, **values: Any) -> None:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))


# ── Conversation persistence (source of truth, independent of SSE) ────────────

_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", __import__("re").IGNORECASE
)


def _conversation_id_for(task: dict[str, Any]) -> str | None:
    """Return the conversation UUID this task should post to, or None.

    Only top-level tasks (depth 0) triggered directly by a conversation post
    a result message. Sub-agents (triggered_by 'task:...') and manual/API
    tasks (triggered_by 'manual') do not.
    """
    if int(task.get("depth") or 0) != 0:
        return None
    triggered_by = str(task.get("triggered_by") or "")
    if _UUID_RE.match(triggered_by):
        return triggered_by
    return None


def format_task_answer(result: dict[str, Any]) -> str:
    """Convert a task result dict to a readable message string for the chat.

    Canonical formatter shared by the agent loop (persistence) and the chat
    router (live streaming) so the saved message and the streamed text match.
    """
    if not result:
        return "Task completed."

    answer = result.get("answer")
    if isinstance(answer, str) and answer.strip():
        return answer.strip()

    parts: list[str] = []

    findings = result.get("findings")
    if isinstance(findings, list) and findings:
        for item in findings:
            if not isinstance(item, dict):
                continue
            title = item.get("title", "")
            summary = item.get("summary") or item.get("snippet", "")
            url = item.get("url", "")
            if title:
                parts.append(f"**{title}**" + (f"  \n{url}" if url else ""))
            if summary:
                parts.append(f"{summary}\n")

    leads = result.get("leads")
    if isinstance(leads, list) and leads:
        parts.append(f"**{len(leads)} leads found:**\n")
        for lead in leads[:10]:
            if isinstance(lead, dict):
                company = lead.get("company", "Unknown")
                stage = lead.get("stage", "")
                signal = lead.get("hiring_signal", "")
                line = f"- **{company}**" + (f" ({stage})" if stage else "")
                if signal:
                    line += f"  \n  {signal}"
                parts.append(line)

    drafts = result.get("drafts")
    if isinstance(drafts, list) and drafts:
        parts.append(f"\n{len(drafts)} email drafts created and waiting in Approvals.")

    if not parts:
        for key in reversed(list(result.keys())):
            val = result[key]
            if isinstance(val, dict) and val.get("summary"):
                return str(val["summary"])
        return "Task completed."

    return "\n".join(parts)


async def _persist_to_conversation(task: dict[str, Any], content: str) -> None:
    """Persist a final message to the task's conversation if applicable. Never raises."""
    conv_id = _conversation_id_for(task)
    if not conv_id:
        return
    try:
        await _save_assistant_message(conv_id, content, task)
    except Exception as exc:  # persistence must never break the loop
        logger.warning("Failed to persist message to conversation: %s", exc)


async def _save_assistant_message(conversation_id: str, content: str, task: dict[str, Any]) -> None:
    """Insert the task's final answer as an assistant message in the conversation.

    This is the source of truth — it runs in the agent loop's background task,
    so the result survives even if the chat SSE connection was closed. Any
    artifacts this task produced are linked to the new message so they reload
    on refresh.
    """
    messages = await reflect_table("messages")
    conversations = await reflect_table("conversations")
    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        # Guard: conversation must exist (manual tasks may reference nothing).
        exists = (
            await conn.execute(select(conversations.c.id).where(conversations.c.id == conversation_id))
        ).first()
        if not exists:
            return
        result = await conn.execute(
            insert(messages)
            .values(
                organization_id=task.get("organization_id", settings.org_id),
                region=task.get("region", settings.region),
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                token_count=len(content.split()),
            )
            .returning(messages.c.id)
        )
        message_id = str(result.scalar_one())
        await conn.execute(
            update(conversations)
            .where(conversations.c.id == conversation_id)
            .values(updated_at=datetime.now(timezone.utc))
        )
        # Link this task's artifacts to the message for refresh-time grouping.
        await conn.execute(
            update(artifacts)
            .where(artifacts.c.task_id == task["id"], artifacts.c.message_id.is_(None))
            .values(message_id=message_id)
        )


# ── Activity emission ─────────────────────────────────────────────────────────

def activity_channel(task_id: str) -> str:
    return f"activity:{task_id}"


async def emit_activity(
    task_id: str,
    event: dict[str, Any],
    actor_id: str | None = "chronos",
) -> None:
    payload = {"task_id": task_id, "ts": datetime.now(timezone.utc).isoformat(), **event}
    await audit.log(
        "activity",
        actor_id,
        payload.get("type", "activity"),
        resource_type="tasks",
        resource_id=task_id,
        payload=payload,
    )
    await redis_client.publish(activity_channel(task_id), json.dumps(payload, default=str))


async def publish_activity(task_id: str, event: dict[str, Any]) -> None:
    """Publish a transient activity event to Redis WITHOUT writing audit_log.

    Used for high-frequency heartbeats (e.g. 'thinking') that should drive the
    live UI but must not bloat the append-only audit table.
    """
    payload = {"task_id": task_id, "ts": datetime.now(timezone.utc).isoformat(), **event}
    await redis_client.publish(activity_channel(task_id), json.dumps(payload, default=str))


# ── Message history ───────────────────────────────────────────────────────────

def _agent_system_message() -> dict[str, Any]:
    return {
        "role": "system",
        "content": (
            "You are Chronos running an autonomous enterprise task. "
            "Use the available tools to accomplish the goal. "
            "You may call multiple independent tools in parallel in a single response. "
            "When you have gathered enough information to fully answer the goal, respond with "
            "a clear final answer — do not make unnecessary additional tool calls. "
            "All external actions are governed by the broker; some require human approval. "
            "Be direct and operational."
        ),
    }


def _load_history(task: dict[str, Any]) -> list[dict[str, Any]]:
    state = task.get("agent_state") or {}
    if isinstance(state, dict):
        history = state.get("agent_history") or []
        if isinstance(history, list) and history:
            return list(history)
    # Fresh start
    return [
        _agent_system_message(),
        {"role": "user", "content": str(task["goal"])},
    ]


async def _checkpoint(
    task_id: str, history: list[dict[str, Any]], iteration: int, *, model: str | None = None, **extra: Any
) -> None:
    state: dict[str, Any] = {"agent_history": history, "iteration_count": iteration}
    if model:
        state["model"] = model  # preserve the UI-chosen model across resume
    await save_task(task_id, agent_state=state, iteration_count=iteration, **extra)


# ── Context budgeting (Category 7) ──────────────────────────────────────────────

async def _summarize(text: str) -> str:
    """Summarize old task turns with the fast model for compaction."""
    return await complete_text(
        "Summarize this autonomous-task history into 4-6 sentences. Preserve key "
        "facts discovered, decisions made, tool results, and anything needed to "
        "continue the task. Be concrete:\n\n" + text,
        model=settings.fast_model,
    )


# Never let the computed history budget collapse to ~0 (e.g. a tiny configured
# window or an oversized tool schema), which would make compaction fire every
# iteration to no effect. Floor it so compaction stays meaningful.
_MIN_HISTORY_BUDGET = 8_000


def _history_budget(tools: list[dict[str, Any]]) -> int:
    """Tokens available for message history, after reserving for the response and tool schemas."""
    tool_tokens = estimate_tokens(json.dumps(tools, default=str))
    budget = settings.max_context_tokens - settings.response_reserve_tokens - tool_tokens
    return max(budget, _MIN_HISTORY_BUDGET)


async def _maybe_compact(
    task_id: str,
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compact history if it would overflow the model window before the next call.

    Turn-aware (preserves tool_call/tool_result pairing). Emits an activity event
    when compaction actually fires so the timeline reflects it.
    """
    budget = _history_budget(tools)
    if estimate_messages_tokens(history) <= budget:
        return history
    before = len(history)
    compacted = await compact_agent_history(history, budget_tokens=budget, summarizer=_summarize)
    if len(compacted) != before:
        await emit_activity(task_id, {
            "type": "context_compacted",
            "summary": f"Compacted context: {before} → {len(compacted)} messages to stay within the model window.",
        })
    return compacted


# ── LLM step ──────────────────────────────────────────────────────────────────

async def _llm_step(
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Call the LLM and return (final_text | None, list_of_tool_calls).

    Returns:
        final_text: The text response if the LLM is done calling tools.
        tool_calls: Normalised list of tool call dicts if the LLM wants to act.
    """
    kwargs = model_kwargs(model, messages=history, stream=False)
    kwargs["tools"] = tools
    kwargs["tool_choice"] = "auto"
    response = await _with_retry(lambda: litellm.acompletion(**kwargs))

    msg = _message(response)
    raw_calls = _tool_calls(msg)
    text = _message_content(response)

    if raw_calls:
        return None, _normalise_calls(raw_calls)
    return (text or ""), []


def _normalise_calls(raw_calls: list[Any]) -> list[dict[str, Any]]:
    """Normalise litellm tool_call objects into plain dicts."""
    result: list[dict[str, Any]] = []
    for i, call in enumerate(raw_calls):
        if isinstance(call, dict):
            fn = call.get("function") or {}
            result.append({
                "id": call.get("id") or f"call_{i}",
                "name": fn.get("name") or "",
                "args_str": fn.get("arguments") or "{}",
            })
        else:
            result.append({
                "id": getattr(call, "id", f"call_{i}"),
                "name": call.function.name,
                "args_str": call.function.arguments or "{}",
            })
    return result


def _serialise_assistant(raw_calls: list[dict[str, Any]], text: str | None) -> dict[str, Any]:
    """Build a JSON-serialisable assistant message with tool_calls."""
    msg: dict[str, Any] = {"role": "assistant", "content": text or ""}
    if raw_calls:
        msg["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {"name": c["name"], "arguments": c["args_str"]},
            }
            for c in raw_calls
        ]
    return msg


def _parse_args(args_str: str | dict) -> dict[str, Any]:
    if isinstance(args_str, dict):
        return args_str
    try:
        parsed = json.loads(args_str)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


# Substrings that mark a key as credential-shaped (api_key, access_token,
# client_secret, …). Matched anywhere in the lowercased key name.
_SECRET_FRAGMENTS = ("password", "token", "secret", "key", "credential", "auth", "bearer")
# Large opaque fields we omit from the audit preview for noise, not secrecy.
_BULKY_KEYS = {"body", "content", "code"}


def _args_preview(args: dict[str, Any]) -> dict[str, Any]:
    """Redact credential-shaped and bulky args before they reach the audit log.

    vault_ref is the one key safe (and required) to log — it is a reference, not
    a credential.
    """
    preview: dict[str, Any] = {}
    for key, value in args.items():
        lowered = key.lower()
        if lowered == "vault_ref":
            preview[key] = value
        elif lowered in _BULKY_KEYS or any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
            preview[key] = "[omitted]"
        else:
            preview[key] = value
    return preview


# ── Tool execution ────────────────────────────────────────────────────────────

# Safety net: cap any single tool result so one giant fetch/read can't blow the
# context window on its own. Generous (~12k tokens) — routine results pass
# untouched; only outliers are trimmed. Stale turns are handled by compaction.
_MAX_TOOL_RESULT_CHARS = 48_000


def _tool_message(call_id: str, tool_name: str, content: str) -> dict[str, Any]:
    """Build a tool-role history message, truncating oversized content.

    Keeps the head and tail (the most useful parts of most payloads) and marks
    the elision so the model knows the result was trimmed and can re-fetch.
    """
    if len(content) > _MAX_TOOL_RESULT_CHARS:
        head = content[: _MAX_TOOL_RESULT_CHARS // 2]
        tail = content[-(_MAX_TOOL_RESULT_CHARS // 4):]
        dropped = len(content) - len(head) - len(tail)
        content = f"{head}\n\n…[{dropped} characters truncated — re-fetch if you need the full result]…\n\n{tail}"
    return {"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": content}


async def _execute_tool(
    call: dict[str, Any],
    task: dict[str, Any],
    agent: AgentContext,
) -> dict[str, Any]:
    """Execute one tool call.  Returns a tool-role message for the history.

    Raises ApprovalRequired if the broker gates the call.
    """
    tool_name = call["name"]
    args = _parse_args(call["args_str"])
    task_id = task["id"]
    depth = int(task.get("depth") or 0)

    await emit_activity(task_id, {
        "type": "tool_call",
        "tool": tool_name,
        "args_preview": _args_preview(args),
    })

    # spawn__subagent is handled here, not via the broker
    if tool_name == _SUBAGENT_TOOL_NAME:
        try:
            result_data = await _run_subagent(task, args, depth)
            content = json.dumps(result_data, default=str)
            await emit_activity(task_id, {
                "type": "tool_result",
                "tool": tool_name,
                "summary": f"Sub-agent finished: {str(args.get('goal', ''))[:60]}",
            })
        except Exception as exc:
            logger.warning("Sub-agent failed: %s", exc)
            content = json.dumps({"error": str(exc)})
            await emit_activity(task_id, {"type": "tool_error", "tool": tool_name, "error": str(exc)})
        return _tool_message(call["id"], tool_name, content)

    # All other tools go through the ToolBroker
    broker_name = to_broker_name(tool_name)
    try:
        result = await tool_broker.execute(agent, broker_name, args)
        payload = {"summary": result.summary, "data": result.data}
        content = json.dumps(payload, default=str)
        await emit_activity(task_id, {
            "type": "tool_result",
            "tool": tool_name,
            "summary": result.summary,
        })
        # A file write of a renderable type becomes a user-facing artifact.
        if tool_name == "fs__write":
            artifact = await _maybe_create_artifact(task, args)
            if artifact:
                await emit_activity(task_id, {"type": "artifact", **artifact})
    except ApprovalRequired:
        raise  # propagate — caller decides whether to gate
    except Exception as exc:
        logger.warning("Tool %s error: %s", tool_name, exc)
        content = json.dumps({"error": str(exc)})
        await emit_activity(task_id, {"type": "tool_error", "tool": tool_name, "error": str(exc)})

    return _tool_message(call["id"], tool_name, content)


# Renderable file extensions → (artifact kind, mime type).
_RENDERABLE_EXT: dict[str, tuple[str, str]] = {
    ".html": ("html", "text/html"),
    ".htm": ("html", "text/html"),
    ".md": ("markdown", "text/markdown"),
    ".markdown": ("markdown", "text/markdown"),
    ".css": ("code", "text/css"),
    ".js": ("code", "text/javascript"),
    ".json": ("data", "application/json"),
    ".csv": ("data", "text/csv"),
    ".svg": ("image", "image/svg+xml"),
    ".py": ("code", "text/plain"),
    ".txt": ("text", "text/plain"),
}


async def _maybe_create_artifact(task: dict[str, Any], args: dict[str, Any]) -> dict[str, Any] | None:
    """Persist a renderable fs__write as a downloadable/openable artifact.

    Returns an event payload {artifact_id, title, kind, mime_type, size_bytes}
    for the activity stream, or None if the file is not a renderable type.
    """
    import os

    path = str(args.get("path") or "")
    file_content = args.get("content")
    if not path or not isinstance(file_content, str):
        return None
    ext = os.path.splitext(path)[1].lower()
    mapping = _RENDERABLE_EXT.get(ext)
    if not mapping:
        return None
    kind, mime = mapping

    from core.artifacts import save_artifact

    try:
        artifact_id = await save_artifact(
            file_content,
            kind=kind,
            title=os.path.basename(path),
            conversation_id=_conversation_id_for(task),
            task_id=task["id"],
            org_id=task.get("organization_id", settings.org_id),
            region=task.get("region", settings.region),
            mime_type=mime,
        )
    except Exception as exc:
        logger.warning("Artifact creation failed for %s: %s", path, exc)
        return None

    return {
        "artifact_id": artifact_id,
        "title": os.path.basename(path),
        "kind": kind,
        "mime_type": mime,
        "size_bytes": len(file_content.encode("utf-8")),
    }


async def _run_subagent(
    parent_task: dict[str, Any],
    args: dict[str, Any],
    parent_depth: int,
) -> dict[str, Any]:
    """Create a child task and run a nested agent loop for it."""
    if parent_depth >= MAX_DEPTH:
        raise ValueError(f"Sub-agent depth limit ({MAX_DEPTH}) reached — cannot spawn further.")

    goal = str(args.get("goal") or "No goal specified")
    model_tier = str(args.get("model") or "agent")
    child_model = settings.agent_model if model_tier == "agent" else settings.fast_model

    tasks = await reflect_table("tasks")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(tasks)
            .values(
                organization_id=parent_task["organization_id"],
                region=parent_task.get("region", settings.region),
                parent_task_id=parent_task["id"],
                triggered_by=f"task:{parent_task['id']}",
                triggered_by_member_id=parent_task.get("triggered_by_member_id"),
                status="running",
                goal=goal,
                plan={},
                agent_state={"agent_history": [], "iteration_count": 0},
                current_step=0,
                result={},
                depth=parent_depth + 1,
                started_at=now,
            )
            .returning(tasks.c.id)
        )
        child_id = str(result.scalar_one())

    child_task = await get_task(child_id)
    if not child_task:
        raise RuntimeError("Failed to create child task")

    await emit_activity(parent_task["id"], {
        "type": "sub_agent_spawned",
        "sub_task_id": child_id,
        "goal": goal,
    })

    final_result = await run_loop(child_task, tools=SUBAGENT_TOOLS, model=child_model)

    await emit_activity(parent_task["id"], {
        "type": "sub_agent_complete",
        "sub_task_id": child_id,
        "result": final_result,
    })
    return final_result


# ── Approval gating ───────────────────────────────────────────────────────────

def _needs_approval(tool_name: str) -> bool:
    return tool_name in ALWAYS_APPROVAL_TOOL_NAMES


async def _open_approval_gate(
    task: dict[str, Any],
    pending_calls: list[dict[str, Any]],
    history: list[dict[str, Any]],
    iteration: int,
    model: str | None = None,
) -> None:
    """Persist state and create approval records; set task to awaiting_approval."""
    task_id = task["id"]
    approvals = await reflect_table("approvals")
    now = datetime.now(timezone.utc)
    approval_ids: list[str] = []

    async with engine.begin() as conn:
        for call in pending_calls:
            broker_name = to_broker_name(call["name"])
            args = _parse_args(call["args_str"])
            payload = {
                "tool": broker_name,
                "args": args,
                "call_id": call["id"],
                "agent_loop": True,
                "justification": f"Chronos requested permission to call {broker_name}.",
            }
            row = await conn.execute(
                insert(approvals)
                .values(
                    organization_id=task["organization_id"],
                    region=task.get("region", settings.region),
                    task_id=task_id,
                    step_id="agent_loop",
                    action_type=broker_name,
                    action_payload=payload,
                    expires_at=now + timedelta(hours=24),
                )
                .returning(approvals.c.id)
            )
            approval_ids.append(str(row.scalar_one()))

    state: dict[str, Any] = {
        "agent_history": history,
        "iteration_count": iteration,
        "pending_approval_calls": pending_calls,
    }
    if model:
        state["model"] = model
    await save_task(task_id, agent_state=state, iteration_count=iteration, status="awaiting_approval")
    await emit_activity(task_id, {
        "type": "awaiting_approval",
        "approval_ids": approval_ids,
        "step_id": "agent_loop",
    })


# ── Resume after approval ─────────────────────────────────────────────────────

async def resume_after_approval(task_id: str) -> None:
    """Resume a loop that paused for approval.  Called by the approvals router."""
    task = await get_task(task_id)
    if not task:
        return
    if task["status"] not in {"awaiting_approval", "paused"}:
        return

    state = task.get("agent_state") or {}
    history = state.get("agent_history") or _load_history(task)
    iteration = int(state.get("iteration_count") or 0)
    pending_calls: list[dict[str, Any]] = state.get("pending_approval_calls") or []
    agent = AgentContext.from_task(task)

    # Load approval rows
    approvals = await reflect_table("approvals")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(approvals).where(
                    approvals.c.task_id == task_id,
                    approvals.c.step_id == "agent_loop",
                )
            )
        ).mappings().all()
    rows = [dict(r) for r in rows]

    pending = [r for r in rows if r["status"] == "pending"]
    if pending:
        return  # still waiting on a human

    # Process each approval decision
    for row in rows:
        payload = dict(row.get("action_payload") or {})
        if payload.get("execution_result") or payload.get("execution_error") or payload.get("rejection_result"):
            continue  # already processed

        broker_name = payload.get("tool") or row["action_type"]
        call_id = payload.get("call_id") or row["id"]
        args = payload.get("args") or {}

        if row["status"] == "rejected":
            content = json.dumps({"status": "rejected", "reason": row.get("decision_note")})
            history.append({"role": "tool", "tool_call_id": call_id, "name": broker_name, "content": content})
            await _mark_approval(row["id"], {**payload, "rejection_result": True}, approvals)
            await emit_activity(task_id, {"type": "approval_rejected", "approval_id": row["id"]})
            continue

        # approved → execute
        try:
            result = await tool_broker.execute(agent, broker_name, {**args, "__approved_by_gate": True})
            result_data = {"summary": result.summary, "data": result.data}
            content = json.dumps(result_data, default=str)
            await _mark_approval(row["id"], {**payload, "execution_result": result_data}, approvals)
            await emit_activity(task_id, {"type": "tool_result", "tool": broker_name, "summary": result.summary})
        except Exception as exc:
            content = json.dumps({"error": str(exc)})
            await _mark_approval(row["id"], {**payload, "execution_error": str(exc)}, approvals)
            await emit_activity(task_id, {"type": "tool_error", "tool": broker_name, "error": str(exc)})

        history.append({"role": "tool", "tool_call_id": call_id, "name": broker_name, "content": content})

    # Clear pending state and continue (preserve the UI-chosen model).
    new_state: dict[str, Any] = {"agent_history": history, "iteration_count": iteration}
    chosen_model = state.get("model")
    if chosen_model:
        new_state["model"] = chosen_model
    await save_task(task_id, agent_state=new_state, status="running")
    refreshed = await get_task(task_id)
    if refreshed:
        await run_loop(refreshed)


async def _mark_approval(approval_id: str, payload: dict[str, Any], approvals_table: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(approvals_table).where(approvals_table.c.id == approval_id).values(action_payload=payload)
        )


# ── Core loop ─────────────────────────────────────────────────────────────────

async def run_loop(
    task: dict[str, Any],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Native agent loop.  Runs until the LLM delivers a final text answer
    or MAX_ITERATIONS is exceeded.  Returns the final result dict.

    - tools: defaults to ALL_TOOLS (sub-agents pass SUBAGENT_TOOLS)
    - model: defaults to settings.agent_model
    """
    task_id = task["id"]
    effective_tools = tools if tools is not None else ALL_TOOLS
    # Precedence: explicit arg (sub-agents) > model chosen in the UI (stored in
    # agent_state) > default agent model.
    stored_model = (task.get("agent_state") or {}).get("model") if isinstance(task.get("agent_state"), dict) else None
    effective_model = model or stored_model or settings.agent_model
    agent = AgentContext.from_task(task)

    history = _load_history(task)
    iteration = int(task.get("iteration_count") or 0)

    await save_task(task_id, status="running",
                    started_at=task.get("started_at") or datetime.now(timezone.utc))

    while iteration < MAX_ITERATIONS:
        # ── Context budgeting (Category 7) ──────────────────────────────────
        # Compact before the call so a long-running task never overflows the
        # model window. The compacted list becomes the new ground truth so a
        # restart resumes from the trimmed history, not the unbounded original.
        history = await _maybe_compact(task_id, history, effective_tools)

        # ── Ask the LLM ────────────────────────────────────────────────────
        # Heartbeat: the completion is non-streaming and can take many seconds
        # (e.g. generating a whole file in tool args). Tell the UI we're working
        # so it shows "Thinking…" instead of a frozen caret. Publish-only — this
        # high-frequency signal must not bloat the append-only audit_log.
        await publish_activity(task_id, {"type": "thinking"})
        try:
            final_text, calls = await _llm_step(history, effective_tools, effective_model)
        except Exception as exc:
            logger.error("LLM error in agent loop for task %s: %s", task_id, exc)
            await save_task(task_id, status="failed", error=str(exc))
            await _persist_to_conversation(task, f"The task stopped due to a model error: {exc}")
            await emit_activity(task_id, {"type": "task_failed", "error": f"LLM error: {exc}"})
            return {"error": str(exc)}

        # ── Final answer ────────────────────────────────────────────────────
        if not calls:
            result = {"answer": final_text or ""}
            history.append({"role": "assistant", "content": final_text or ""})
            await _checkpoint(task_id, history, iteration + 1, model=effective_model,
                              status="complete", result=result,
                              completed_at=datetime.now(timezone.utc))
            # Persist the answer to the conversation (source of truth, survives
            # SSE disconnects). Only top-level conversation-triggered tasks.
            await _persist_to_conversation(task, format_task_answer(result))
            await emit_activity(task_id, {"type": "task_complete", "result": result})
            return result

        iteration += 1

        # Append assistant message with tool_calls to history
        history.append(_serialise_assistant(calls, None))

        # ── Check for always-approval tools ────────────────────────────────
        # If the batch mixes always-approval tools with normal ones, execute the
        # normal siblings now and append their results, then gate only what needs
        # approval. Otherwise their tool_calls would be orphaned on resume, since
        # resume_after_approval() only appends results for approval rows.
        approval_needed = [c for c in calls if _needs_approval(c["name"])]
        if approval_needed:
            siblings = [c for c in calls if not _needs_approval(c["name"])]
            gate_calls = list(approval_needed)
            if siblings:
                raw_results = await asyncio.gather(
                    *[_execute_tool(c, task, agent) for c in siblings],
                    return_exceptions=True,
                )
                for call, r in zip(siblings, raw_results):
                    if isinstance(r, ApprovalRequired):
                        gate_calls.append(call)  # broker-gated sibling joins the gate
                    elif isinstance(r, Exception):
                        history.append({
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["name"],
                            "content": json.dumps({"error": str(r)}),
                        })
                    else:
                        history.append(r)
            await _open_approval_gate(task, gate_calls, history, iteration, model=effective_model)
            return {"status": "awaiting_approval"}

        # ── Execute tool calls (parallel if multiple) ───────────────────────
        if len(calls) == 1:
            try:
                tool_msg = await _execute_tool(calls[0], task, agent)
            except ApprovalRequired:
                await _open_approval_gate(task, calls, history, iteration, model=effective_model)
                return {"status": "awaiting_approval"}
            history.append(tool_msg)
        else:
            # Parallel execution — gather all results concurrently
            raw_results = await asyncio.gather(
                *[_execute_tool(c, task, agent) for c in calls],
                return_exceptions=True,
            )
            gated_calls = [c for c, r in zip(calls, raw_results) if isinstance(r, ApprovalRequired)]
            # Persist results for calls that already completed BEFORE gating, so
            # ungated siblings are never discarded and re-run (duplicate side
            # effects) on resume. Pair each by its own call id.
            for c, r in zip(calls, raw_results):
                if isinstance(r, ApprovalRequired):
                    continue
                if isinstance(r, Exception):
                    history.append({
                        "role": "tool",
                        "tool_call_id": c["id"],
                        "name": c["name"],
                        "content": json.dumps({"error": str(r)}),
                    })
                else:
                    history.append(r)
            if gated_calls:
                await _open_approval_gate(task, gated_calls, history, iteration, model=effective_model)
                return {"status": "awaiting_approval"}

        # Persist after every iteration
        await _checkpoint(task_id, history, iteration, model=effective_model, current_step=iteration)

    # Max iterations exceeded
    error = "max_iterations_exceeded"
    await save_task(task_id, status="failed", error=error, completed_at=datetime.now(timezone.utc))
    await _persist_to_conversation(
        task, "The task ran for the maximum number of steps without finishing. "
        "Try narrowing the goal or breaking it into smaller requests."
    )
    await emit_activity(task_id, {"type": "task_failed", "error": error})
    return {"error": error}
