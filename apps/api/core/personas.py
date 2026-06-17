from __future__ import annotations

from sqlalchemy import select

from core.db import engine, reflect_table


_PERSONAS: dict[str, dict[str, str]] = {
    "default": {
        "name": "Chronos",
        "prompt": "You are Chronos, a precise operational AI employee that keeps work auditable and asks for approval before external side effects.",
    },
    "sdr": {
        "name": "SDR Assistant",
        "prompt": "You research accounts against an ICP, qualify leads, and draft personalized outreach. Never send messages without explicit approval.",
    },
    "sdr-outreach": {
        "name": "SDR Assistant",
        "prompt": "You research leads, identify sales hiring signals, score fit, and write concise personalized cold email drafts for approval.",
    },
    "research": {
        "name": "Research Analyst",
        "prompt": "You gather source-grounded research, separate facts from assumptions, and summarize evidence clearly.",
    },
}


async def get_persona_prompt(persona_id: str | None) -> str:
    if not persona_id:
        return ""
    try:
        profiles = await reflect_table("agent_profiles")
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(profiles).where(
                        profiles.c.id == persona_id,
                        profiles.c.status != "deleted",
                    )
                )
            ).mappings().first()
        if row:
            data = dict(row)
            kind = data.get("profile_kind") or "agent"
            parts = [
                f"You are {data.get('name')}, a Chronos {kind}.",
                f"Role: {data.get('role')}.",
            ]
            if data.get("personality"):
                parts.append(f"Personality: {data['personality']}")
            if data.get("instructions"):
                parts.append(f"Instructions: {data['instructions']}")
            if kind == "assistant":
                parts.append("You primarily shape the conversation: answer, reason, and ask clarifying questions. Do not start background work unless the user asks or the task clearly requires it.")
            else:
                parts.append("You are an executable worker: use granted tools and workflows when the task requires action, and respect approval policy before external side effects.")
            if data.get("tool_grants"):
                parts.append(f"Allowed tools: {', '.join(data['tool_grants'])}.")
            if data.get("workflows"):
                parts.append(f"Target workflows: {', '.join(data['workflows'])}.")
            if data.get("approval_policy"):
                parts.append(f"Approval policy: {data['approval_policy']}.")
            return "\n".join(parts)
    except Exception:
        # Keep chat resilient if profile storage is unavailable during startup or tests.
        pass
    persona = _PERSONAS.get(persona_id)
    return persona["prompt"] if persona else ""
