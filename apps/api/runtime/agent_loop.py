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
from core.llm import _message_content, _message, _tool_calls, _with_retry, default_chat_model_id, model_kwargs, resolve_agent_model, stream_step
from core.models import AgentContext
from core.redis import redis_client
from core.task_envelope import build_task_envelope, envelope_to_agent_prompt
from core.tool_manifest import generate_tool_manifest
from runtime.tool_registry import (
    ALL_TOOLS,
    ALWAYS_APPROVAL_TOOL_NAMES,
    INLINE_CHAT_TOOLS,
    SUBAGENT_TOOLS,
    _START_TASK_TOOL_NAME,
    _SUBAGENT_TOOL_NAME,
    to_broker_name,
)
from runtime import task_runner
from memory.extraction import extract_and_save

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 40
MAX_DEPTH = 3
DURABLE_TRACE_TYPES = {
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

# Strong references to fire-and-forget background tasks so they are not
# garbage-collected mid-flight; failures are logged rather than lost.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _spawn_background(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("Background task failed: %s", t.exception())

    task.add_done_callback(_on_done)


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


def collect_tool_summaries(history: list[dict[str, Any]]) -> list[str]:
    """Extract one summary string per tool result in the loop history.

    Used to derive runtime action verbs (drafted/sent/updated) for the
    structured response. Reads the 'summary' field the broker writes into
    tool messages; falls back to the tool name.
    """
    out: list[str] = []
    for msg in history:
        if msg.get("role") != "tool":
            continue
        name = str(msg.get("name") or "tool")
        summary = ""
        content = msg.get("content")
        if isinstance(content, str):
            try:
                summary = str(json.loads(content).get("summary") or "")
            except Exception:
                summary = ""
        out.append(f"{name}: {summary}".strip())
    return out


async def _persist_to_conversation(
    task: dict[str, Any],
    content: str,
    *,
    structured_response: dict | None = None,
    citations: list | None = None,
    tool_traces: list | None = None,
    memory_refs: list | None = None,
) -> str | None:
    """Persist a final message to the task's conversation if applicable. Never raises.

    Returns the inserted message id on success, or None if the task has no
    conversation or persistence fails.
    """
    conv_id = _conversation_id_for(task)
    if not conv_id:
        return None
    try:
        return await _save_assistant_message(
            conv_id,
            content,
            task,
            structured_response=structured_response,
            citations=citations,
            tool_traces=tool_traces,
            memory_refs=memory_refs,
        )
    except Exception as exc:  # persistence must never break the loop
        logger.warning("Failed to persist message to conversation: %s", exc)
        return None


async def _save_assistant_message(
    conversation_id: str,
    content: str,
    task: dict[str, Any],
    *,
    mode: str | None = None,
    structured_response: dict | None = None,
    citations: list | None = None,
    tool_traces: list | None = None,
    memory_refs: list | None = None,
) -> str | None:
    """Insert the task's final answer as an assistant message in the conversation.

    This is the source of truth — it runs in the agent loop's background task,
    so the result survives even if the chat SSE connection was closed. Any
    artifacts this task produced are linked to the new message so they reload
    on refresh.

    Returns the newly inserted message id (str), or None if the conversation
    does not exist.
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
            return None
        result = await conn.execute(
            insert(messages)
            .values(
                organization_id=task.get("organization_id", "default"),
                region=task.get("region", "us"),
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                token_count=len(content.split()),
                mode=mode,
                structured_response=structured_response,
                citations=citations,
                tool_traces=tool_traces,
                memory_refs=memory_refs,
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
    return message_id


# ── Activity emission ─────────────────────────────────────────────────────────

def activity_channel(task_id: str) -> str:
    return f"activity:{task_id}"


async def _task_org(task_id: str) -> str:
    """Resolve a task's tenant for audit. The task row owns the org.

    If the task can't be loaded we must not attribute the activity to a real
    tenant. Defaulting to the process org would hide the entry from the true
    tenant and pollute the default org's audit log; raising would crash the
    durable-trace path (telemetry must stay resilient). Instead return a
    synthetic, non-colliding marker so the anomaly is visible and monitorable."""
    task = await get_task(task_id)
    org_id = (task or {}).get("organization_id")
    return str(org_id) if org_id else f"unresolved_task:{task_id}"


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
        organization_id=await _task_org(task_id),
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
            organization_id=await _task_org(task_id),
            resource_type="tasks",
            resource_id=task_id,
            payload=payload,
        )
    await redis_client.publish(activity_channel(task_id), json.dumps(payload, default=str))


# ── Message history ───────────────────────────────────────────────────────────

async def _agent_system_message(tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    current_date = datetime.now(timezone.utc).date().isoformat()
    is_sub_agent = tools == SUBAGENT_TOOLS
    manifest = await generate_tool_manifest(sub_agent=is_sub_agent)
    orchestration_guidance = (
        "For large jobs with independent workstreams, spawn all useful sub-agents in the same "
        "assistant step so the tool calls run in parallel. Do not spawn one sub-agent, wait for it, "
        "then spawn the next if the roles are already known. Give each sub-agent a specific role "
        "inside its goal, and once their results return, synthesize the final answer directly.\n\n"
    )
    sub_agent_guidance = (
        "You are a bounded sub-agent. Optimize for speed and useful output. Use at most 2-4 model "
        "iterations unless blocked. Prefer browser__search result snippets for breadth, fetch only "
        "the 1-3 most valuable pages, and do not keep retrying after rate limits or sparse pages. "
        "If a connector rate-limits or a fetch returns little content, synthesize from the evidence "
        "already gathered and say what was limited. Return a concise, structured report for the "
        "parent to use.\n\n"
    )
    return {
        "role": "system",
        "content": (
            "You are Chronos, an enterprise AI assistant. Answer quick questions "
            "directly and conversationally. Use tools only when they genuinely help: "
            "use browser__search for the latest / current / recent / time-sensitive facts "
            f"(Current date: {current_date}) instead of relying on model memory; "
            "read or write files, draft emails. You may call multiple independent tools in "
            "parallel. Do not narrate tool use you are not doing, and stop calling tools once "
            "you can answer. "
            "If a request is a large, multi-step, long-running job (deep research, batch "
            "outreach, anything spanning many steps or sub-agents), call start_task to run it "
            "as a durable background task instead of doing it all inline. "
            "All external actions are governed by the broker; some require human approval.\n\n"
            f"{sub_agent_guidance if is_sub_agent else orchestration_guidance}"
            "CRITICAL RULE — Honesty about search results:\n"
            "- If a search returns 0 results, say so. Do not fabricate statistics, sources, or data.\n"
            "- If a tool result contains `is_fallback: true` or a `warning` field, the live search failed. "
            "Report this honestly. Do not present placeholder/fixture data as real.\n"
            "- If you cannot find real data, say \"I could not find that information\" rather than inventing it.\n\n"
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


def _format_attachments_context(attachments: list[dict[str, Any]]) -> str:
    """Render parsed attachment previews as one immutable seed block."""
    lines = ["# Attached files", "The user attached these files. Their parsed text follows."]
    for a in attachments:
        name = str(a.get("filename") or "file")
        artifact_id = str(a.get("parsed_artifact_id") or a.get("attachment_id") or "")
        note = a.get("note")
        header = f"\n## {name}"
        if a.get("truncated"):
            header += f"  (truncated — use doc__read with artifact_id={artifact_id} for more)"
        if note:
            header += f"  [{note}]"
        lines.append(header)
        lines.append(str(a.get("preview") or ""))
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
    attachments = state.get("attachments") if isinstance(state, dict) else None
    if isinstance(attachments, list) and attachments:
        seed.append({"role": "user", "content": _format_attachments_context(attachments)})
    project_knowledge = state.get("project_knowledge") if isinstance(state, dict) else None
    if isinstance(project_knowledge, str) and project_knowledge.strip():
        seed.append({"role": "user", "content": project_knowledge})
    envelope_data = state.get("task_envelope") if isinstance(state, dict) else None
    if isinstance(envelope_data, dict):
        try:
            from core.models import TaskEnvelope

            seed.append({"role": "user", "content": envelope_to_agent_prompt(TaskEnvelope(**envelope_data))})
            return seed
        except Exception:
            logger.warning("Invalid task envelope for task %s; falling back to raw message", task.get("id"))

    original_message = state.get("original_user_message") if isinstance(state, dict) else None
    if isinstance(original_message, str) and original_message.strip():
        goal = str(task["goal"])
        envelope = build_task_envelope(
            task_id=str(task.get("id") or ""),
            raw_user_message=original_message.strip(),
            ui_title=goal,
            router_decision={"mode": "agent", "ui_title": goal},
            attachments=attachments if isinstance(attachments, list) else [],
        )
        seed.append({"role": "user", "content": envelope_to_agent_prompt(envelope)})
    else:
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
    if not path:
        return None
    ext = os.path.splitext(path)[1].lower()
    mapping = _RENDERABLE_EXT.get(ext)
    if not mapping:
        return None
    kind, mime = mapping
    if not isinstance(file_content, str):
        try:
            from connectors.filesystem import WORKSPACE_ROOT, _jailed_path

            root = (WORKSPACE_ROOT / str(task.get("organization_id") or "default") / str(task["id"])).resolve()
            candidate = _jailed_path(root, path)
            if not candidate.is_file():
                return None
            file_content = candidate.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Artifact recovery failed for %s: %r", path, exc)
            return None

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
        logger.warning("Artifact creation failed for %s: %r", path, exc)
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
    model_id = str(args.get("model") or default_chat_model_id())
    try:
        child_model = resolve_agent_model(model_id)
    except ValueError:
        child_model = resolve_agent_model(default_chat_model_id())

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


# ── Inline chat turn ──────────────────────────────────────────────────────────

async def persist_assistant_message(
    conversation_id: str,
    content: str,
    requester_context: Any,
    *,
    mode: str | None = None,
    citations: list | None = None,
    tool_traces: list | None = None,
    memory_refs: list | None = None,
) -> None:
    """Save an inline chat turn's final answer as an assistant message. Never raises."""
    try:
        await _save_assistant_message(
            conversation_id,
            content,
            {"id": None, "organization_id": requester_context.org_id, "region": "us"},
            mode=mode,
            citations=citations,
            tool_traces=tool_traces,
            memory_refs=memory_refs,
        )
    except Exception as exc:  # persistence must never break the turn
        logger.warning("Failed to persist inline assistant message: %s", exc)


def _normalize_chat_tool_traces(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pending: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    seq = 0

    def next_id(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}-{seq}"

    for event in raw:
        etype = str(event.get("type") or "")
        if etype in {"thinking", "reasoning_summary"}:
            continue
        if etype == "tool_call":
            tool = str(event.get("tool") or "tool")
            pending[tool] = {
                "id": next_id("tool"),
                "tool": tool,
                "summary": str(event.get("summary") or ""),
                "status": "pending",
            }
        elif etype in {"tool_result", "tool_error"}:
            tool = str(event.get("tool") or "tool")
            row = pending.pop(tool, None) or {
                "id": next_id("tool"),
                "tool": tool,
                "summary": "",
                "status": "pending",
            }
            if etype == "tool_result":
                row["summary"] = str(event.get("summary") or row.get("summary") or "")
                row["status"] = "complete"
            else:
                row["summary"] = str(event.get("error") or event.get("summary") or row.get("summary") or "")
                row["status"] = "error"
            out.append(row)
        elif event.get("tool"):
            out.append({
                "id": next_id("trace"),
                "tool": str(event.get("tool")),
                "summary": str(event.get("summary") or ""),
                "status": str(event.get("status") or "complete"),
            })

    out.extend(pending.values())
    return out


async def create_task_from_history(
    *,
    goal: str,
    history: list[dict[str, Any]],
    requester_context: Any,
    conversation_id: str,
    model: str | None,
    status: str = "running",
) -> str:
    """Create a tasks row seeded with the full in-flight history (not a bare goal).

    Used for lazy persistence on the first inline tool call and for start_task
    promotion. The stored agent_history is what resume/approval rebuilds from.
    """
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(tasks)
            .values(
                organization_id=requester_context.org_id,
                region="us",
                persona_id=requester_context.persona_id,
                workspace_id=requester_context.workspace_id,
                triggered_by=conversation_id,
                triggered_by_member_id=requester_context.member_id,
                status=status,
                goal=goal,
                plan={},
                agent_state={
                    "agent_history": history,
                    "iteration_count": 0,
                    "model": resolve_agent_model(model),
                },
                current_step=0,
                result={},
                depth=0,
            )
            .returning(tasks.c.id)
        )
        return str(result.scalar_one())


def should_emit_fast_ack(message: str) -> bool:
    """Return true for turns likely to need tools, retrieval, or durable execution."""
    text = message.lower()
    long_or_complex = len(text.split()) >= 18
    triggers = (
        "research", "analyze", "analyse", "compare", "extract", "contract",
        "liability", "draft", "presentation", "slide", "deck", "artifact",
        "file", "document", "sub-agent", "subagent", "agent", "latest",
        "current", "today", "find", "search", "generate", "build", "create",
    )
    return long_or_complex or any(trigger in text for trigger in triggers)


def fast_ack_text(message: str) -> str:
    """Lightweight visible response sent before planning, retrieval, or tools."""
    text = message.lower()
    if "contract" in text or "liability" in text:
        return "I'll start by checking the contract, extracting the key terms, and comparing the relevant changes."
    if "presentation" in text or "slide" in text or "deck" in text:
        return "I'll start by scoping the deck, gathering the research threads, and tracking the presentation artifact as it is built."
    if "research" in text or "latest" in text or "current" in text or "today" in text:
        return "I'll start by gathering the relevant sources, checking what needs live lookup, and keeping progress visible as I work."
    if "file" in text or "document" in text or "extract" in text:
        return "I'll start by reading the material, extracting the important parts, and then turning that into the result you asked for."
    return "I'll start by checking the request, gathering the needed context, and keeping the work visible as it runs."


def requires_mailbox_grounding(message: str) -> bool:
    text = message.lower()
    if not any(marker in text for marker in ("email", "emails", "gmail", "inbox", "mailbox")):
        return False
    if any(marker in text for marker in ("draft an email", "write an email", "compose an email")):
        return False
    return any(
        marker in text
        for marker in (
            "summarize", "summarise", "summary", "search", "find", "look",
            "what", "which", "who", "last", "recent", "today", "yesterday",
            "days", "week", "inbox", "received", "came in",
        )
    )


async def stream_chat_turn(
    *,
    conversation_id: str,
    message: str,
    context_messages: list[dict[str, Any]],
    requester_context: Any,
    model: str | None,
    mode: str | None = None,
    emit_conversation: bool = True,
    user_content: Any | None = None,
):
    """Stream one chat turn inline.

    Yields SSE-ready event dicts: conversation / token / trace / artifact /
    task_created / awaiting_approval / done. Creates a tasks row lazily on the
    first tool call (with full history). Promotes to a durable background task
    when the model calls start_task.
    """
    if emit_conversation:
        yield {"type": "conversation", "conversation_id": conversation_id}

    history: list[dict[str, Any]] = list(context_messages)
    if requires_mailbox_grounding(message):
        history.append(
            {
                "role": "system",
                "content": (
                    "Controller requirement: this user request asks about mailbox contents. "
                    "Before giving any factual summary, count, sender, subject, or existence claim, "
                    "call gmail__search and use only the returned Gmail threads/messages as evidence. "
                    "If the Gmail tool errors, say the search failed. If result_count is 0 or no threads "
                    "are returned, say no matching emails were found. Do not invent emails, senders, "
                    "subjects, counts, or dates."
                ),
            }
        )
    # user_content may be a list (multimodal vision blocks) or a plain string.
    # message (always str) is kept for ack/grounding/memory/goal — user_content
    # only controls what the model receives as the user turn's content field.
    history.append({"role": "user", "content": user_content if user_content is not None else message})
    effective_model = resolve_agent_model(model)
    ack_prefix = ""
    if should_emit_fast_ack(message):
        ack_prefix = f"{fast_ack_text(message)}\n\n"
        yield {"type": "token", "content": ack_prefix}
    task_id: str | None = None
    task: dict[str, Any] | None = None
    iteration = 0
    raw_tool_trace_events: list[dict[str, Any]] = []
    surfaced_citations = list(getattr(requester_context, "surfaced_citations", []) or [])
    surfaced_memory_refs = list(getattr(requester_context, "surfaced_memory_refs", []) or [])

    async def save_inline_answer(content: str) -> None:
        await persist_assistant_message(
            conversation_id,
            content,
            requester_context,
            mode=mode,
            citations=surfaced_citations,
            tool_traces=_normalize_chat_tool_traces(raw_tool_trace_events),
            memory_refs=surfaced_memory_refs,
        )

    while iteration < MAX_ITERATIONS:
        iteration += 1
        final_text: str | None = None
        calls: list[dict[str, Any]] = []
        try:
            async for ev in stream_step(history, INLINE_CHAT_TOOLS, effective_model):
                if ev["type"] == "token":
                    yield {"type": "token", "content": ev["content"]}
                elif ev["type"] == "text_done":
                    final_text = ev["text"]
                elif ev["type"] == "tool_calls":
                    calls = ev["calls"]
        except Exception as exc:
            logger.error("Inline turn model error: %s", exc)
            msg = "Sorry — I hit a model error and couldn't finish that. Please try again."
            await save_inline_answer(msg)
            yield {"type": "token", "content": msg}
            yield {"type": "done"}
            return

        if not calls:
            answer = f"{ack_prefix}{final_text or ''}"
            await save_inline_answer(answer)
            _spawn_background(extract_and_save(conversation_id, message, answer, requester_context))
            yield {"type": "done"}
            return

        promote = next((c for c in calls if c["name"] == _START_TASK_TOOL_NAME), None)
        if promote:
            goal = _parse_args(promote["args_str"]).get("goal") or message
            bg_id = await create_task_from_history(
                goal=goal, history=history, requester_context=requester_context,
                conversation_id=conversation_id, model=model, status="queued",
            )
            await task_runner.enqueue_task(bg_id)
            yield {"type": "task_created", "task_id": bg_id, "background": True}
            yield {"type": "done"}
            return

        history.append(_serialise_assistant(calls, final_text))
        if task_id is None:
            task_id = await create_task_from_history(
                goal=message, history=history, requester_context=requester_context,
                conversation_id=conversation_id, model=model, status="running",
            )
            task = await get_task(task_id)
            yield {"type": "task_created", "task_id": task_id, "background": False}

        agent = AgentContext.from_task(task)

        approval_needed = [c for c in calls if _needs_approval(c["name"])]
        # Mirror run_loop's escalation: once untrusted (prompt-injected) content is in
        # history, any external write must be gated even if it isn't always-approval.
        if _history_has_prompt_injection(history):
            approval_needed.extend(
                c for c in calls if _call_is_external_write(c) and c not in approval_needed
            )
        if approval_needed:
            await _open_approval_gate(task, approval_needed, history, iteration, model=effective_model)
            yield {"type": "awaiting_approval", "task_id": task_id}
            yield {"type": "done"}
            return

        for call in calls:
            tool_call_event = {
                "type": "tool_call",
                "tool": call["name"],
                "args_preview": _args_preview(_parse_args(call["args_str"])),
            }
            raw_tool_trace_events.append(tool_call_event)
            yield {"type": "trace", "event": tool_call_event}
            try:
                tool_msg = await _execute_tool(call, task, agent)
            except ApprovalRequired:
                await _open_approval_gate(task, [call], history, iteration, model=effective_model)
                yield {"type": "awaiting_approval", "task_id": task_id}
                yield {"type": "done"}
                return
            history.append(tool_msg)
            summary = ""
            try:
                summary = json.loads(tool_msg["content"]).get("summary", "")
            except Exception:
                pass
            tool_result_event = {"type": "tool_result", "tool": call["name"], "summary": summary}
            raw_tool_trace_events.append(tool_result_event)
            yield {"type": "trace", "event": tool_result_event}

        await _checkpoint(task_id, history, iteration, model=effective_model, current_step=iteration)

    msg = "This is taking many steps. Try narrowing the request, or ask me to run it as a background task."
    await save_inline_answer(msg)
    yield {"type": "token", "content": msg}
    yield {"type": "done"}


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
            final_text, calls = await _llm_step(history, effective_tools, effective_model)
        except Exception as exc:
            logger.error("LLM error in agent loop for task %s: %s", task_id, exc)
            await save_task(task_id, status="failed", error=str(exc))
            failed_msg_id = await _persist_to_conversation(task, f"The task stopped due to a model error: {exc}")
            failed_event: dict[str, Any] = {"type": "task_failed", "error": f"LLM error: {exc}"}
            if failed_msg_id:
                failed_event["message_id"] = failed_msg_id
            await emit_activity(task_id, failed_event)
            return {"error": str(exc)}

        # ── Final answer ────────────────────────────────────────────────────
        if not calls:
            await publish_activity(task_id, {
                "type": "model_result",
                "iteration": iteration + 1,
                "summary": "No tool call needed; preparing final answer.",
            })
            result = {"answer": final_text or ""}
            history.append({"role": "assistant", "content": final_text or ""})
            await _checkpoint(task_id, history, iteration + 1, model=effective_model,
                              status="complete", result=result,
                              completed_at=datetime.now(timezone.utc))
            # Build the structured response envelope from runtime facts + prose composer.
            # Failures are silently suppressed so the loop never breaks on this path.
            from core.structured_response import (
                build_runtime_facts, compose, resolve_verbosity, derive_response_type,
            )
            from core.models import RequesterContext

            answer_text = format_task_answer(result)
            try:
                approvals = await reflect_table("approvals")
                async with engine.begin() as conn:
                    approval_exists = (
                        await conn.execute(
                            select(approvals.c.id).where(approvals.c.task_id == task_id).limit(1)
                        )
                    ).first() is not None
                facts = build_runtime_facts(
                    result=result,
                    task_status="complete",
                    tool_summaries=collect_tool_summaries(history),
                    approval_exists=approval_exists,
                )
                triggered_member = task.get("triggered_by_member_id") or "system"
                verbosity = await resolve_verbosity(
                    RequesterContext(org_id=task.get("organization_id", "default"),
                                     member_id=str(triggered_member), role="user")
                )
                envelope = await compose(
                    response_type=derive_response_type(facts), answer_text=answer_text,
                    facts=facts, verbosity=verbosity,
                )
                envelope_dict = envelope.model_dump()
            except Exception:
                envelope_dict = None

            # Persist the answer to the conversation (source of truth, survives
            # SSE disconnects). Only top-level conversation-triggered tasks.
            message_id = await _persist_to_conversation(task, answer_text, structured_response=envelope_dict)
            event: dict[str, Any] = {"type": "task_complete", "result": result}
            if envelope_dict is not None:
                event["structured_response"] = envelope_dict
            if message_id:
                event["message_id"] = message_id
            await emit_activity(task_id, event)
            return result

        iteration += 1

        # Append assistant message with tool_calls to history
        history.append(_serialise_assistant(calls, None))

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
    maxiter_msg_id = await _persist_to_conversation(
        task, "The task ran for the maximum number of steps without finishing. "
        "Try narrowing the goal or breaking it into smaller requests."
    )
    maxiter_event: dict[str, Any] = {"type": "task_failed", "error": error}
    if maxiter_msg_id:
        maxiter_event["message_id"] = maxiter_msg_id
    await emit_activity(task_id, maxiter_event)
    return {"error": error}
