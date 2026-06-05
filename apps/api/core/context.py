from __future__ import annotations

import base64
from pathlib import Path
import asyncio

from sqlalchemy import select, text

from core import memory
from core.config import settings
from core.db import engine, reflect_table
from core.models import RequesterContext
from core.personas import get_persona_prompt
from core.tool_manifest import generate_tool_manifest
from memory.source_retrieval import (
    build_knowledge_block,
    citations_payload,
    retrieve_source_chunks,
)
from skills.loader import find_relevant_skills, load_skill_content, skill_connector_warning
from skills.registry import load_skill_index

ROOT = Path(__file__).resolve().parents[3]

# Category 7: rough token estimation (4 chars ≈ 1 token for English prose).
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _estimate_tokens_from_chars(char_count: int) -> int:
    return max(1, char_count // _CHARS_PER_TOKEN)


def load_base_system_prompt() -> str:
    prompt_path = Path(__file__).resolve().parent / "prompts" / "base_system_prompt.md"
    return prompt_path.read_text(encoding="utf-8")


async def load_org_context(org_id: str) -> str:
    context_dir = ROOT / "context" / org_id
    if not context_dir.exists():
        return ""
    parts: list[str] = []
    for path in sorted(context_dir.glob("*.md")):
        parts.append(f"## {path.name}\n{path.read_text()}")
    return "\n\n".join(parts)


async def _compact_history(
    conversation_id: str,
    *,
    budget_tokens: int,
    verbatim_turns: int = 6,
) -> list[dict[str, str]]:
    """Load conversation history, compacting oldest messages if they exceed budget.

    Always keeps the most recent `verbatim_turns` pairs verbatim.
    Summarizes older turns into a single synthetic 'assistant' entry.
    """
    messages_table = await reflect_table("messages")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(messages_table.c.role, messages_table.c.content)
                .where(messages_table.c.conversation_id == conversation_id)
                .order_by(messages_table.c.created_at.desc())
                .limit(200)  # hard ceiling; compaction handles the rest
            )
        ).mappings().all()

    all_messages = [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    if not all_messages:
        return []

    # Always keep the last verbatim_turns messages verbatim.
    verbatim = all_messages[-verbatim_turns:] if len(all_messages) > verbatim_turns else all_messages
    older = all_messages[: len(all_messages) - len(verbatim)]

    # If everything fits in budget, return as-is.
    total_chars = sum(len(m["content"]) for m in all_messages)
    if _estimate_tokens_from_chars(total_chars) <= budget_tokens or not older:
        return all_messages

    # Summarize the older block using the fast model.
    try:
        from core.llm import complete_text

        older_text = "\n".join(f"{m['role'].upper()}: {m['content'][:300]}" for m in older)
        summary_text = await complete_text(
            f"Summarize this conversation history in 3-5 sentences, preserving key facts and decisions:\n\n{older_text}",
            model=settings.fast_model,
        )
        history: list[dict[str, str]] = [
            {"role": "assistant", "content": f"[Earlier conversation summary]: {summary_text}"}
        ]
    except Exception:
        # If summarization fails, just drop the oldest messages.
        history = []

    return history + verbatim


async def assemble_context(
    conversation_id: str,
    message: str,
    requester_context: RequesterContext,
) -> list[dict[str, str]]:
    # ── Category 7: establish token budget ──────────────────────────────────
    budget = settings.max_context_tokens - settings.response_reserve_tokens
    # Reserve half the budget for history; the system layers get the other half.
    system_budget = budget // 2
    history_budget = budget - system_budget

    # ── Concurrent fetch phase ──────────────────────────────────────────────
    # Every layer's data source is independent, but the string assembly below has
    # sequential, budget-gated dependencies (each layer's fit check depends on the
    # running `base` length). So fetch all the slow I/O concurrently here, then
    # assemble synchronously with results in hand. This makes time-to-first-token
    # ≈ max(fetch latencies) instead of their sum — the two LLM round-trips
    # (skills relevance + memory embed) and the DB/file reads now overlap.
    # Capture the sub-agent flag before overwriting memory_context: the tool
    # manifest depends on the *incoming* context (sub-agents get a different
    # manifest), whereas memory.retrieve wants the resolved task/chat value.
    is_sub_agent = requester_context.memory_context == "sub_agent"
    requester_context.memory_context = "task" if requester_context.task_id else "chat"

    async def _fetch_skills() -> list[tuple[str, str | None, str]]:
        skill_ids = await find_relevant_skills(message)
        skill_index = {s["id"]: s for s in load_skill_index()}
        out: list[tuple[str, str | None, str]] = []
        for skill_id in skill_ids:
            skill_meta = skill_index.get(skill_id, {})
            warning = await skill_connector_warning(skill_meta)
            content = await load_skill_content(skill_id, progressive=True)
            out.append((skill_id, warning, content))
        return out

    async def _fetch_memory() -> list:
        try:
            return await asyncio.wait_for(
                memory.retrieve(message, requester_context),
                timeout=settings.memory_retrieve_timeout_seconds,
            )
        except (Exception, asyncio.TimeoutError):
            return []

    async def _fetch_project_instructions() -> str | None:
        if requester_context.project_id is None:
            return None
        return await _load_project_instructions(
            requester_context.project_id, requester_context.org_id
        )

    async def _fetch_citations() -> list | None:
        # Returns None when not applicable (no project) so the assembler can skip
        # touching surfaced_citations, vs [] meaning "applicable but nothing found".
        if requester_context.project_id is None:
            return None
        try:
            return await retrieve_source_chunks(message, requester_context)
        except Exception:
            return []

    (
        org_context,
        tool_manifest,
        persona_prompt,
        project_instructions,
        skills_data,
        memories,
        citations,
        history,
    ) = await asyncio.gather(
        load_org_context(requester_context.org_id),
        generate_tool_manifest(
            persona_id=requester_context.persona_id,
            org_id=requester_context.org_id,
            sub_agent=is_sub_agent,
        ),
        get_persona_prompt(requester_context.persona_id),
        _fetch_project_instructions(),
        _fetch_skills(),
        _fetch_memory(),
        _fetch_citations(),
        _compact_history(conversation_id, budget_tokens=history_budget),
    )

    # ── Assembly phase (sequential, budget-gated — order preserved) ─────────
    # ── Layer 1: base system prompt ─────────────────────────────────────────
    base = load_base_system_prompt()

    # ── Layer 2: org context ────────────────────────────────────────────────
    if org_context and _estimate_tokens(base + org_context) <= system_budget:
        base += f"\n\n# Organization Context\n{org_context}"

    # ── Layer 2b: dynamic tool manifest ────────────────────────────────────
    if tool_manifest and _estimate_tokens(base + tool_manifest) <= system_budget:
        base += f"\n\n{tool_manifest}"

    # ── Layer 3: persona ────────────────────────────────────────────────────
    if persona_prompt and _estimate_tokens(base + persona_prompt) <= system_budget:
        base += f"\n\n# Your Identity\n{persona_prompt}"

    # ── Layer 3b: project instructions ─────────────────────────────────────
    if project_instructions and _estimate_tokens(base + project_instructions) <= system_budget:
        base += f"\n\n# Project Instructions\n{project_instructions}"

    # ── Layer 4: skills (Category 6: connector-aware, progressive) ──────────
    for skill_id, warning, content in skills_data:
        if content and _estimate_tokens(base + content) <= system_budget:
            base += f"\n\n# Skill: {skill_id}\n{content}"
            if warning:
                base += f"\n\n{warning}"
        elif warning:
            # Even if the skill doesn't fit, show the setup prompt.
            base += f"\n\n{warning}"

    # ── Layer 5: memory ─────────────────────────────────────────────────────
    if memories:
        mem_block = "\n".join(f"- {m.content}" for m in memories)
        if _estimate_tokens(base + mem_block) <= system_budget:
            base += "\n\n# What I Remember\n" + mem_block
            requester_context.surfaced_memory_refs = [
                {
                    "id": getattr(m, "id", None),
                    "content": getattr(m, "content", ""),
                    "scope": getattr(m, "scope", None),
                    "source": getattr(m, "source", None),
                    "importance_score": getattr(m, "importance_score", None),
                }
                for m in memories
            ]
        else:
            requester_context.surfaced_memory_refs = []
    else:
        requester_context.surfaced_memory_refs = []

    # ── Layer 5b: project knowledge (permission-aware source citations) ─────
    if requester_context.project_id is not None:
        if citations:
            knowledge_block = build_knowledge_block(citations)
            if knowledge_block and _estimate_tokens(base + knowledge_block) <= system_budget:
                base += f"\n\n{knowledge_block}"
                requester_context.surfaced_citations = citations_payload(citations)
            else:
                requester_context.surfaced_citations = []
        else:
            requester_context.surfaced_citations = []

    # ── Layer 6: task state ─────────────────────────────────────────────────
    if requester_context.task_id:
        task_context = await _load_task_context(
            requester_context.task_id, requester_context.org_id
        )
        if task_context:
            base += f"\n\n# Current Task\n{task_context}"

    # ── Layer 7: conversation history (with compaction) ─────────────────────
    if history and history[-1].get("role") == "user" and history[-1].get("content") == message:
        history = history[:-1]

    return [{"role": "system", "content": base}, *history, {"role": "user", "content": message}]


async def _load_project_instructions(project_id: str, org_id: str) -> str | None:
    """Return the project's instructions text, or None if absent / not in caller's org.

    Defense-in-depth: filters on BOTH id AND organization_id so a caller cannot
    obtain instructions from a project in a different tenant, even if they supply
    the correct project UUID.
    """
    try:
        projects = await reflect_table("projects")
    except Exception:
        return None
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(projects.c.instructions).where(
                    projects.c.id == project_id,
                    projects.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    if row is None:
        return None
    instructions = row["instructions"]
    if not instructions or not instructions.strip():
        return None
    return instructions


_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})

# Vision-unavailable note appended when images are present but the selected model
# cannot process them natively — truthful, observable in context/response.
_VISION_UNAVAILABLE_NOTE = (
    "[Note: image attachment(s) present but the selected model does not support "
    "vision — OCR text extraction was used instead. Switch to a vision-capable "
    "model (e.g. GPT-5.4 Mini) to send images directly.]"
)


def build_user_turn_content(
    message: str,
    image_blocks: list[dict],
    *,
    vision_available: bool,
    ocr_text: str | None = None,
    ocr_note: str | None = None,
) -> str | list:
    """Return the content value for the final user-turn message dict.

    Two shapes:
    - vision_available=True and image_blocks non-empty:
        Returns a list: [text_block, image_url_block, ...].
        litellm forwards list-content to providers that support it (OpenAI vision API).
    - Otherwise:
        Returns a plain string (message + optional OCR-extracted text + optional note).
        When images are attached but vision is unavailable, the OCR-extracted text is
        embedded directly in the user turn so the model actually receives the image
        content — this makes the accompanying ``ocr_note`` truthful rather than a bare
        claim. Keeps the path byte-for-byte identical to the pre-vision code for all
        non-image turns (no ocr_text/ocr_note).

    This function is **pure** (no I/O) so it is fast-path safe and trivially testable.

    Design note: the helper lives here (not in chat.py) because it is part of
    context assembly — it decides the final user-turn payload.  The chat router
    calls it after resolving org-scoped image attachment bytes and calls
    stream_chat_turn with the result via the `user_content` param.
    """
    if vision_available and image_blocks:
        blocks: list[dict] = [{"type": "text", "text": message}]
        blocks.extend(image_blocks)
        return blocks

    # Non-vision path: plain text, with the OCR-extracted image text and an honest
    # note appended when present. Order: user message → extracted text → note.
    parts = [message]
    if ocr_text:
        parts.append(ocr_text)
    if ocr_note:
        parts.append(ocr_note)
    return "\n\n".join(parts) if len(parts) > 1 else message


def build_image_block(image_bytes: bytes, mime: str) -> dict:
    """Return a single OpenAI-style image_url content block from raw bytes."""
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
    return {"type": "image_url", "image_url": {"url": data_url}}


async def _load_task_context(task_id: str, org_id: str) -> str:
    try:
        tasks = await reflect_table("tasks")
    except Exception:
        return ""
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(tasks).where(
                    tasks.c.id == task_id,
                    tasks.c.organization_id == org_id,
                )
            )
        ).mappings().first()
    if not row:
        return ""
    task = dict(row)
    plan = task.get("plan") or []
    if isinstance(plan, dict):
        plan = plan.get("steps", [])
    step_count = len(plan) if isinstance(plan, list) else 0
    current_step = int(task.get("current_step") or 0)
    return (
        f"Goal: {task.get('goal')}\n"
        f"Status: {task.get('status')}\n"
        f"Step: {min(current_step + 1, step_count) if step_count else current_step}/{step_count}"
    )
