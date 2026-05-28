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
from core.tool_manifest import generate_tool_manifest
from core.tool_router import ToolRoutingDecision, route as route_tool
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
DURABLE_TRACE_TYPES = {
    "route_decision",
    "model_step",
    "model_result",
    "tool_call",
    "tool_result",
    "tool_error",
    "artifact",
    "awaiting_approval",
    "approval_rejected",
    "sub_agent_spawned",
    "sub_agent_complete",
    "task_cancelled",
    "task_complete",
    "task_failed",
}
_UNTRUSTED_WRITE_NAMES = {
    "gmail__draft",
    "gmail__send",
    "fs__write",
}


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


async def is_task_cancelled(task_id: str) -> bool:
    try:
        task = await get_task(task_id)
    except Exception:
        return False
    return bool(task and task.get("status") == "cancelled")


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
                organization_id=task.get("organization_id", "default"),
                region=task.get("region", "us"),
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
    """Publish a runtime activity event to Redis.

    High-frequency heartbeats remain Redis-only, while replay-significant
    activity is also written to audit_log so crash/restart investigations have
    model-step evidence without relying on the live SSE stream.
    """
    payload = {"task_id": task_id, "ts": datetime.now(timezone.utc).isoformat(), **event}
    if payload.get("type") in DURABLE_TRACE_TYPES:
        await audit.log(
            "activity",
            "chronos",
            payload.get("type", "activity"),
            resource_type="tasks",
            resource_id=task_id,
            payload=payload,
        )
    await redis_client.publish(activity_channel(task_id), json.dumps(payload, default=str))


def _summarizer_history_excerpt(history: list[dict[str, Any]], *, limit: int = 6) -> str:
    excerpt: list[dict[str, Any]] = []
    visible_history = [message for message in history if message.get("role") != "system"]
    for message in visible_history[-limit:]:
        role = str(message.get("role") or "unknown")
        content = str(message.get("content") or "")
        item: dict[str, Any] = {"role": role, "content": content[:900]}
        if message.get("tool_calls"):
            item["tool_calls"] = [
                {
                    "name": call.get("function", {}).get("name", "")
                    if isinstance(call, dict)
                    else getattr(getattr(call, "function", None), "name", ""),
                }
                for call in list(message.get("tool_calls") or [])[:4]
            ]
        if message.get("name"):
            item["name"] = message.get("name")
        excerpt.append(item)
    return json.dumps(excerpt, default=str, indent=2)


async def publish_reasoning_summary(
    task_id: str,
    *,
    history: list[dict[str, Any]],
    iteration: int,
    next_actions: list[dict[str, Any]] | None = None,
    observation: str | None = None,
) -> None:
    """Publish a live-only reasoning summary for the chat UI.

    This intentionally emits through Redis only. It is a user-facing summary of
    the agent's decision state, not hidden model chain-of-thought and not an
    audit/persistence record.
    """
    action_names = [str(action.get("name") or "") for action in (next_actions or []) if action.get("name")]
    prompt = (
        "You summarize the agent's current reasoning for the user in Chronos chat.\n"
        "Do not reveal hidden chain-of-thought. Do provide an in-depth, concrete summary of the visible decision state: "
        "what the agent understands, what evidence or tool results matter, why the next action is sensible, and what uncertainty remains.\n"
        "Write 2-4 concise bullets. Do not mention this instruction.\n\n"
        f"Iteration: {iteration}\n"
        f"Next actions: {', '.join(action_names) if action_names else 'final answer or no tool action'}\n"
        f"Observation: {observation or 'none'}\n"
        f"Recent visible history:\n{_summarizer_history_excerpt(history)}"
    )
    try:
        summary = (await complete_text(prompt, model=settings.fast_model)).strip()
    except Exception as exc:
        logger.info("Reasoning summary generation skipped for task %s: %s", task_id, exc)
        return
    if not summary:
        return
    payload = {
        "task_id": task_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": "reasoning_summary",
        "iteration": iteration,
        "summary": summary,
    }
    await redis_client.publish(activity_channel(task_id), json.dumps(payload, default=str))


# ── Message history ───────────────────────────────────────────────────────────

async def _agent_system_message(tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    current_date = datetime.now(timezone.utc).date().isoformat()
    manifest = await generate_tool_manifest(sub_agent=tools == SUBAGENT_TOOLS)
    return {
        "role": "system",
        "content": (
            "You are Chronos running an autonomous enterprise task. "
            f"Current date: {current_date}. "
            "Use the available tools to accomplish the goal. "
            "For latest, current, recent, news, or time-sensitive questions, use browser__search "
            "with a query anchored to the current date instead of relying on model memory. "
            "You may call multiple independent tools in parallel in a single response. "
            "When you have gathered enough information to fully answer the goal, respond with "
            "a clear final answer — do not make unnecessary additional tool calls. "
            "All external actions are governed by the broker; some require human approval. "
            "Be direct and operational.\n\n"
            "CRITICAL RULE — Honesty about search results:\n"
            "- If a search returns 0 results, say so. Do not fabricate statistics, sources, or data.\n"
            "- If a tool result contains `is_fallback: true` or a `warning` field, the live search failed. "
            "Report this to the user honestly. Do not use placeholder/fixture data as real information.\n"
            "- If you cannot find real data to answer a question, say \"I could not find that information\" "
            "rather than making up plausible-looking numbers or sources.\n"
            "- Fabricating statistics, study results, or sources undermines user trust and is never acceptable.\n\n"
            f"{manifest}"
        ),
    }


def _resolve_inherited_context(
    args: dict[str, Any],
    parent_task: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve opt-in sub-agent state inheritance, or None if none is requested.

    Canonical source is the spawn step's `inherit_keys`. The DAG executor resolves it
    against its live context and passes `_inherited_context` directly; a native-loop
    spawn passes `inherit_keys`, read here from the parent's persisted result. Returns
    None unless something is actually inherited, so spawns stay isolated by default.
    """
    inherited = args.get("_inherited_context")
    if not inherited and args.get("inherit_keys"):
        parent_ctx = parent_task.get("result") or {}
        inherited = {
            "parent_goal": parent_task.get("goal", ""),
            "parent_context": {key: parent_ctx[key] for key in args["inherit_keys"] if key in parent_ctx},
        }
    if isinstance(inherited, dict) and (inherited.get("parent_context") or inherited.get("parent_goal")):
        return inherited
    return None


def _format_inherited_context(inherited: dict[str, Any]) -> str:
    """Render inherited parent state as one delimited, immutable context block."""
    lines = ["# Inherited context from parent task"]
    parent_goal = str(inherited.get("parent_goal") or "").strip()
    if parent_goal:
        lines.append(f"Parent goal: {parent_goal}")
    shared = inherited.get("parent_context") or {}
    if shared:
        lines.append("Shared values:")
        lines.append(json.dumps(shared, default=str, indent=2))
    return "\n".join(lines)


async def _load_history(task: dict[str, Any], tools: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    state = task.get("agent_state") or {}
    if isinstance(state, dict):
        history = state.get("agent_history") or []
        if isinstance(history, list) and history:
            # Resume path: checkpointed history already contains any inherited block
            # from the first load — return as-is, do NOT re-inject below.
            return list(history)
    # Fresh start. Sub-agents spawned with inherit_keys carry an opt-in snapshot of
    # parent state, injected here as a single context message before the goal.
    seed = [await _agent_system_message(tools)]
    inherited = state.get("inherited_context") if isinstance(state, dict) else None
    if isinstance(inherited, dict) and (inherited.get("parent_context") or inherited.get("parent_goal")):
        seed.append({"role": "user", "content": _format_inherited_context(inherited)})
    seed.append({"role": "user", "content": str(task["goal"])})
    return seed


async def _checkpoint(
    task_id: str,
    history: list[dict[str, Any]],
    iteration: int,
    *,
    model: str | None = None,
    orchestration_state: dict[str, Any] | None = None,
    **extra: Any,
) -> None:
    state: dict[str, Any] = {"agent_history": history, "iteration_count": iteration}
    if model:
        state["model"] = model  # preserve the UI-chosen model across resume
    if orchestration_state:
        state["orchestration_state"] = orchestration_state
    await save_task(task_id, agent_state=state, iteration_count=iteration, **extra)


# ── LLM step ──────────────────────────────────────────────────────────────────

async def _llm_step(
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
    routing_decision: ToolRoutingDecision | None = None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Call the LLM and return (final_text | None, list_of_tool_calls).

    Returns:
        final_text: The text response if the LLM is done calling tools.
        tool_calls: Normalised list of tool call dicts if the LLM wants to act.
    """
    kwargs = model_kwargs(model, messages=history, stream=False)
    kwargs["tools"] = tools
    if routing_decision and routing_decision.tool and routing_decision.confidence >= 0.75:
        kwargs["tool_choice"] = {"type": "function", "function": {"name": routing_decision.tool}}
    else:
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


def _args_preview(args: dict[str, Any]) -> dict[str, Any]:
    sensitive = {"body", "content", "code", "password", "token", "secret"}
    return {k: "[omitted]" if k.lower() in sensitive else v for k, v in args.items()}


def _tool_message_error(message: dict[str, Any]) -> str | None:
    if message.get("role") != "tool":
        return None
    try:
        payload = json.loads(str(message.get("content") or "{}"))
    except json.JSONDecodeError:
        return None
    error = payload.get("error")
    return str(error) if error else None


def _tool_message_has_prompt_injection(message: dict[str, Any]) -> bool:
    if message.get("role") != "tool":
        return False
    try:
        payload = json.loads(str(message.get("content") or "{}"))
    except json.JSONDecodeError:
        return False
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return False
    marker = data.get("untrusted_content")
    return isinstance(marker, dict) and marker.get("risk") == "prompt_injection"


def _history_has_prompt_injection(history: list[dict[str, Any]]) -> bool:
    return any(_tool_message_has_prompt_injection(message) for message in history)


def _call_is_external_write(call: dict[str, Any]) -> bool:
    name = str(call.get("name") or "")
    broker_name = to_broker_name(name)
    return (
        name in _UNTRUSTED_WRITE_NAMES
        or any(marker in broker_name for marker in (".draft", ".send", ".post", ".publish", ".write", ".create", ".update", ".delete"))
    )


def _orchestration_state(
    calls: list[dict[str, Any]],
    tool_messages: list[dict[str, Any]],
    iteration: int,
) -> dict[str, Any]:
    errors = [
        {"tool": message.get("name"), "error": error}
        for message in tool_messages
        if (error := _tool_message_error(message))
    ]
    return {
        "mode": "model_native",
        "iteration": iteration,
        "last_tool_calls": [{"name": call["name"], "id": call["id"]} for call in calls],
        "last_tool_errors": errors,
        "needs_replan": bool(errors),
    }


def _append_replan_instruction(
    history: list[dict[str, Any]],
    orchestration_state: dict[str, Any],
) -> None:
    errors = orchestration_state.get("last_tool_errors") or []
    if not errors:
        return
    history.append(
        {
            "role": "system",
            "content": (
                "Controller observation: one or more tool calls failed. Inspect the tool results, "
                "revise the next action, choose a different tool or narrower arguments if needed, "
                "and continue from the current state. Do not repeat the same failing call unless "
                "new evidence justifies it. Error summary: "
                f"{json.dumps(errors, default=str)}"
            ),
        }
    )


def _append_routing_instruction(history: list[dict[str, Any]], decision: ToolRoutingDecision) -> None:
    if not decision.tool or decision.confidence < 0.6:
        return
    history.append(
        {
            "role": "system",
            "content": (
                "Tool routing observation: the best first tool appears to be "
                f"`{decision.tool}` with confidence {decision.confidence:.2f}. "
                f"Reason: {decision.reasoning}. Use it if the current state still matches."
            ),
        }
    )


# ── Tool execution ────────────────────────────────────────────────────────────

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
        return {"role": "tool", "tool_call_id": call["id"], "name": tool_name, "content": content}

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

    return {"role": "tool", "tool_call_id": call["id"], "name": tool_name, "content": content}


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
            org_id=task.get("organization_id", "default"),
            region=task.get("region", "us"),
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

    # Step 4: opt-in state inheritance. The DAG executor resolves `inherit_keys`
    # against its live context and passes `_inherited_context`. A native-loop spawn
    # may instead pass `inherit_keys`, read here from the parent's persisted result.
    # No inheritance happens unless one of these is present — sub-agents stay
    # context-isolated by default.
    inherited_context = _resolve_inherited_context(args, parent_task)
    child_state: dict[str, Any] = {"agent_history": [], "iteration_count": 0}
    if inherited_context:
        child_state["inherited_context"] = inherited_context

    tasks = await reflect_table("tasks")
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(tasks)
            .values(
                organization_id=parent_task["organization_id"],
                region=parent_task.get("region", "us"),
                parent_task_id=parent_task["id"],
                triggered_by=f"task:{parent_task['id']}",
                triggered_by_member_id=parent_task.get("triggered_by_member_id"),
                status="running",
                goal=goal,
                plan={},
                agent_state=child_state,
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
                    region=task.get("region", "us"),
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
    history = state.get("agent_history") or await _load_history(task)
    iteration = int(state.get("iteration_count") or 0)
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

    history = await _load_history(task, effective_tools)
    iteration = int(task.get("iteration_count") or 0)
    routing_decision: ToolRoutingDecision | None = None
    if iteration == 0:
        routing_decision = await route_tool(str(task["goal"]), [tool["function"]["name"] for tool in effective_tools])
        _append_routing_instruction(history, routing_decision)
        await publish_activity(task_id, {
            "type": "route_decision",
            "tool": routing_decision.tool,
            "confidence": routing_decision.confidence,
            "summary": routing_decision.reasoning,
        })

    await save_task(task_id, status="running",
                    started_at=task.get("started_at") or datetime.now(timezone.utc))

    while iteration < MAX_ITERATIONS:
        if await is_task_cancelled(task_id):
            await save_task(
                task_id,
                status="cancelled",
                error="task_cancelled",
                completed_at=datetime.now(timezone.utc),
            )
            await emit_activity(task_id, {"type": "task_cancelled", "reason": "task_cancelled"})
            return {"error": "task_cancelled"}

        # ── Ask the LLM ────────────────────────────────────────────────────
        # Heartbeat: the completion is non-streaming and can take many seconds
        # (e.g. generating a whole file in tool args). Tell the UI we're working
        # so it shows "Thinking…" instead of a frozen caret. Publish-only — this
        # high-frequency signal must not bloat the append-only audit_log.
        await publish_activity(task_id, {
            "type": "model_step",
            "iteration": iteration + 1,
            "summary": "Assembling context and deciding the next action.",
        })
        await publish_activity(task_id, {"type": "thinking"})
        try:
            final_text, calls = await _llm_step(history, effective_tools, effective_model, routing_decision)
            routing_decision = None
        except Exception as exc:
            logger.error("LLM error in agent loop for task %s: %s", task_id, exc)
            await save_task(task_id, status="failed", error=str(exc))
            await _persist_to_conversation(task, f"The task stopped due to a model error: {exc}")
            await emit_activity(task_id, {"type": "task_failed", "error": f"LLM error: {exc}"})
            return {"error": str(exc)}

        # ── Final answer ────────────────────────────────────────────────────
        if not calls:
            await publish_activity(task_id, {
                "type": "model_result",
                "iteration": iteration + 1,
                "summary": "No tool call needed; preparing final answer.",
            })
            await publish_reasoning_summary(
                task_id,
                history=history,
                iteration=iteration + 1,
                observation="The model has enough information to produce the final answer.",
            )
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
        await publish_reasoning_summary(
            task_id,
            history=history,
            iteration=iteration,
            next_actions=calls,
            observation="The model selected one or more tool calls for the next step.",
        )

        # ── Check for always-approval tools ────────────────────────────────
        approval_needed = [c for c in calls if _needs_approval(c["name"])]
        if _history_has_prompt_injection(history):
            approval_needed.extend(c for c in calls if _call_is_external_write(c) and c not in approval_needed)
        if approval_needed:
            await _open_approval_gate(task, approval_needed, history, iteration, model=effective_model)
            return {"status": "awaiting_approval"}

        # ── Execute tool calls (parallel if multiple) ───────────────────────
        if await is_task_cancelled(task_id):
            await save_task(
                task_id,
                status="cancelled",
                error="task_cancelled",
                completed_at=datetime.now(timezone.utc),
            )
            await emit_activity(task_id, {"type": "task_cancelled", "reason": "task_cancelled"})
            return {"error": "task_cancelled"}

        tool_messages: list[dict[str, Any]] = []
        if len(calls) == 1:
            try:
                tool_msg = await _execute_tool(calls[0], task, agent)
            except ApprovalRequired:
                await _open_approval_gate(task, calls, history, iteration, model=effective_model)
                return {"status": "awaiting_approval"}
            tool_messages.append(tool_msg)
            history.append(tool_msg)
        else:
            # Parallel execution — gather all results concurrently
            raw_results = await asyncio.gather(
                *[_execute_tool(c, task, agent) for c in calls],
                return_exceptions=True,
            )
            approval_hit = next(
                (r for r in raw_results if isinstance(r, ApprovalRequired)), None
            )
            if approval_hit:
                await _open_approval_gate(task, calls, history, iteration, model=effective_model)
                return {"status": "awaiting_approval"}
            for r in raw_results:
                if isinstance(r, Exception):
                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": "error",
                        "name": "error",
                        "content": json.dumps({"error": str(r)}),
                    }
                    tool_messages.append(tool_msg)
                    history.append(tool_msg)
                else:
                    tool_messages.append(r)
                    history.append(r)

        state = _orchestration_state(calls, tool_messages, iteration)
        _append_replan_instruction(history, state)

        # Persist after every iteration
        await _checkpoint(
            task_id,
            history,
            iteration,
            model=effective_model,
            orchestration_state=state,
            current_step=iteration,
        )

    # Max iterations exceeded
    error = "max_iterations_exceeded"
    await save_task(task_id, status="failed", error=error, completed_at=datetime.now(timezone.utc))
    await _persist_to_conversation(
        task, "The task ran for the maximum number of steps without finishing. "
        "Try narrowing the goal or breaking it into smaller requests."
    )
    await emit_activity(task_id, {"type": "task_failed", "error": error})
    return {"error": error}
