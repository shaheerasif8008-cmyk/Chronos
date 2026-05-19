from pathlib import Path

from sqlalchemy import select

from core import memory
from core.db import engine, reflect_table
from core.models import RequesterContext

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

    memories = await memory.retrieve(message, requester_context)
    if memories:
        base += "\n\n# What I Remember\n" + "\n".join(f"- {m.content}" for m in memories)

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
