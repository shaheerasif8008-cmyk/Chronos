"""Bounded active-content inspection and text-only connector disarm.

Chronos does not attempt to preserve active behavior from untrusted documents.
Attachments containing executable/embedded/automatic content are rejected after
malware scanning. Connector-synchronized binary documents are additionally
parsed into a new text-only artifact; the original provider bytes are never
persisted or exposed for download.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import PurePosixPath
import re
import zipfile

from core.config import settings


_EXECUTABLE_EXTENSIONS = {
    ".app",
    ".com",
    ".cpl",
    ".dll",
    ".dmg",
    ".exe",
    ".jar",
    ".lnk",
    ".msi",
    ".msp",
    ".scr",
}
_EXECUTABLE_MIME_PREFIXES = (
    "application/x-dosexec",
    "application/x-executable",
    "application/x-mach-binary",
    "application/x-msdownload",
    "application/x-sharedlib",
)
_OFFICE_EXTENSIONS = {
    ".docx",
    ".docm",
    ".dotm",
    ".pptx",
    ".pptm",
    ".potm",
    ".ppsx",
    ".ppsm",
    ".xlsx",
    ".xlsm",
    ".xltm",
}
_MACRO_EXTENSIONS = {".docm", ".dotm", ".pptm", ".potm", ".ppsm", ".xlsm", ".xltm"}
_ACTIVE_PDF_MARKERS = (
    b"/javascript",
    b"/js",
    b"/openaction",
    b"/aa",
    b"/launch",
    b"/embeddedfiles",
    b"/richmedia",
    b"/xfa",
)
_ACTIVE_MARKUP = re.compile(
    rb"<(?:script|iframe|object|embed|applet)\b|\bon[a-z]{3,}\s*=|javascript\s*:",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ContentDisarmResult:
    status: str
    reason: str | None = None
    content: bytes | None = None
    mime_type: str | None = None


def _has_executable_magic(content: bytes) -> bool:
    if content.startswith(b"MZ") or content.startswith(b"\x7fELF"):
        return True
    return content.startswith(
        (
            b"\xfe\xed\xfa\xce",
            b"\xfe\xed\xfa\xcf",
            b"\xce\xfa\xed\xfe",
            b"\xcf\xfa\xed\xfe",
            b"\xca\xfe\xba\xbe",
        )
    )


def _inspect_zip(content: bytes, *, office_document: bool) -> ContentDisarmResult:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > 10_000:
                return ContentDisarmResult("rejected", "archive_file_count_limit")
            total = sum(max(0, int(info.file_size)) for info in infos)
            if total > settings.artifact_preview_max_uncompressed_bytes:
                return ContentDisarmResult("rejected", "archive_uncompressed_size_limit")
            names = [info.filename.replace("\\", "/").lower() for info in infos]
            for name in names:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    return ContentDisarmResult("rejected", "archive_path_traversal")
                if path.suffix.lower() in _EXECUTABLE_EXTENSIONS:
                    return ContentDisarmResult("rejected", "archive_executable_content")
            if not office_document:
                return ContentDisarmResult("safe")
            active_parts = (
                "vbaproject.bin",
                "/activex/",
                "/embeddings/",
                "/customui/",
                "/oleobject",
            )
            if any(any(part in f"/{name}" for part in active_parts) for name in names):
                return ContentDisarmResult("rejected", "office_active_content")
            for info in infos:
                if not info.filename.lower().endswith(".rels"):
                    continue
                if info.file_size > 2 * 1024 * 1024:
                    return ContentDisarmResult("rejected", "office_relationships_oversize")
                relationships = archive.read(info).lower()
                if b'targetmode="external"' in relationships or b"targetmode='external'" in relationships:
                    return ContentDisarmResult("rejected", "office_external_relationship")
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return ContentDisarmResult("rejected", "invalid_archive")
    return ContentDisarmResult("safe")


def inspect_active_content(
    content: bytes,
    *,
    filename: str,
    mime_type: str | None,
) -> ContentDisarmResult:
    """Reject executable and automatic/embedded document behavior."""

    suffix = PurePosixPath(filename.lower()).suffix
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    if suffix in _EXECUTABLE_EXTENSIONS or mime.startswith(_EXECUTABLE_MIME_PREFIXES):
        return ContentDisarmResult("rejected", "executable_file_type")
    if _has_executable_magic(content):
        return ContentDisarmResult("rejected", "executable_file_magic")
    if suffix in _MACRO_EXTENSIONS:
        return ContentDisarmResult("rejected", "macro_enabled_office_document")
    if content.startswith(b"PK\x03\x04"):
        return _inspect_zip(content, office_document=suffix in _OFFICE_EXTENSIONS)
    if suffix == ".pdf" or mime == "application/pdf" or content.startswith(b"%PDF-"):
        lowered = content.lower()
        if b"/encrypt" in lowered:
            return ContentDisarmResult("rejected", "encrypted_pdf_not_inspectable")
        if any(marker in lowered for marker in _ACTIVE_PDF_MARKERS):
            return ContentDisarmResult("rejected", "pdf_active_content")
    if suffix in {".html", ".htm", ".svg"} or mime in {"text/html", "image/svg+xml"}:
        if _ACTIVE_MARKUP.search(content[: settings.clamav_max_bytes]):
            return ContentDisarmResult("rejected", "active_markup")
    return ContentDisarmResult("safe")


async def disarm_connector_binary(
    content: bytes,
    *,
    filename: str,
    mime_type: str | None,
) -> ContentDisarmResult:
    """Return a text-only representation or a bounded rejection/error verdict."""

    inspected = inspect_active_content(content, filename=filename, mime_type=mime_type)
    if inspected.status != "safe":
        return inspected
    from parsing.engine import parse_document

    try:
        parsed = await parse_document(content, str(mime_type or ""), filename)
    except Exception:  # noqa: BLE001 - parser/native-library errors stay bounded
        return ContentDisarmResult("error", "document_parser_error")
    text = str(parsed.full_text or "").strip()
    if parsed.parser_used == "none" or not text:
        return ContentDisarmResult("rejected", "document_has_no_safe_text")
    encoded = text.encode("utf-8")
    if len(encoded) > settings.clamav_max_bytes:
        return ContentDisarmResult("rejected", "sanitized_text_size_limit")
    return ContentDisarmResult(
        "sanitized",
        str(parsed.note or "")[:255] or None,
        encoded,
        "text/plain",
    )
