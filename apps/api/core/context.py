from pathlib import Path
import asyncio

from sqlalchemy import select

from core import memory
from core.config import settings
from core.db import engine, reflect_table
from core.models import RequesterContext
from core.personas import get_persona_prompt
from skills.loader import find_relevant_skills, load_skill_content

ROOT = Path(__file__).resolve().parents[3]


def load_base_system_prompt() -> str:
    return (
        "You are Chronos by Cognisia, an autonomous enterprise AI agent. "
        "In Phase 1 you answer through the skeleton chat system and preserve auditability."
    )


async def load_org_context(org_id: str) -> str:
    context_dir = ROOT / "context" / org_id
    if not context_dir.exists():
        return ""
    parts: list[str] = []
    for path in sorted(context_dir.glob("*.md")):
        parts.append(f"## {path.name}\n{path.read_text()}")
    return "\n\n".join(parts)


async def assemble_context(
    conversation_id: str,
    message: str,
    requester_context: RequesterContext,
) -> list[dict[str, str]]:
    base = load_base_system_prompt()
    org_context = await load_org_context(requester_context.org_id)
    if org_context:
        base += f"\n\n# Organization Context\n{org_context}"

    persona_prompt = await get_persona_prompt(requester_context.persona_id)
    if persona_prompt:
        base += f"\n\n# Your Identity\n{persona_prompt}"

    skill_ids = await find_relevant_skills(message)
    for skill_id in skill_ids:
        content = await load_skill_content(skill_id)
        if content:
            base += f"\n\n# Skill: {skill_id}\n{content}"

    try:
        memories = await asyncio.wait_for(
            memory.retrieve(message, requester_context),
            timeout=settings.memory_retrieve_timeout_seconds,
        )
    except (Exception, asyncio.TimeoutError):
        memories = []
    if memories:
        base += "\n\n# What I Remember\n" + "\n".join(f"- {m.content}" for m in memories)

    if requester_context.task_id:
        task_context = await _load_task_context(requester_context.task_id)
        if task_context:
            base += f"\n\n# Current Task\n{task_context}"

    history: list[dict[str, str]] = []
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(messages.c.role, messages.c.content)
                .where(messages.c.conversation_id == conversation_id)
                .order_by(messages.c.created_at.desc())
                .limit(20)
            )
        ).mappings().all()
    for row in reversed(rows):
        history.append({"role": row["role"], "content": row["content"]})

    return [{"role": "system", "content": base}, *history, {"role": "user", "content": message}]


async def _load_task_context(task_id: str) -> str:
    try:
        tasks = await reflect_table("tasks")
    except Exception:
        return ""
    async with engine.begin() as conn:
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().first()
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
