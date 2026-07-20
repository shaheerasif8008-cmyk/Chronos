"""Fail-closed malware scanning and durable, metadata-only verdict evidence.

User-controlled bytes are streamed to a private ClamAV daemon with the clamd
``INSTREAM`` protocol. Bytes and scanner responses are never logged or stored in
the evidence table; only bounded metadata, a content hash, and the verdict are
retained. Infected bytes are discarded rather than placed in the ordinary
artifact bucket where an authorization mistake could expose them later.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import re
import struct
from typing import Any

from sqlalchemy import insert

from core.config import settings
from core.db import engine, reflect_table


_CHUNK_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024
_SAFE_SCANNER_TEXT = re.compile(r"[^A-Za-z0-9 ._:/()+-]+")


class FileScanUnavailable(RuntimeError):
    """The configured malware scanner did not return an authoritative verdict."""

    def __init__(self, error_code: str = "scanner_unavailable") -> None:
        self.error_code = error_code
        super().__init__("The file security scanner is temporarily unavailable.")


@dataclass(frozen=True)
class FileScanResult:
    verdict: str
    sha256: str
    size_bytes: int
    engine: str = "clamav"
    engine_version: str | None = None
    signature: str | None = None
    error_code: str | None = None
    scanned_at: datetime = field(
        default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc)
    )

    def public_metadata(self) -> dict[str, Any]:
        value = asdict(self)
        value["scanned_at"] = self.scanned_at.isoformat()
        return value


def _bounded_scanner_text(value: str, *, limit: int = 255) -> str:
    return _SAFE_SCANNER_TEXT.sub("", value).strip()[:limit]


def _signature_database_time(version: str) -> datetime | None:
    parts = version.rsplit("/", 2)
    if len(parts) != 3:
        return None
    try:
        parsed = datetime.strptime(parts[-1].strip(), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _signature_database_is_fresh(version: str, *, now: datetime | None = None) -> bool:
    published_at = _signature_database_time(version)
    if published_at is None:
        return False
    current = now or datetime.now(timezone.utc)
    age_hours = (current - published_at).total_seconds() / 3600
    return -1 <= age_hours <= settings.clamav_max_signature_age_hours


async def _read_response(reader: asyncio.StreamReader) -> str:
    response = bytearray()
    while len(response) < _MAX_RESPONSE_BYTES:
        chunk = await reader.read(min(1024, _MAX_RESPONSE_BYTES - len(response)))
        if not chunk:
            break
        response.extend(chunk)
        if b"\0" in chunk or b"\n" in chunk:
            break
    if not response or len(response) >= _MAX_RESPONSE_BYTES:
        raise FileScanUnavailable("invalid_scanner_response")
    return response.split(b"\0", 1)[0].split(b"\n", 1)[0].decode(
        "utf-8", errors="replace"
    )


async def _clamd_version() -> str:
    reader, writer = await asyncio.open_connection(
        settings.clamav_host, settings.clamav_port
    )
    try:
        writer.write(b"zVERSION\0")
        await writer.drain()
        response = await _read_response(reader)
    finally:
        writer.close()
        await writer.wait_closed()
    version = _bounded_scanner_text(response)
    if not version.lower().startswith("clamav"):
        raise FileScanUnavailable("invalid_scanner_version")
    return version


async def _reload_clamd_signatures() -> None:
    """Ask clamd to reload the database that freshclam wrote to disk.

    The upstream container updates signatures independently from the daemon.
    A missed startup notification must not leave an otherwise healthy scanner
    stale until the next task restart, so readiness and ingress may perform this
    bounded, idempotent recovery before failing closed.
    """

    reader, writer = await asyncio.open_connection(settings.clamav_host, settings.clamav_port)
    try:
        writer.write(b"zRELOAD\0")
        await writer.drain()
        response = await _read_response(reader)
    finally:
        writer.close()
        await writer.wait_closed()
    if _bounded_scanner_text(response).lower() != "reloading":
        raise FileScanUnavailable("scanner_reload_failed")


async def _fresh_clamd_version() -> str:
    """Return a fresh engine version, attempting one bounded database reload."""

    version = await _clamd_version()
    if _signature_database_is_fresh(version):
        return version
    await _reload_clamd_signatures()
    # RELOAD is asynchronous. Poll for at most three seconds, well inside the
    # five-second readiness budget and the normal twenty-second scan budget.
    for _attempt in range(12):
        await asyncio.sleep(0.25)
        version = await _clamd_version()
        if _signature_database_is_fresh(version):
            return version
    raise FileScanUnavailable("scanner_signatures_stale")


async def _clamd_instream(content: bytes) -> tuple[str, str | None]:
    reader, writer = await asyncio.open_connection(settings.clamav_host, settings.clamav_port)
    try:
        writer.write(b"zINSTREAM\0")
        for offset in range(0, len(content), _CHUNK_BYTES):
            chunk = content[offset : offset + _CHUNK_BYTES]
            writer.write(struct.pack("!I", len(chunk)))
            writer.write(chunk)
            await writer.drain()
        writer.write(struct.pack("!I", 0))
        await writer.drain()
        response = await _read_response(reader)
    finally:
        writer.close()
        await writer.wait_closed()

    normalized = response.strip()
    if normalized.endswith(": OK"):
        return "clean", None
    if normalized.endswith(" FOUND"):
        details = normalized.rsplit(": ", 1)[-1][: -len(" FOUND")]
        signature = _bounded_scanner_text(details)
        if not signature:
            raise FileScanUnavailable("invalid_scanner_signature")
        return "infected", signature
    raise FileScanUnavailable("scanner_error")


async def scan_file_bytes(content: bytes) -> FileScanResult:
    """Return an authoritative verdict or a bounded error result.

    The caller decides whether an ``error`` verdict is permitted. All production
    ingress callers reject it because ``MALWARE_SCAN_REQUIRED`` is mandatory.
    """

    digest = hashlib.sha256(content).hexdigest()
    scanned_at = datetime.now(timezone.utc)
    if len(content) > settings.clamav_max_bytes:
        return FileScanResult(
            verdict="error",
            sha256=digest,
            size_bytes=len(content),
            error_code="scanner_size_limit",
            scanned_at=scanned_at,
        )
    try:
        async with asyncio.timeout(settings.clamav_timeout_seconds):
            version = await _fresh_clamd_version()
            verdict, signature = await _clamd_instream(content)
    except FileScanUnavailable as exc:
        return FileScanResult(
            verdict="error",
            sha256=digest,
            size_bytes=len(content),
            error_code=exc.error_code,
            scanned_at=scanned_at,
        )
    except TimeoutError:
        return FileScanResult(
            verdict="error",
            sha256=digest,
            size_bytes=len(content),
            error_code="scanner_timeout",
            scanned_at=scanned_at,
        )
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        return FileScanResult(
            verdict="error",
            sha256=digest,
            size_bytes=len(content),
            error_code="scanner_unavailable",
            scanned_at=scanned_at,
        )
    return FileScanResult(
        verdict=verdict,
        sha256=digest,
        size_bytes=len(content),
        engine_version=version,
        signature=signature,
        scanned_at=scanned_at,
    )


def require_safe_verdict(result: FileScanResult) -> None:
    if result.verdict == "infected":
        raise ValueError("malware_detected")
    if result.verdict != "clean" and settings.malware_scan_required:
        raise FileScanUnavailable(result.error_code or "scanner_unavailable")


async def record_file_security_event(
    result: FileScanResult,
    *,
    organization_id: str,
    source: str,
    filename: str,
    mime_type: str | None,
    created_by: str | None,
    artifact_id: str | None = None,
    source_ref: str | None = None,
    content_disarm_status: str = "not_applicable",
    content_disarm_reason: str | None = None,
) -> str:
    events = await reflect_table("file_security_events")
    safe_filename = str(filename or "file").replace("\x00", "")[:255]
    async with engine.begin() as conn:
        values: dict[str, Any] = {
            "organization_id": organization_id,
            "region": settings.region,
            "artifact_id": artifact_id,
            "source": source,
            "source_ref": str(source_ref)[:255] if source_ref else None,
            "filename": safe_filename,
            "mime_type": str(mime_type)[:255] if mime_type else None,
            "size_bytes": result.size_bytes,
            "sha256": result.sha256,
            "verdict": result.verdict,
            "engine": result.engine,
            "engine_version": result.engine_version,
            "signature": result.signature,
            "error_code": result.error_code,
            "created_by": created_by,
            "scanned_at": result.scanned_at,
        }
        if "content_disarm_status" in events.c:
            values["content_disarm_status"] = content_disarm_status
        if "content_disarm_reason" in events.c:
            values["content_disarm_reason"] = (
                str(content_disarm_reason)[:255] if content_disarm_reason else None
            )
        if "review_status" in events.c:
            values["review_status"] = (
                "pending"
                if result.verdict != "clean"
                or content_disarm_status in {"rejected", "error"}
                else "closed"
            )
        event_id = (
            await conn.execute(
                insert(events)
                .values(**values)
                .returning(events.c.id)
            )
        ).scalar_one()
    return str(event_id)


async def record_file_security_event_if_available(
    result: FileScanResult,
    **kwargs: Any,
) -> str | None:
    """Persist verdict evidence, tolerating old local schemas only outside prod.

    Production migrations run before service promotion and scanning is required,
    so losing the evidence store is a hard failure. Development/test databases
    can be intentionally behind while a migration is authored; those callers
    retain scan metadata on the artifact/download record without pretending an
    event id exists.
    """

    try:
        return await record_file_security_event(result, **kwargs)
    except Exception:
        if settings.malware_scan_required or settings.is_production:
            raise
        return None


async def scanner_health() -> dict[str, Any]:
    """Return secret-free ClamAV liveness/version evidence."""

    try:
        async with asyncio.timeout(min(settings.clamav_timeout_seconds, 5.0)):
            version = await _fresh_clamd_version()
    except FileScanUnavailable as exc:
        return {
            "healthy": False,
            "engine": "clamav",
            "version": None,
            "error_code": exc.error_code,
        }
    except Exception:  # noqa: BLE001 - readiness must stay redacted
        return {"healthy": False, "engine": "clamav", "version": None}
    return {"healthy": True, "engine": "clamav", "version": version}
