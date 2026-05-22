from __future__ import annotations

import json
import re

from core.llm import complete_json
from skills.registry import SKILLS_ROOT, load_skill_index


def _score_skill(message: str, skill: dict) -> int:
    haystack = f"{skill.get('name', '')} {skill.get('description', '')}".lower()
    terms = set(re.findall(r"[a-z0-9]+", haystack))
    tokens = set(re.findall(r"[a-z0-9]+", message.lower()))
    return len(tokens & terms)


async def find_relevant_skills(message: str, top_k: int = 2) -> list[str]:
    index = load_skill_index()
    if not index:
        return []

    prompt = f"""
Return only JSON: {{"relevant_skill_ids":["id"]}}
Pick at most {top_k} skills that should be loaded for this user message.
Return an empty list when none apply.

Skills:
{json.dumps([{ "id": s["id"], "name": s["name"], "description": s["description"] } for s in index])}

User message:
{message}
"""
    try:
        parsed = json.loads(await complete_json(prompt))
        selected = parsed.get("relevant_skill_ids", [])
        known = {skill["id"] for skill in index}
        return [sid for sid in selected if sid in known][:top_k]
    except Exception:
        ranked = sorted(index, key=lambda skill: _score_skill(message, skill), reverse=True)
        return [skill["id"] for skill in ranked if _score_skill(message, skill) > 0][:top_k]


async def load_skill_content(skill_id: str) -> str:
    skill_dir = SKILLS_ROOT / skill_id
    if not skill_dir.exists() or not skill_dir.is_dir():
        return ""
    parts: list[str] = []
    for path in sorted(skill_dir.glob("*.md")):
        parts.append(path.read_text())
    return "\n\n".join(parts)
