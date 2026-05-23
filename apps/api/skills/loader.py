from __future__ import annotations

import json
import re
from pathlib import Path

from core.config import settings
from core.llm import complete_json
from skills.registry import SKILLS_ROOT, load_skill_index


def _score_skill(message: str, skill: dict) -> int:
    haystack = f"{skill.get('name', '')} {skill.get('description', '')}".lower()
    terms = set(re.findall(r"[a-z0-9]+", haystack))
    tokens = set(re.findall(r"[a-z0-9]+", message.lower()))
    return len(tokens & terms)


async def _connectors_available(required: list[str]) -> dict[str, bool]:
    """Return {connector: is_live} for each required connector."""
    if not required:
        return {}
    try:
        from core.connector_health import check_connectors
        health = await check_connectors()
        return {c: health.get(c, {}).get("tier") == "live" for c in required}
    except Exception:
        return {c: False for c in required}


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
        parsed = json.loads(await complete_json(prompt, model=settings.fast_model))
        selected = parsed.get("relevant_skill_ids", [])
        known = {skill["id"] for skill in index}
        return [sid for sid in selected if sid in known][:top_k]
    except Exception:
        ranked = sorted(index, key=lambda skill: _score_skill(message, skill), reverse=True)
        return [skill["id"] for skill in ranked if _score_skill(message, skill) > 0][:top_k]


async def load_skill_content(skill_id: str, *, progressive: bool = True) -> str:
    """Load skill content.

    Args:
        skill_id: Skill directory name under skills/.
        progressive: When True (default), inject only SKILL.md + a file index so the
            agent can request detail files via fs.read. When False, inline everything
            (used for short skills or when the agent explicitly requests full content).
    """
    skill_dir = SKILLS_ROOT / skill_id
    if not skill_dir.exists() or not skill_dir.is_dir():
        return ""

    parts: list[str] = []

    # Always load SKILL.md first (the canonical summary/instructions).
    skill_md = skill_dir / "SKILL.md"
    if skill_md.exists():
        parts.append(skill_md.read_text())
    else:
        # Fall back to any .md file if SKILL.md is missing.
        for path in sorted(skill_dir.glob("*.md")):
            parts.append(path.read_text())
        return "\n\n".join(parts)

    # Collect auxiliary files (non-SKILL.md).
    aux_files = [p for p in sorted(skill_dir.iterdir()) if p.name != "SKILL.md" and p.name != "metadata.json" and not p.name.startswith(".")]

    if progressive and aux_files:
        # Progressive disclosure: list available files so the agent can fetch them on demand.
        file_list = "\n".join(f"- {p.name}" for p in aux_files)
        parts.append(
            f"\n## Additional skill files (use fs.read to load when needed)\n{file_list}"
        )
    else:
        # Inline all aux .md files; skip binary files.
        for path in aux_files:
            if path.suffix in {".md", ".txt", ".yaml", ".yml", ".json"}:
                try:
                    parts.append(f"### {path.name}\n{path.read_text()}")
                except Exception:
                    pass

    return "\n\n".join(parts)


async def skill_connector_warning(skill: dict) -> str | None:
    """Return a human-readable warning if required connectors are missing, else None."""
    required: list[str] = skill.get("requires_connectors") or []
    if not required:
        return None
    availability = await _connectors_available(required)
    missing = [c for c, ok in availability.items() if not ok]
    if not missing:
        return None
    joined = ", ".join(missing)
    return (
        f"⚠️ Skill '{skill['name']}' needs {joined} connected to work fully. "
        f"Go to Settings → Connectors to set up {joined}."
    )
