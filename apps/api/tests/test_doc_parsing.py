import base64
import io
from unittest.mock import AsyncMock, patch

import pytest

from core import llm
from parsing.engine import PREVIEW_CHAR_LIMIT, ParsedDocument, parse_document


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


def _one_page_pdf_with_text(text: str) -> bytes:
    from pypdf import PdfWriter
    # pypdf can't author text-bearing pages easily; use reportlab-free minimal route
    # via a blank page, then monkeypatch extraction in the test instead.
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
