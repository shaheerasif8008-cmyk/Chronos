"""Task workspace path helpers.

Neutral shared location for the workspace root and path-jailing logic so that
both the filesystem connector and non-connector callers (runtime, parsing) can
reuse them without importing a connector module across the tool-broker seam.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path("/tmp/chronos_task_workspaces")


def task_workspace_root(org_id: str | None = None, task_id: str | None = None) -> Path:
    """Return the created task workspace root for a tenant/task scope."""
    org = str(org_id or "default")
    task = str(task_id or "manual")
    root = (WORKSPACE_ROOT / org / task).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def task_workspace_root_from_args(args: dict[str, Any]) -> Path:
    """Pop broker-injected scope keys and return the created task workspace."""
    org_id = str(args.pop("__org_id", "default") or "default")
    task_id = str(args.pop("__task_id", "manual") or "manual")
    return task_workspace_root(org_id, task_id)


def jailed_path(root: Path, requested: str) -> Path:
    """Resolve ``requested`` under ``root``, refusing any path that escapes it."""
    relative = requested.strip().lstrip("/")
    if not relative or relative in {".", ".."}:
        return root
    path = (root / relative).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Path escapes the task workspace")
    return path
