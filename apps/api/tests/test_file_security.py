from __future__ import annotations

from datetime import datetime, timezone
import io
import struct
import zipfile

from fastapi import HTTPException, UploadFile
import pytest

from core.file_security import (
    FileScanResult,
    FileScanUnavailable,
    require_safe_verdict,
    scan_file_bytes,
)
from core.models import Member


def _fresh_version() -> bytes:
    stamp = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S %Y")
    return f"ClamAV 1.4.5/27890/{stamp}\0".encode()


class _Reader:
    def __init__(self, response: bytes) -> None:
        self.response = response

    async def read(self, _size: int) -> bytes:
        response, self.response = self.response, b""
        return response


class _Writer:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, value: bytes) -> None:
        self.buffer.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_clamd_instream_protocol_returns_durable_clean_metadata(monkeypatch):
    writers: list[_Writer] = []
    responses = [_fresh_version(), b"stream: OK\0"]

    async def open_connection(_host: str, _port: int):
        writer = _Writer()
        writers.append(writer)
        return _Reader(responses.pop(0)), writer

    monkeypatch.setattr("core.file_security.asyncio.open_connection", open_connection)
    result = await scan_file_bytes(b"client-document")

    assert result.verdict == "clean"
    assert result.engine == "clamav"
    assert result.engine_version and result.engine_version.startswith("ClamAV 1.4.5")
    assert len(result.sha256) == 64
    assert writers[0].buffer == b"zVERSION\0"
    wire = bytes(writers[1].buffer)
    assert wire.startswith(b"zINSTREAM\0" + struct.pack("!I", len(b"client-document")))
    assert wire.endswith(struct.pack("!I", 0))
    assert b"client-document" in wire


@pytest.mark.asyncio
async def test_clamd_infected_verdict_is_bounded_and_rejected(monkeypatch):
    responses = [_fresh_version(), b"stream: Win.Test.EICAR_HDB-1 FOUND\0"]

    async def open_connection(_host: str, _port: int):
        return _Reader(responses.pop(0)), _Writer()

    monkeypatch.setattr("core.file_security.asyncio.open_connection", open_connection)
    result = await scan_file_bytes(b"eicar-fixture")

    assert result.verdict == "infected"
    assert result.signature == "Win.Test.EICAR_HDB-1"
    with pytest.raises(ValueError, match="malware_detected"):
        require_safe_verdict(result)


def test_required_scanner_failure_fails_closed(monkeypatch):
    monkeypatch.setattr("core.file_security.settings.malware_scan_required", True)
    result = FileScanResult(
        verdict="error",
        sha256="a" * 64,
        size_bytes=10,
        error_code="scanner_timeout",
        scanned_at=datetime.now(timezone.utc),
    )
    with pytest.raises(FileScanUnavailable) as exc:
        require_safe_verdict(result)
    assert exc.value.error_code == "scanner_timeout"


@pytest.mark.asyncio
async def test_stale_signature_database_never_scans_user_bytes(monkeypatch):
    stale = "ClamAV 1.4.5/27000/Wed Jan 01 00:00:00 2025"
    reloads: list[bool] = []

    async def stale_version() -> str:
        return stale

    async def reload_signatures() -> None:
        reloads.append(True)

    async def no_wait(_seconds: float) -> None:
        return None

    async def must_not_scan(_content: bytes):
        raise AssertionError("stale signatures must never scan user bytes")

    monkeypatch.setattr("core.file_security._clamd_version", stale_version)
    monkeypatch.setattr("core.file_security._reload_clamd_signatures", reload_signatures)
    monkeypatch.setattr("core.file_security.asyncio.sleep", no_wait)
    monkeypatch.setattr("core.file_security._clamd_instream", must_not_scan)
    result = await scan_file_bytes(b"must-not-stream")

    assert result.verdict == "error"
    assert result.error_code == "scanner_signatures_stale"
    assert reloads == [True]


@pytest.mark.asyncio
async def test_stale_loaded_database_recovers_after_bounded_reload(monkeypatch):
    stale = "ClamAV 1.4.5/27000/Wed Jan 01 00:00:00 2025"
    fresh = _fresh_version().rstrip(b"\0").decode()
    versions = [stale, fresh]
    reloads: list[bool] = []

    async def version() -> str:
        return versions.pop(0)

    async def reload_signatures() -> None:
        reloads.append(True)

    async def no_wait(_seconds: float) -> None:
        return None

    async def clean_scan(_content: bytes):
        return "clean", None

    monkeypatch.setattr("core.file_security._clamd_version", version)
    monkeypatch.setattr("core.file_security._reload_clamd_signatures", reload_signatures)
    monkeypatch.setattr("core.file_security.asyncio.sleep", no_wait)
    monkeypatch.setattr("core.file_security._clamd_instream", clean_scan)

    result = await scan_file_bytes(b"reload-then-scan")

    assert result.verdict == "clean"
    assert result.engine_version == fresh
    assert reloads == [True]


@pytest.mark.asyncio
async def test_attachment_malware_is_rejected_before_object_storage(monkeypatch):
    from routers import attachments

    infected = FileScanResult(
        verdict="infected",
        sha256="b" * 64,
        size_bytes=12,
        signature="Win.Test.EICAR_HDB-1",
        scanned_at=datetime.now(timezone.utc),
    )
    stored = False

    async def allow(*_args, **_kwargs):
        return True

    async def fake_scan(_content: bytes):
        return infected

    async def fake_record(*_args, **_kwargs):
        return "event-1"

    async def fake_save(*_args, **_kwargs):
        nonlocal stored
        stored = True
        return "artifact-1"

    monkeypatch.setattr(attachments.permissions, "check", allow)
    monkeypatch.setattr(attachments.audit, "log", allow)
    monkeypatch.setattr(attachments, "scan_file_bytes", fake_scan)
    monkeypatch.setattr(attachments, "record_file_security_event_if_available", fake_record)
    monkeypatch.setattr(attachments, "save_artifact", fake_save)

    member = Member(id="member-1", organization_id="org-1", email="member@example.com")
    upload = UploadFile(filename="payload.txt", file=io.BytesIO(b"eicar-fixture"))
    with pytest.raises(HTTPException) as exc:
        await attachments.upload_attachment(
            file=upload,
            conversation_id=None,
            project_id=None,
            task_id=None,
            research_run_id=None,
            member=member,
        )

    assert exc.value.status_code == 422
    assert stored is False


@pytest.mark.asyncio
async def test_browser_download_malware_is_rejected_before_object_storage(
    monkeypatch, tmp_path
):
    from connectors import browser_operator as browser_module

    infected_path = tmp_path / "invoice.pdf"
    infected_path.write_bytes(b"eicar-fixture")
    infected = FileScanResult(
        verdict="infected",
        sha256="c" * 64,
        size_bytes=13,
        signature="Win.Test.EICAR_HDB-1",
        scanned_at=datetime.now(timezone.utc),
    )

    class FakeDownload:
        suggested_filename = "invoice.pdf"

        async def path(self):
            return str(infected_path)

    class FakeDownloadInfo:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        @property
        async def value(self):
            return FakeDownload()

    class FakePage:
        def expect_download(self):
            return FakeDownloadInfo()

        async def click(self, _selector):
            return None

    async def fake_scan(_content: bytes):
        return infected

    async def fake_record(*_args, **_kwargs):
        return "event-browser-1"

    async def noop(*_args, **_kwargs):
        return None

    async def forbidden_put(*_args, **_kwargs):
        raise AssertionError("infected browser bytes reached object storage")

    operator = browser_module.BrowserOperator()
    operator._pages["session-1"] = browser_module._RuntimeHandle(
        None, None, None, FakePage(), "browserbase"
    )
    session = {
        "id": "session-1",
        "organization_id": "org-1",
        "member_id": "member-1",
        "status": "active",
        "downloads": [],
        "history": [],
    }
    monkeypatch.setattr(browser_module, "scan_file_bytes", fake_scan)
    monkeypatch.setattr(
        browser_module, "record_file_security_event_if_available", fake_record
    )
    monkeypatch.setattr(browser_module, "put_object", forbidden_put)
    monkeypatch.setattr(operator, "_record_action", noop)

    with pytest.raises(ValueError, match="blocked because it contains malware"):
        await operator._download(session, {"selector": "a.download"})

    assert session["downloads"] == []


def test_active_content_inspection_rejects_executables_macros_and_embeds():
    from core.content_disarm import inspect_active_content

    assert inspect_active_content(b"MZ\x00payload", filename="invoice.txt", mime_type="text/plain").reason == "executable_file_magic"
    assert inspect_active_content(b"office", filename="budget.xlsm", mime_type="application/octet-stream").reason == "macro_enabled_office_document"
    assert inspect_active_content(b"%PDF-1.7 /OpenAction 2 0 R", filename="brief.pdf", mime_type="application/pdf").reason == "pdf_active_content"
    assert inspect_active_content(b"<svg onload='steal()'/>", filename="logo.svg", mime_type="image/svg+xml").reason == "active_markup"

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("word/document.xml", "safe")
        zipped.writestr("word/_rels/document.xml.rels", '<Relationship TargetMode="External"/>')
    result = inspect_active_content(
        archive.getvalue(),
        filename="client.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.reason == "office_external_relationship"


@pytest.mark.asyncio
async def test_connector_disarm_returns_new_text_only_bytes(monkeypatch):
    from types import SimpleNamespace
    from core.content_disarm import disarm_connector_binary

    async def parse(_content, _mime_type, _filename):
        return SimpleNamespace(full_text="Quarterly client summary", parser_used="pdf", note="parsed")

    monkeypatch.setattr("parsing.engine.parse_document", parse)
    result = await disarm_connector_binary(
        b"%PDF-1.7 safe inert bytes",
        filename="summary.pdf",
        mime_type="application/pdf",
    )
    assert result.status == "sanitized"
    assert result.content == b"Quarterly client summary"
    assert result.content != b"%PDF-1.7 safe inert bytes"
    assert result.mime_type == "text/plain"


def test_quarantine_review_is_metadata_only_and_false_positive_is_explicit():
    from pydantic import ValidationError
    from routers.file_security import ReviewRequest, _public_event

    raw = {
        "id": "event-1",
        "organization_id": "org-secret",
        "artifact_id": "artifact-never-restored",
        "filename": "invoice.pdf",
        "sha256": "a" * 64,
        "verdict": "infected",
        "raw_bytes": b"forbidden",
    }
    public = _public_event(raw)
    assert public["id"] == "event-1"
    assert "organization_id" not in public
    assert "artifact_id" not in public
    assert "raw_bytes" not in public
    with pytest.raises(ValidationError):
        ReviewRequest(status="false_positive", note="too short")
    accepted = ReviewRequest(
        status="false_positive",
        note="Validated against the original provider record.",
        confirmation="MARK FALSE POSITIVE",
    )
    assert accepted.status == "false_positive"
