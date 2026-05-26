"""Doc tool connector — resolves a file reference to bytes and parses it.

Wraps parsing.engine for the agent-facing doc__parse / doc__read tools. Routed
to from core.tool_broker like the filesystem and code connectors.
"""
from __future__ import annotations

from typing import Any

from core.artifacts import get_artifact, read_artifact_content
from core.models import ToolResult
from parsing.engine import parse_document


def _workspace_file(args: dict[str, Any], rel: str) -> bytes:
    from connectors.filesystem import WORKSPACE_ROOT, _jailed_path

    org_id = str(args.get("__org_id", "default") or "default")
    task_id = str(args.get("__task_id", "manual") or "manual")
    root = (WORKSPACE_ROOT / org_id / task_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = _jailed_path(root, rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    return path.read_bytes()


class DocConnector:
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args.pop("__connector_tier", None)
        if tool == "doc.parse":
            return await self._parse(args)
        if tool == "doc.read":
            return await self._read(args)
        raise ValueError(f"Unknown doc tool: {tool}")

    async def _parse(self, args: dict[str, Any]) -> ToolResult:
        artifact_id = args.get("artifact_id")
        path = args.get("path")
        if artifact_id:
            meta = await get_artifact(str(artifact_id))
            if not meta:
                raise FileNotFoundError(f"artifact {artifact_id}")
            raw = await read_artifact_content(str(artifact_id)) or b""
            mime = str(meta.get("mime_type") or "")
            filename = str(meta.get("title") or "file")
        elif path:
            raw = _workspace_file(args, str(path))
            mime, filename = "", str(path)
        else:
            raise ValueError("doc.parse requires artifact_id or path")

        doc = await parse_document(raw, mime, filename)
        return ToolResult(
            data={
                "preview": doc.preview,
                "char_count": doc.char_count,
                "page_count": doc.page_count,
                "parser_used": doc.parser_used,
                "truncated": doc.truncated,
                "note": doc.note,
            },
            summary=f"Parsed {filename} ({doc.parser_used}, {doc.char_count} chars)",
        )

    async def _read(self, args: dict[str, Any]) -> ToolResult:
        artifact_id = str(args.get("artifact_id") or "")
        if not artifact_id:
            raise ValueError("doc.read requires artifact_id")
        offset = int(args.get("char_offset", 0) or 0)
        max_chars = int(args.get("max_chars", 8000) or 8000)

        meta = await get_artifact(artifact_id)
        if not meta:
            raise FileNotFoundError(f"artifact {artifact_id}")
        raw = await read_artifact_content(artifact_id) or b""
        # parsed_text artifacts already hold plain text; source files are parsed first.
        if str(meta.get("kind")) == "parsed_text":
            full = raw.decode("utf-8", errors="replace")
        else:
            doc = await parse_document(raw, str(meta.get("mime_type") or ""), str(meta.get("title") or "file"))
            full = doc.full_text
        window = full[offset : offset + max_chars]
        return ToolResult(
            data={"content": window, "char_offset": offset, "returned_chars": len(window), "total_chars": len(full)},
            summary=f"Read {len(window)} chars at offset {offset}",
        )


doc_connector = DocConnector()
