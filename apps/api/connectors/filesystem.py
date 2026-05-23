from __future__ import annotations

from pathlib import Path
from typing import Any

from core.models import ToolResult

WORKSPACE_ROOT = Path("/tmp/chronos_task_workspaces")
MAX_WRITE_BYTES = 256_000
MAX_READ_BYTES = 256_000


def _workspace_root(args: dict[str, Any]) -> Path:
    org_id = str(args.pop("__org_id", "default") or "default")
    task_id = str(args.pop("__task_id", "manual") or "manual")
    root = (WORKSPACE_ROOT / org_id / task_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _jailed_path(root: Path, requested: str) -> Path:
    relative = requested.strip().lstrip("/")
    if not relative or relative in {".", ".."}:
        return root
    path = (root / relative).resolve()
    if root != path and root not in path.parents:
        raise ValueError("Path escapes the task workspace")
    return path


class FilesystemConnector:
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        root = _workspace_root(args)
        args.pop("__connector_tier", None)
        if tool == "fs.list":
            return await self._list(root, args)
        if tool == "fs.read":
            return await self._read(root, args)
        if tool == "fs.write":
            return await self._write(root, args)
        raise ValueError(f"Unknown filesystem tool: {tool}")

    async def _list(self, root: Path, args: dict[str, Any]) -> ToolResult:
        path = _jailed_path(root, str(args.get("path") or "."))
        if not path.exists():
            raise FileNotFoundError(str(args.get("path") or "."))
        if path.is_file():
            entries = [{"path": str(path.relative_to(root)), "type": "file", "size": path.stat().st_size}]
        else:
            entries = [
                {
                    "path": str(child.relative_to(root)),
                    "type": "directory" if child.is_dir() else "file",
                    "size": None if child.is_dir() else child.stat().st_size,
                }
                for child in sorted(path.iterdir(), key=lambda item: item.name)[:200]
            ]
        return ToolResult(data={"root": str(root), "entries": entries}, summary=f"Listed {len(entries)} workspace entries")

    async def _read(self, root: Path, args: dict[str, Any]) -> ToolResult:
        path = _jailed_path(root, str(args.get("path") or ""))
        if not path.is_file():
            raise FileNotFoundError(str(args.get("path") or ""))
        raw = path.read_bytes()
        truncated = len(raw) > MAX_READ_BYTES
        content = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        return ToolResult(
            data={"path": str(path.relative_to(root)), "content": content, "truncated": truncated, "bytes": len(raw)},
            summary=f"Read {path.relative_to(root)} ({min(len(raw), MAX_READ_BYTES)} bytes)",
        )

    async def _write(self, root: Path, args: dict[str, Any]) -> ToolResult:
        path = _jailed_path(root, str(args.get("path") or ""))
        content = str(args.get("content") or "")
        raw = content.encode("utf-8")
        if len(raw) > MAX_WRITE_BYTES:
            raise ValueError(f"fs.write payload exceeds {MAX_WRITE_BYTES} bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return ToolResult(
            data={"path": str(path.relative_to(root)), "bytes": len(raw)},
            summary=f"Wrote {path.relative_to(root)} ({len(raw)} bytes)",
        )


filesystem_connector = FilesystemConnector()
