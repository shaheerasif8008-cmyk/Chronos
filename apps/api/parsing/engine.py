"""Pure document parsing engine.

bytes + mime + filename → ParsedDocument. No DB, no object storage, no network
except the litellm OCR call. Both the chat-upload surface and the agent doc tools
wrap this module.
"""
from __future__ import annotations

from dataclasses import dataclass

#: Preview is the first ~6K tokens, approximated at 4 chars/token.
PREVIEW_CHAR_LIMIT = 24_000

#: Note set when the file type is not supported (vs a recognized type that errored).
#: Used by callers to distinguish "unparseable" from "failed".
UNPARSEABLE_NOTE = "not parseable yet"

_TEXT_MIMES = {"text/plain", "text/csv", "text/markdown", "application/csv"}
_TEXT_EXTS = {".txt", ".csv", ".md", ".markdown", ".log", ".tsv"}
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}


@dataclass
class ParsedDocument:
    full_text: str
    preview: str
    page_count: int
    char_count: int
    parser_used: str   # "pdf"|"pdf+ocr"|"docx"|"xlsx"|"pptx"|"text"|"image-ocr"|"none"
    truncated: bool
    note: str | None = None


def _finalize(full_text: str, *, page_count: int, parser_used: str, note: str | None = None) -> ParsedDocument:
    preview = full_text[:PREVIEW_CHAR_LIMIT]
    return ParsedDocument(
        full_text=full_text,
        preview=preview,
        page_count=page_count,
        char_count=len(full_text),
        parser_used=parser_used,
        truncated=len(full_text) > PREVIEW_CHAR_LIMIT,
        note=note,
    )


def _ext(filename: str) -> str:
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


async def parse_document(raw: bytes, mime: str, filename: str) -> ParsedDocument:
    mime = (mime or "").lower().split(";")[0].strip()
    ext = _ext(filename)

    if mime in _TEXT_MIMES or ext in _TEXT_EXTS:
        return _finalize(raw.decode("utf-8", errors="replace"), page_count=1, parser_used="text")
    if mime == "application/pdf" or ext == ".pdf":
        return await _parse_pdf(raw)
    if ext == ".docx" or "wordprocessingml" in mime:
        return _parse_docx(raw)
    if ext == ".xlsx" or "spreadsheetml" in mime:
        return _parse_xlsx(raw)
    if ext == ".pptx" or "presentationml" in mime:
        return _parse_pptx(raw)
    if mime in _IMAGE_MIMES or ext in {".png", ".jpg", ".jpeg", ".webp"}:
        return await _parse_image(raw, mime or f"image/{ext.lstrip('.')}")

    return _finalize("", page_count=0, parser_used="none", note=UNPARSEABLE_NOTE)


async def _parse_pdf(raw: bytes) -> ParsedDocument:  # implemented in Task 4
    raise NotImplementedError


def _parse_docx(raw: bytes) -> ParsedDocument:  # implemented in Task 5
    raise NotImplementedError


def _parse_xlsx(raw: bytes) -> ParsedDocument:  # implemented in Task 5
    raise NotImplementedError


def _parse_pptx(raw: bytes) -> ParsedDocument:  # implemented in Task 5
    raise NotImplementedError


async def _parse_image(raw: bytes, mime: str) -> ParsedDocument:  # implemented in Task 4
    raise NotImplementedError
