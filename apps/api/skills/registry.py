from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = ROOT / "skills"


@lru_cache
def load_skill_index() -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    if not SKILLS_ROOT.exists():
        return index
    for meta_path in sorted(SKILLS_ROOT.glob("*/metadata.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            continue
        skill_id = str(meta.get("id") or meta_path.parent.name)
        index.append(
            {
                "id": skill_id,
                "name": str(meta.get("name") or skill_id),
                "description": str(meta.get("description") or ""),
                "path": str(meta_path.parent),
                "requires_connectors": list(meta.get("requires_connectors") or []),
                "spawns_sub_agent": bool(meta.get("spawns_sub_agent", False)),
            }
        )
    return index
