"""Doc tool connector — resolves a file reference to bytes and parses it.

Wraps parsing.engine for the agent-facing doc__parse / doc__read tools. Routed
to from core.tool_broker like the filesystem and code connectors.
"""
from __future__ import annotations

import json
from typing import Any

from core.artifacts import get_artifact, read_artifact_content
from core.models import ToolResult
from parsing.engine import ParsedDocument, parse_document


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


def _derive_citations(full_text: str, artifact_id: str, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Locate each claim's verbatim quote in full_text and build citation objects.

    Claims whose quote cannot be found in full_text are silently dropped so that
    no citation is ever returned for text not present in the source.

    Args:
        full_text: The parsed source text of the document.
        artifact_id: The source artifact identifier for citation attribution.
        claims: List of dicts with at least a ``quote`` key (verbatim text from source).

    Returns:
        List of citation dicts with ``source_artifact_id``, ``char_start``,
        ``char_end``, and ``quote``.
    """
    citations: list[dict[str, Any]] = []
    for claim in claims:
        quote = (claim.get("quote") or "").strip()
        if not quote:
            continue
        idx = full_text.find(quote)
        if idx == -1:
            continue  # drop — quote not found in source
        citations.append(
            {
                "source_artifact_id": artifact_id,
                "char_start": idx,
                "char_end": idx + len(quote),
                "quote": quote,
            }
        )
    return citations


async def _resolve_artifact(artifact_id: str, org_id: str) -> tuple[bytes, str, str]:
    """Resolve an artifact to (raw_bytes, mime_type, title), enforcing org boundary."""
    meta = await get_artifact(str(artifact_id))
    if not meta:
        raise FileNotFoundError(f"artifact {artifact_id}")
    if str(meta.get("organization_id", "")) != str(org_id):
        raise PermissionError(f"artifact {artifact_id} does not belong to this organization")
    raw = await read_artifact_content(str(artifact_id)) or b""
    mime = str(meta.get("mime_type") or "")
    title = str(meta.get("title") or "file")
    return raw, mime, title


class DocConnector:
    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        args.pop("__connector_tier", None)
        if tool == "doc.parse":
            return await self._parse(args)
        if tool == "doc.read":
            return await self._read(args)
        if tool == "doc.summarize":
            return await self._summarize(args)
        if tool == "doc.compare":
            return await self._compare(args)
        raise ValueError(f"Unknown doc tool: {tool}")

    async def _parse(self, args: dict[str, Any]) -> ToolResult:
        artifact_id = args.get("artifact_id")
        path = args.get("path")
        org_id = str(args.get("__org_id", "default") or "default")
        if artifact_id:
            raw, mime, filename = await _resolve_artifact(str(artifact_id), org_id)
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
        org_id = str(args.get("__org_id", "default") or "default")

        meta = await get_artifact(artifact_id)
        if not meta:
            raise FileNotFoundError(f"artifact {artifact_id}")
        if str(meta.get("organization_id", "")) != org_id:
            raise PermissionError(f"artifact {artifact_id} does not belong to this organization")
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

    async def _summarize(self, args: dict[str, Any]) -> ToolResult:
        """Parse a document and produce a structured summary with verifiable citations.

        Each claim in the summary is backed by a verbatim quote from the source text.
        Quotes are located in the full text to derive char-offset citations; any claim
        whose quote is not found in the source is dropped rather than fabricated.

        Returns an honest warning result (no model call) when the document is unparseable.
        """
        from core import llm

        artifact_id = str(args.get("artifact_id") or "")
        if not artifact_id:
            raise ValueError("doc.summarize requires artifact_id")
        org_id = str(args.get("__org_id", "default") or "default")

        raw, mime, title = await _resolve_artifact(artifact_id, org_id)
        doc = await parse_document(raw, mime, title)

        if doc.parser_used == "none":
            return ToolResult(
                data={
                    "warning": "document could not be parsed",
                    "note": doc.note,
                    "parser_used": doc.parser_used,
                    "summary": None,
                    "citations": [],
                },
                summary=f"Cannot summarize {title}: {doc.note or 'unsupported format'}",
            )

        prompt = (
            "You are a document analysis assistant. Produce a structured summary of the document below.\n"
            "Return ONLY valid JSON in this exact shape:\n"
            '{"sections": [{"heading": "string", "text": "string", "quote": "verbatim_excerpt_from_document"}]}\n'
            "Rules:\n"
            "- Each section MUST include a `quote` that is a VERBATIM substring of the document text below.\n"
            "- Do not paraphrase for quotes — copy exact words from the source.\n"
            "- Return between 2 and 6 sections.\n"
            f"\n\nDocument title: {title}\n\nDocument text:\n{doc.full_text[:16000]}"
        )

        try:
            raw_json = await llm.complete_json(prompt)
            parsed = json.loads(raw_json)
            sections = parsed.get("sections") or []
        except Exception:
            sections = []

        citations = _derive_citations(doc.full_text, artifact_id, sections)

        # Only include sections whose quote was verifiably found in the source.
        # Sections with unverifiable quotes are dropped entirely — no claim is returned
        # without a corresponding stored source span (acceptance requirement).
        verified_quotes = {c["quote"] for c in citations}
        summary_sections = [
            {
                "heading": sec.get("heading", ""),
                "text": sec.get("text", ""),
            }
            for sec in sections
            if (sec.get("quote") or "").strip() in verified_quotes
        ]

        return ToolResult(
            data={
                "summary": summary_sections,
                "citations": citations,
                "parser_used": doc.parser_used,
                "truncated": doc.truncated,
                "note": doc.note,
                "char_count": doc.char_count,
            },
            summary=f"Summarized {title} ({len(citations)} citations)",
        )

    async def _compare(self, args: dict[str, Any]) -> ToolResult:
        """Parse two documents and produce a structured comparison with citations into both.

        Each similarity/difference entry is backed by a verbatim quote from its respective
        source document. Quotes not found in the source are dropped (no fabricated spans).
        """
        from core import llm

        artifact_id_a = str(args.get("artifact_id_a") or "")
        artifact_id_b = str(args.get("artifact_id_b") or "")
        if not artifact_id_a or not artifact_id_b:
            raise ValueError("doc.compare requires artifact_id_a and artifact_id_b")
        org_id = str(args.get("__org_id", "default") or "default")

        raw_a, mime_a, title_a = await _resolve_artifact(artifact_id_a, org_id)
        raw_b, mime_b, title_b = await _resolve_artifact(artifact_id_b, org_id)

        doc_a = await parse_document(raw_a, mime_a, title_a)
        doc_b = await parse_document(raw_b, mime_b, title_b)

        warnings: list[str] = []
        if doc_a.parser_used == "none":
            warnings.append(f"Document A ({title_a}) could not be parsed: {doc_a.note}")
        if doc_b.parser_used == "none":
            warnings.append(f"Document B ({title_b}) could not be parsed: {doc_b.note}")
        if warnings:
            return ToolResult(
                data={
                    "warning": "; ".join(warnings),
                    "summary": None,
                    "citations": [],
                    "parser_used_a": doc_a.parser_used,
                    "parser_used_b": doc_b.parser_used,
                },
                summary=f"Cannot compare: {'; '.join(warnings)}",
            )

        prompt = (
            "You are a document comparison assistant. Compare the two documents below.\n"
            "Return ONLY valid JSON in this exact shape:\n"
            '{"items": [{"type": "similarity"|"difference", "description": "string", '
            '"quote_a": "verbatim_from_doc_a_or_null", "quote_b": "verbatim_from_doc_b_or_null"}]}\n'
            "Rules:\n"
            "- `quote_a` must be a VERBATIM substring of Doc A text (or null if not applicable).\n"
            "- `quote_b` must be a VERBATIM substring of Doc B text (or null if not applicable).\n"
            "- Do not paraphrase — copy exact words.\n"
            "- Return between 3 and 8 items covering both similarities and differences.\n"
            f"\n\nDoc A title: {title_a}\nDoc A text:\n{doc_a.full_text[:8000]}"
            f"\n\nDoc B title: {title_b}\nDoc B text:\n{doc_b.full_text[:8000]}"
        )

        try:
            raw_json = await llm.complete_json(prompt)
            parsed = json.loads(raw_json)
            items = parsed.get("items") or []
        except Exception:
            items = []

        # Derive citations for each source separately
        claims_a = [{"quote": item.get("quote_a")} for item in items if item.get("quote_a")]
        claims_b = [{"quote": item.get("quote_b")} for item in items if item.get("quote_b")]
        citations_a = _derive_citations(doc_a.full_text, artifact_id_a, claims_a)
        citations_b = _derive_citations(doc_b.full_text, artifact_id_b, claims_b)
        all_citations = citations_a + citations_b

        return ToolResult(
            data={
                "comparison": items,
                "citations": all_citations,
                "parser_used_a": doc_a.parser_used,
                "parser_used_b": doc_b.parser_used,
                "truncated_a": doc_a.truncated,
                "truncated_b": doc_b.truncated,
            },
            summary=f"Compared {title_a} vs {title_b} ({len(all_citations)} citations)",
        )


doc_connector = DocConnector()
