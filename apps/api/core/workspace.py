"""Task workspace path helpers.

Neutral shared location for the workspace root and path-jailing logic so that
both the filesystem connector and non-connector callers (runtime, parsing) can
reuse them without importing a connector module across the tool-broker seam.
"""
from __future__ import annotations

from pathlib import Path

WORKSPACE_ROOT = Path("/tmp/chronos_task_workspaces")


def jailed_path(root: Path, requested: str) -> Path:
    """Resolve ``requested`` under ``root``, refusing any path that escapes it."""
    relative = requested.strip().lstrip("/")
    if not relative or relative in {".", ".."}:
        return root
    path = (root / relative).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Path escapes the task workspace")
    return path
