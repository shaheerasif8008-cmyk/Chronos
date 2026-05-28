import base64
import io
from unittest.mock import AsyncMock, patch

import pytest

from core import llm


# ── Task 2: vision_ocr ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vision_ocr_returns_empty_when_no_model_configured():
    with patch.object(llm.settings, "vision_model", ""):
        out = await llm.vision_ocr(b"\x89PNG\r\n", "image/png")
    assert out == ""


@pytest.mark.asyncio
async def test_vision_ocr_sends_data_url_and_returns_text():
    fake = {"choices": [{"message": {"content": "INVOICE 42"}}]}
    with patch.object(llm.settings, "vision_model", "openrouter/openai/gpt-4o-mini"), \
         patch.object(llm, "litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=fake)
        out = await llm.vision_ocr(b"rawbytes", "image/png")
    assert out == "INVOICE 42"
    sent = mock_litellm.acompletion.call_args.kwargs
    content = sent["messages"][0]["content"]
    # multimodal: a text part + an image_url data URL part
    image_part = next(p for p in content if p["type"] == "image_url")
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64encode(b"rawbytes").decode() in image_part["image_url"]["url"]


# ── Task 3: parsing engine — text formats ────────────────────────────────────

from parsing.engine import parse_document, ParsedDocument, PREVIEW_CHAR_LIMIT


@pytest.mark.asyncio
async def test_parse_plain_text():
    doc = await parse_document(b"hello world", "text/plain", "note.txt")
    assert isinstance(doc, ParsedDocument)
    assert doc.full_text == "hello world"
    assert doc.parser_used == "text"
    assert doc.truncated is False
    assert doc.note is None


@pytest.mark.asyncio
async def test_parse_csv():
    doc = await parse_document(b"a,b\n1,2\n", "text/csv", "data.csv")
    assert "a,b" in doc.full_text
    assert doc.parser_used == "text"


@pytest.mark.asyncio
async def test_parse_unknown_type_is_unparseable_not_crash():
    doc = await parse_document(b"\x00\x01\x02", "application/x-thing", "blob.bin")
    assert doc.parser_used == "none"
    assert doc.note == "not parseable yet"
    assert doc.full_text == ""


@pytest.mark.asyncio
async def test_preview_truncates_long_text():
    big = "x" * (PREVIEW_CHAR_LIMIT + 500)
    doc = await parse_document(big.encode(), "text/plain", "big.txt")
    assert doc.truncated is True
    assert len(doc.preview) == PREVIEW_CHAR_LIMIT
    assert doc.full_text == big


# ── Task 4: PDF and image OCR ─────────────────────────────────────────────────

def _one_page_pdf_with_text(text: str) -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_parse_pdf_extracts_text_layer():
    pdf = _one_page_pdf_with_text("ignored")
    with patch("parsing.engine._pdf_page_texts", return_value=["Quarterly report", "page two"]):
        doc = await parse_document(pdf, "application/pdf", "report.pdf")
    assert "Quarterly report" in doc.full_text
    assert "page two" in doc.full_text
    assert doc.page_count == 2
    assert doc.parser_used == "pdf"


@pytest.mark.asyncio
async def test_parse_pdf_falls_back_to_ocr_on_empty_pages():
    pdf = _one_page_pdf_with_text("ignored")
    with patch("parsing.engine._pdf_page_texts", return_value=["", ""]), \
         patch("parsing.engine._pdf_page_ocr", new=AsyncMock(return_value="scanned text")):
        doc = await parse_document(pdf, "application/pdf", "scan.pdf")
    assert "scanned text" in doc.full_text
    assert doc.parser_used == "pdf+ocr"


@pytest.mark.asyncio
async def test_parse_image_uses_vision_ocr():
    with patch("parsing.engine.vision_ocr", new=AsyncMock(return_value="receipt total $9")):
        doc = await parse_document(b"\x89PNG", "image/png", "receipt.png")
    assert doc.full_text == "receipt total $9"
    assert doc.parser_used == "image-ocr"


# ── Task 5: DOCX, XLSX, PPTX ─────────────────────────────────────────────────

def _make_docx(text: str) -> bytes:
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _make_xlsx(value: str) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.active["A1"] = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_pptx(text: str) -> bytes:
    from pptx import Presentation
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_parse_docx():
    doc = await parse_document(_make_docx("Contract clause 7"), "", "agreement.docx")
    assert "Contract clause 7" in doc.full_text
    assert doc.parser_used == "docx"


@pytest.mark.asyncio
async def test_parse_xlsx():
    doc = await parse_document(_make_xlsx("Revenue 2026"), "", "model.xlsx")
    assert "Revenue 2026" in doc.full_text
    assert doc.parser_used == "xlsx"


@pytest.mark.asyncio
async def test_parse_pptx():
    doc = await parse_document(_make_pptx("Roadmap Q3"), "", "deck.pptx")
    assert "Roadmap Q3" in doc.full_text
    assert doc.parser_used == "pptx"


# ── Task 6: artifact storage round-trip ──────────────────────────────────────

@pytest.mark.asyncio
async def test_save_artifact_records_parent_and_status():
    from core import artifacts

    parent = await artifacts.save_artifact(
        b"raw pdf bytes", kind="attachment", title="report.pdf",
        mime_type="application/pdf", parse_status="pending",
    )
    child = await artifacts.save_artifact(
        "extracted text", kind="parsed_text", title="report.pdf (text)",
        parent_artifact_id=parent, parse_status="parsed",
    )
    meta = await artifacts.get_artifact(child)
    assert str(meta["parent_artifact_id"]) == parent
    assert meta["parse_status"] == "parsed"


# ── Task 7: doc tools registered ─────────────────────────────────────────────

def test_doc_tools_registered_and_broker_named():
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, to_broker_name, tool_name

    names = {tool_name(s) for s in ALL_TOOLS}
    assert "doc__parse" in names
    assert "doc__read" in names
    # available to sub-agents too
    sub_names = {tool_name(s) for s in SUBAGENT_TOOLS}
    assert "doc__parse" in sub_names and "doc__read" in sub_names
    # convert cleanly to broker dot-notation
    assert to_broker_name("doc__parse") == "doc.parse"
    assert to_broker_name("doc__read") == "doc.read"


# ── Task 8: doc connector + broker routing ───────────────────────────────────

from core.models import AgentContext


@pytest.mark.asyncio
async def test_doc_connector_parse_by_path(tmp_path, monkeypatch):
    from parsing import tool as doctool

    # Point the workspace root at a temp dir and drop a file in it.
    monkeypatch.setattr("connectors.filesystem.WORKSPACE_ROOT", tmp_path)
    work = tmp_path / "default" / "t1"
    work.mkdir(parents=True)
    (work / "note.txt").write_text("workspace doc body")

    args = {"path": "note.txt", "__org_id": "default", "__task_id": "t1"}
    result = await doctool.doc_connector.execute("doc.parse", args)
    assert "workspace doc body" in result.data["preview"]
    assert result.data["parser_used"] == "text"


@pytest.mark.asyncio
async def test_doc_connector_read_pages_artifact(monkeypatch):
    from parsing import tool as doctool

    async def fake_read(artifact_id):
        return b"A" * 100

    async def fake_meta(artifact_id):
        return {"mime_type": "text/plain", "title": "big.txt", "organization_id": "default"}

    monkeypatch.setattr(doctool, "read_artifact_content", fake_read)
    monkeypatch.setattr(doctool, "get_artifact", fake_meta)

    args = {"artifact_id": "x", "char_offset": 10, "max_chars": 5, "__org_id": "default", "__task_id": "t1"}
    result = await doctool.doc_connector.execute("doc.read", args)
    assert result.data["content"] == "AAAAA"
    assert result.data["char_offset"] == 10


@pytest.mark.asyncio
async def test_doc_parse_routes_through_broker(monkeypatch):
    from core import tool_broker as tb
    from core.models import ToolResult

    audited: list[str] = []

    async def fake_log(event_type, actor, action, **kw):
        audited.append(event_type)

    async def fake_check(*a, **k):
        return True

    monkeypatch.setattr(tb.audit, "log", fake_log)
    monkeypatch.setattr(tb.permissions, "check", fake_check)
    # connector_tier is imported into tool_broker's namespace; patch it there.
    monkeypatch.setattr(tb, "connector_tier", AsyncMock(return_value="live"))

    async def fake_doc_exec(tool, args):
        return ToolResult(data={"preview": "hi"}, summary="ok")

    monkeypatch.setattr("parsing.tool.doc_connector.execute", fake_doc_exec)

    agent = AgentContext(id="a1", org_id="default", task_id="t1")
    result = await tb.execute(agent, "doc.parse", {"artifact_id": "x"})
    assert result.summary == "ok"
    assert "tool_call" in audited and "tool_result" in audited


# ── Task 9: upload endpoint ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_attachment_stores_and_returns_id(monkeypatch):
    from routers import attachments
    from core.models import Member
    from io import BytesIO
    from starlette.datastructures import UploadFile as StarletteUploadFile, Headers

    saved: dict = {}

    async def fake_save(raw, **kw):
        saved.update(kw)
        saved["raw"] = raw
        return "att-123"

    async def fake_log(*a, **k):
        return None

    async def fake_check(*a, **k):
        return True

    monkeypatch.setattr(attachments, "save_artifact", fake_save)
    monkeypatch.setattr(attachments.audit, "log", fake_log)
    monkeypatch.setattr(attachments.permissions, "check", fake_check)

    member = Member(id="m1", organization_id="default", email="a@b.c", role="user", name="A")
    upload = StarletteUploadFile(
        filename="report.pdf",
        file=BytesIO(b"%PDF-1.4 data"),
        headers=Headers({"content-type": "application/pdf"}),
    )
    out = await attachments.upload_attachment(file=upload, conversation_id="c1", member=member)
    assert out["attachment_id"] == "att-123"
    assert out["filename"] == "report.pdf"
    assert saved["kind"] == "attachment"
    assert saved["parse_status"] == "pending"


# ── Task 10: attachments seed injection ──────────────────────────────────────

@pytest.mark.asyncio
async def test_load_history_injects_attachments_block():
    from runtime import agent_loop

    task = {
        "id": "t1",
        "goal": "Summarize the attached report",
        "agent_state": {
            "agent_history": [],
            "attachments": [
                {"filename": "report.pdf", "preview": "Q3 revenue up 12%", "truncated": True, "parsed_artifact_id": "p1"},
            ],
        },
    }
    history = await agent_loop._load_history(task, tools=[])
    blocks = [m["content"] for m in history if m["role"] == "user"]
    assert any("Q3 revenue up 12%" in b and "report.pdf" in b for b in blocks)
    assert any("doc__read" in b for b in blocks)  # truncation hint present
    assert history[-1]["content"] == "Summarize the attached report"  # goal is last


# ── Task 12: tenant isolation ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_attachment_not_parseable_across_orgs(monkeypatch):
    """An attachment owned by org A must not be parsed when requested as org B."""
    from routers import chat

    async def fake_get_artifact(att_id):
        return {"id": att_id, "organization_id": "orgA", "mime_type": "text/plain", "title": "secret.txt"}

    monkeypatch.setattr("core.artifacts.get_artifact", fake_get_artifact)

    out = await chat._parse_attachments(["att-A"], conversation_id="c1", org_id="orgB")
    assert out == []  # org mismatch → skipped, no parsed_text created
