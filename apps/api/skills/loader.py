from __future__ import annotations

import json
import re
from pathlib import Path

from core.config import settings
from core.llm import complete_json
from skills.registry import SKILLS_ROOT, get_candidate_skills, get_skill_db


def _score_skill(message: str, skill: dict) -> int:
    haystack = f"{skill.get('name', '')} {skill.get('description', '')}".lower()
    terms = set(re.findall(r"[a-z0-9]+", haystack))
    tokens = set(re.findall(r"[a-z0-9]+", message.lower()))
    return len(tokens & terms)


async def _connectors_available(required: list[str], org_id: str = "default") -> dict[str, bool]:
    """Return {connector: is_available} for each required connector.

    A connector counts as available when the org actually connected it
    (active connector row — the Anthropic model) or when its runtime family
    is live without a per-user connection (browser, fs, code, …).
    """
    if not required:
        return {}
    connected: set[str] = set()
    try:
        from core.connector_tools import connected_providers
        connected = set((await connected_providers(org_id)).keys())
    except Exception:
        connected = set()
    try:
        from core.connector_health import check_connectors
        health = await check_connectors()
        return {
            c: c in connected or health.get(c, {}).get("tier") == "live"
            for c in required
        }
    except Exception:
        return {c: c in connected for c in required}


async def find_relevant_skills(
    message: str, org_id: str = "default", top_k: int = 2
) -> list[str]:
    index = await get_candidate_skills(org_id)
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


async def load_skill_content(
    skill_id: str, *, progressive: bool = True, org_id: str = "default"
) -> str:
    """Load skill content.

    Args:
        skill_id: Skill slug (directory name under skills/, or DB skill slug).
        progressive: When True (default), inject only SKILL.md + a file index so the
            agent can request detail files via fs.read. When False, inline everything
            (used for short skills or when the agent explicitly requests full content).
        org_id: Tenant scope. DB-persisted skills are looked up under this org first;
            filesystem skills are the fallback.
    """
    # DB-persisted/uploaded skills take precedence over the filesystem seed. They
    # carry no bundled aux files, so the SKILL.md content is rendered directly.
    try:
        db_skill = await get_skill_db(org_id, skill_id)
    except Exception:
        db_skill = None
    if db_skill and db_skill.get("content"):
        return db_skill["content"]

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
        text_files = [p for p in aux_files if p.suffix not in {".py", ".sh"}]
        exec_files = [p for p in aux_files if p.suffix in {".py", ".sh"}]

        sections: list[str] = []
        if text_files:
            file_list = "\n".join(f"- {p.name}" for p in text_files)
            sections.append(f"## Additional skill files (use fs.read to load when needed)\n{file_list}")
        if exec_files:
            script_list = "\n".join(f"- {p.name}" for p in exec_files)
            sections.append(
                f"## Executable scripts (invoke via `skill.run_script`)\n{script_list}"
            )
        if sections:
            parts.append("\n" + "\n\n".join(sections))
    else:
        # Inline all aux .md files; skip binary files.
        for path in aux_files:
            if path.suffix in {".md", ".txt", ".yaml", ".yml", ".json"}:
                try:
                    parts.append(f"### {path.name}\n{path.read_text()}")
                except Exception:
                    pass

    return "\n\n".join(parts)


async def build_agent_skills_block(goal: str, org_id: str = "default", top_k: int = 2) -> str:
    """Assemble a progressive-disclosure skills block for a durable task seed.

    Mirrors how chat assembles skills so the agent loop is no longer skill-blind:
      - Level 1: the full catalog of available skills (name + description), always
        cheap, so the agent knows what exists.
      - Level 2: the SKILL.md body of the skills most relevant to the goal, inlined.
    Returns an empty string when no skills are available.
    """
    candidates = await get_candidate_skills(org_id)
    if not candidates:
        return ""

    lines = [
        "# Skills",
        "These skills are available. Follow a matching skill's instructions when the "
        "task fits its description. Run any bundled scripts with the `skill.run_script` tool.",
        "",
        "## Available skills",
    ]
    for skill in candidates:
        lines.append(f"- **{skill['name']}** ({skill['id']}): {skill.get('description', '')}")

    relevant_ids = await find_relevant_skills(goal, org_id, top_k=top_k)
    index = {s["id"]: s for s in candidates}
    loaded: list[str] = []
    for skill_id in relevant_ids:
        content = await load_skill_content(skill_id, progressive=True, org_id=org_id)
        if not content.strip():
            continue
        name = index.get(skill_id, {}).get("name", skill_id)
        warning = await skill_connector_warning(index.get(skill_id, {}), org_id=org_id)
        if warning:
            content = f"{warning}\n\n{content}"
        loaded.append(f"## Skill: {name}\n{content}")

    if loaded:
        lines.append("")
        lines.append("## Loaded skill instructions (relevant to this task)")
        lines.append("")
        lines.append("\n\n".join(loaded))

    return "\n".join(lines)


async def skill_connector_warning(skill: dict, org_id: str = "default") -> str | None:
    """Return a human-readable warning if required connectors are missing, else None."""
    required: list[str] = skill.get("requires_connectors") or []
    if not required:
        return None
    availability = await _connectors_available(required, org_id=org_id)
    missing = [c for c, ok in availability.items() if not ok]
    if not missing:
        return None
    joined = ", ".join(missing)
    return (
        f"⚠️ Skill '{skill['name']}' needs {joined} connected to work fully. "
        f"Go to Settings → Connectors to set up {joined}."
    )
