# Document Parsing, OCR & File Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Chronos read files — users attach documents/images in chat and the agent parses files it finds mid-task — through one shared parsing engine with litellm-based OCR.

**Architecture:** A pure parsing engine (`parsing/engine.py`) takes `bytes + mime + filename` and returns a `ParsedDocument`. Two surfaces wrap it: a chat upload flow (`POST /attachments` + parse-on-send, preview injected into the agent loop's seed context) and agent tools (`doc__parse` / `doc__read` routed through the ToolBroker). Parsed text and raw files are stored as rows in the existing `artifacts` table. OCR is a litellm vision call, never a system binary.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy Core, Alembic, litellm, MinIO, pytest/pytest-asyncio. New deps: `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `Pillow`.

**Reference spec:** `docs/superpowers/specs/2026-05-26-doc-parsing-ocr-attachments-design.md`

---

## File Structure

**Create:**
- `apps/api/parsing/__init__.py` — package marker
- `apps/api/parsing/engine.py` — pure parsing engine + `ParsedDocument`
- `apps/api/parsing/tool.py` — `DocConnector` wrapper (resolves artifact_id/path → bytes, returns `ToolResult`)
- `apps/api/routers/attachments.py` — `POST /attachments` upload endpoint
- `apps/api/migrations/versions/0017_attachment_parsing.py` — adds `parent_artifact_id`, `parse_status` to `artifacts`
- `apps/api/tests/test_doc_parsing.py` — engine + tool + upload tests
- `apps/api/tests/fixtures/` — small sample files (generated in tests, not committed binaries)

**Modify:**
- `apps/api/core/config.py` — add `vision_model` setting
- `apps/api/core/llm.py` — add `vision_ocr()`
- `apps/api/core/artifacts.py` — `save_artifact` accepts `parent_artifact_id` / `parse_status`; add `set_parse_status` helper
- `apps/api/runtime/tool_registry.py` — add `DOC_PARSE`, `DOC_READ` tool schemas to `ALL_TOOLS` + `SUBAGENT_TOOLS`
- `apps/api/core/tool_broker.py` — route `doc` provider; add `doc` to local-tier set
- `apps/api/routers/tasks.py` — `create_task_record` accepts `attachments_context`
- `apps/api/runtime/agent_loop.py` — `_load_history` injects an attachments seed block
- `apps/api/routers/chat.py` — `ChatRequest.attachment_ids`; parse-on-send; force loop route when attachments present
- `apps/api/main.py` — register the attachments router
- `apps/api/requirements.txt` — add parsing deps
- `apps/web/app/chat/page.tsx` — 📎 upload button, chips, send `attachment_ids`

---

## Task 1: Add parsing dependencies

**Files:**
- Modify: `apps/api/requirements.txt`

- [ ] **Step 1: Append parsing libraries**

Add these lines to the end of `apps/api/requirements.txt`:

```text
# Document parsing & OCR
pypdf>=4.2.0
python-docx>=1.1.0
openpyxl>=3.1.0
python-pptx>=0.6.23
pypdfium2>=4.30.0
Pillow>=10.0.0
```

`pypdfium2` rasterizes PDF pages for OCR via pip-installable PDFium wheels — no
separate system binary (unlike `pdf2image`, which needs Poppler installed).

- [ ] **Step 2: Install**

Run: `cd apps/api && pip install -r requirements.txt`
Expected: installs without conflict.

- [ ] **Step 3: Commit**

```bash
git add apps/api/requirements.txt
git commit -m "build: add document parsing dependencies"
```

---

## Task 2: Vision OCR helper in the model layer

**Files:**
- Modify: `apps/api/core/config.py`
- Modify: `apps/api/core/llm.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Add the vision_model setting**

In `apps/api/core/config.py`, add this line right after `fast_model` (line 21):

```python
    vision_model: str = ""   # vision-capable model for OCR; empty disables OCR
```

- [ ] **Step 2: Write the failing test**

Create `apps/api/tests/test_doc_parsing.py` with:

```python
import base64
from unittest.mock import AsyncMock, patch

import pytest

from core import llm


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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -v`
Expected: FAIL — `AttributeError: module 'core.llm' has no attribute 'vision_ocr'`.

- [ ] **Step 4: Implement vision_ocr**

In `apps/api/core/llm.py`, add after `complete_text` (around line 275):

```python
async def vision_ocr(image_bytes: bytes, mime: str) -> str:
    """Extract text from an image via a litellm vision model.

    Returns "" when no vision model is configured or the call fails — OCR is
    best-effort and must never raise into a chat turn or task step.
    """
    import base64

    if not settings.vision_model:
        return ""
    data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Extract all text from this image verbatim. Preserve reading "
                        "order and layout. Return only the extracted text, no commentary."
                    ),
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]
    try:
        kwargs = model_kwargs(settings.vision_model, messages=messages, stream=False)
        response = await _with_retry(lambda: litellm.acompletion(**kwargs), max_retries=0)
        return _message_content(response)
    except Exception:
        return ""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -v`
Expected: both `test_vision_ocr_*` PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/core/config.py apps/api/core/llm.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: add litellm vision_ocr helper for image text extraction"
```

---

## Task 3: Parsing engine — text formats

**Files:**
- Create: `apps/api/parsing/__init__.py`
- Create: `apps/api/parsing/engine.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Create the package marker**

Create `apps/api/parsing/__init__.py` (empty file).

- [ ] **Step 2: Write failing tests for text formats**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k parse -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'parsing.engine'`.

- [ ] **Step 4: Implement the engine skeleton with text + dispatch**

Create `apps/api/parsing/engine.py`:

```python
"""Pure document parsing engine.

bytes + mime + filename → ParsedDocument. No DB, no object storage, no network
except the litellm OCR call. Both the chat-upload surface and the agent doc tools
wrap this module.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from core.llm import vision_ocr

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
```

(The `_parse_*` helpers are added in the next tasks; for now this task only needs text + unknown to pass. Add temporary stubs so the module imports cleanly:)

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k parse -v`
Expected: the four text/unknown/preview tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/parsing/__init__.py apps/api/parsing/engine.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: parsing engine dispatch with text and unknown-type handling"
```

---

## Task 4: Engine — PDF and image OCR

**Files:**
- Modify: `apps/api/parsing/engine.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Write failing tests**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
from unittest.mock import AsyncMock, patch


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k "pdf or image" -v`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement PDF + image parsing**

In `apps/api/parsing/engine.py`, replace the `_parse_pdf` and `_parse_image` stubs with:

```python
import io  # already imported at top


def _pdf_page_texts(raw: bytes) -> list[str]:
    """Return the extracted text of each PDF page (empty string when none)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    return [(page.extract_text() or "").strip() for page in reader.pages]


async def _pdf_page_ocr(raw: bytes, page_index: int) -> str:
    """Render a single PDF page to a PNG and OCR it. Best-effort; "" on failure.

    Uses pypdfium2 (PDFium via pip wheels) so there is no system-binary dependency.
    """
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(raw)
        try:
            page = pdf[page_index]
            pil_image = page.render(scale=2.0).to_pil()  # ~144 DPI
        finally:
            pdf.close()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return await vision_ocr(buf.getvalue(), "image/png")
    except Exception:
        return ""


async def _parse_pdf(raw: bytes) -> ParsedDocument:
    try:
        texts = _pdf_page_texts(raw)
    except Exception:
        return _finalize("", page_count=0, parser_used="none", note="could not read PDF (corrupt or encrypted)")

    used_ocr = False
    out: list[str] = []
    for i, text in enumerate(texts):
        if text:
            out.append(text)
        else:
            ocr = await _pdf_page_ocr(raw, i)
            if ocr:
                used_ocr = True
                out.append(ocr)
    full = "\n\n".join(p for p in out if p)
    return _finalize(
        full,
        page_count=len(texts),
        parser_used="pdf+ocr" if used_ocr else "pdf",
        note=None if full else "no extractable text in PDF",
    )


async def _parse_image(raw: bytes, mime: str) -> ParsedDocument:
    text = await vision_ocr(raw, mime)
    return _finalize(
        text,
        page_count=1,
        parser_used="image-ocr",
        note=None if text else "no text extracted from image",
    )
```

Note: `pypdfium2` ships prebuilt PDFium wheels, so PDF-page rasterization works
without any system binary (honoring the "no system binary" constraint). The
`try/except` keeps scanned-PDF OCR best-effort — text-layer PDFs (the common case)
always work via `pypdf` regardless. A page is OCR'd only when its text layer is empty.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k "pdf or image" -v`
Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/parsing/engine.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: PDF text extraction with OCR fallback and image OCR"
```

---

## Task 5: Engine — DOCX, XLSX, PPTX

**Files:**
- Modify: `apps/api/parsing/engine.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Write failing tests**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k "docx or xlsx or pptx" -v`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Implement the Office parsers**

In `apps/api/parsing/engine.py`, replace the `_parse_docx`, `_parse_xlsx`, `_parse_pptx` stubs with:

```python
def _parse_docx(raw: bytes) -> ParsedDocument:
    from docx import Document

    d = Document(io.BytesIO(raw))
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return _finalize("\n".join(parts), page_count=1, parser_used="docx")


def _parse_xlsx(raw: bytes) -> ParsedDocument:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    parts: list[str] = []
    for ws in wb.worksheets:
        parts.append(f"# Sheet: {ws.title}")
        for row in ws.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(",".join(cells))
    return _finalize("\n".join(parts), page_count=len(wb.worksheets), parser_used="xlsx")


def _parse_pptx(raw: bytes) -> ParsedDocument:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(raw))
    slides = list(prs.slides)
    parts: list[str] = []
    for i, slide in enumerate(slides, start=1):
        parts.append(f"# Slide {i}")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
    return _finalize("\n".join(parts), page_count=len(slides), parser_used="pptx")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k "docx or xlsx or pptx" -v`
Expected: all three PASS.

- [ ] **Step 5: Run the whole engine test file**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -v`
Expected: all engine + OCR tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/parsing/engine.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: DOCX, XLSX, PPTX parsing in the engine"
```

---

## Task 6: Artifacts storage — parent link & parse status

**Files:**
- Create: `apps/api/migrations/versions/0017_attachment_parsing.py`
- Modify: `apps/api/core/artifacts.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Write the migration**

Create `apps/api/migrations/versions/0017_attachment_parsing.py`:

```python
"""attachment parsing: parent link + parse status on artifacts

Revision ID: 0017_attachment_parsing
Revises: 0016_task_checkpoints
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_attachment_parsing"
down_revision = "0016_task_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("artifacts", sa.Column("parent_artifact_id", sa.UUID(), nullable=True))
    op.add_column("artifacts", sa.Column("parse_status", sa.Text(), nullable=True))
    op.create_index("ix_artifacts_parent", "artifacts", ["parent_artifact_id"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_parent", "artifacts")
    op.drop_column("artifacts", "parse_status")
    op.drop_column("artifacts", "parent_artifact_id")
```

- [ ] **Step 2: Apply the migration**

Run: `cd apps/api && alembic upgrade head`
Expected: `Running upgrade 0016_task_checkpoints -> 0017_attachment_parsing`.

- [ ] **Step 3: Extend save_artifact and add set_parse_status**

In `apps/api/core/artifacts.py`, change the `save_artifact` signature to accept the two new fields. Add these parameters after `mime_type` (line 61):

```python
    parent_artifact_id: str | None = None,
    parse_status: str | None = None,
```

And add them to the `.values(...)` block in the insert (after `size_bytes=size,` on line 110):

```python
                parent_artifact_id=parent_artifact_id,
                parse_status=parse_status,
```

Then add a helper at the end of the file:

```python
async def set_parse_status(artifact_id: str, status: str) -> None:
    """Update an attachment's parse_status (pending|parsed|failed|unparseable)."""
    from sqlalchemy import update

    artifacts = await reflect_table("artifacts")
    async with engine.begin() as conn:
        await conn.execute(
            update(artifacts).where(artifacts.c.id == artifact_id).values(parse_status=status)
        )
```

- [ ] **Step 4: Write a storage round-trip test**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
```

This test requires DB + MinIO (or its local fallback). It runs in the integration environment where `alembic upgrade head` has been applied.

- [ ] **Step 5: Run the test**

Run: `cd apps/api && pytest tests/test_doc_parsing.py::test_save_artifact_records_parent_and_status -v`
Expected: PASS (DB reachable). If the suite normally skips DB tests in this repo, mark consistent with existing DB-touching tests in `test_runtime_sprint4.py`.

- [ ] **Step 6: Commit**

```bash
git add apps/api/migrations/versions/0017_attachment_parsing.py apps/api/core/artifacts.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: artifacts parent_artifact_id + parse_status for attachments"
```

---

## Task 7: Doc tools — registry schemas

**Files:**
- Modify: `apps/api/runtime/tool_registry.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && pytest tests/test_doc_parsing.py::test_doc_tools_registered_and_broker_named -v`
Expected: FAIL — `doc__parse` not in names.

- [ ] **Step 3: Add the tool schemas**

In `apps/api/runtime/tool_registry.py`, add after the `CODE_PYTHON` definition (before `ALL_TOOLS`, ~line 176):

```python
# ── Documents ─────────────────────────────────────────────────────────────────

DOC_PARSE = _fn(
    "doc__parse",
    "Parse a document or image into text. Use on a file the user attached, a file in "
    "the task workspace, or an artifact produced earlier. Supports PDF, DOCX, XLSX, "
    "PPTX, CSV, TXT, and images (OCR). Returns a text preview plus metadata; the full "
    "text is stored and can be paged with doc__read.",
    {
        "artifact_id": {"type": "string", "description": "Artifact id of the file to parse."},
        "path": {"type": "string", "description": "Path in the task workspace to parse (alternative to artifact_id)."},
    },
    [],
)

DOC_READ = _fn(
    "doc__read",
    "Read more of an already-parsed document beyond the preview. Use when the preview "
    "was truncated and you need a specific section.",
    {
        "artifact_id": {"type": "string", "description": "Artifact id of the parsed document (the parsed_text artifact, or the source attachment)."},
        "char_offset": {"type": "integer", "description": "Start offset into the full text (default 0).", "default": 0},
        "max_chars": {"type": "integer", "description": "Maximum characters to return (default 8000).", "default": 8000},
    },
    ["artifact_id"],
)
```

Then add `DOC_PARSE, DOC_READ,` to `ALL_TOOLS` (after `CODE_PYTHON,` on line 186) and to `SUBAGENT_TOOLS` (after `CODE_PYTHON,` on line 198).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && pytest tests/test_doc_parsing.py::test_doc_tools_registered_and_broker_named -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/runtime/tool_registry.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: doc__parse and doc__read tool schemas"
```

---

## Task 8: Doc connector + broker routing

**Files:**
- Create: `apps/api/parsing/tool.py`
- Modify: `apps/api/core/tool_broker.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Write failing tests**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k doc_connector -v`
Expected: FAIL — `No module named 'parsing.tool'`.

- [ ] **Step 3: Implement the connector**

Create `apps/api/parsing/tool.py`:

```python
"""Doc tool connector — resolves a file reference to bytes and parses it.

Wraps parsing.engine for the agent-facing doc__parse / doc__read tools. Routed
to from core.tool_broker like the filesystem and code connectors.
"""
from __future__ import annotations

from pathlib import Path
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
```

- [ ] **Step 4: Route the doc provider in the broker**

In `apps/api/core/tool_broker.py`, add a branch inside `_route` after the `fs` branch (after line that returns `filesystem_connector.execute`):

```python
    if provider == "doc":
        from parsing.tool import doc_connector
        return await doc_connector.execute(tool, routed_args)
```

And add `doc` to the local-tier set in `ToolBroker.execute` — change:

```python
        if tier == "live" and provider not in {"browser", "fs", "code"}:
```

to:

```python
        if tier == "live" and provider not in {"browser", "fs", "code", "doc"}:
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -k doc_connector -v`
Expected: both PASS.

- [ ] **Step 6: Broker integration test (audit + loop detection fire)**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
```

Run: `cd apps/api && pytest tests/test_doc_parsing.py::test_doc_parse_routes_through_broker -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/api/parsing/tool.py apps/api/core/tool_broker.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: doc connector with broker routing for doc.parse and doc.read"
```

---

## Task 9: Upload endpoint

**Files:**
- Create: `apps/api/routers/attachments.py`
- Modify: `apps/api/main.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Inspect main.py router registration**

Run: `grep -n "include_router\|import" apps/api/main.py | head -30`
Expected: a list of `app.include_router(...)` calls and router imports. Note the exact import style used (e.g. `from routers import chat`).

- [ ] **Step 2: Implement the upload router**

Create `apps/api/routers/attachments.py`:

```python
"""Attachment upload — stores raw bytes as an artifact. Parsing happens on first use."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Form

from core import audit, permissions
from core.artifacts import save_artifact
from core.auth import get_current_member
from core.models import Member

router = APIRouter(prefix="/attachments", tags=["attachments"])

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


@router.post("")
async def upload_attachment(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "upload_attachment", conversation_id or "new_conversation")
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")

    attachment_id = await save_artifact(
        raw,
        kind="attachment",
        title=file.filename or "upload",
        conversation_id=conversation_id,
        mime_type=file.content_type,
        org_id=member.organization_id,
        parse_status="pending",
    )
    await audit.log(
        "attachment_uploaded", member.id, "attachments.upload",
        resource_type="artifacts", resource_id=attachment_id,
    )
    return {
        "attachment_id": attachment_id,
        "filename": file.filename,
        "mime_type": file.content_type,
        "size_bytes": len(raw),
    }
```

- [ ] **Step 3: Register the router in main.py**

In `apps/api/main.py`, add `attachments` to the router imports and add `app.include_router(attachments.router)` alongside the other routers, matching the exact style observed in Step 1.

- [ ] **Step 4: Write an upload test**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
    # Member fields confirmed against core/models.py: id, organization_id, region,
    # email, role, name — the kwargs above are exact.
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
```

Confirm the `Member` constructor fields match `core/models.py` (adjust kwargs if the model differs).

- [ ] **Step 5: Run the test**

Run: `cd apps/api && pytest tests/test_doc_parsing.py::test_upload_attachment_stores_and_returns_id -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/routers/attachments.py apps/api/main.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: POST /attachments upload endpoint"
```

---

## Task 10: Parse-on-send + seed-context injection

**Files:**
- Modify: `apps/api/routers/tasks.py`
- Modify: `apps/api/runtime/agent_loop.py`
- Modify: `apps/api/routers/chat.py`
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Add attachments_context to create_task_record**

In `apps/api/routers/tasks.py`, add a parameter to `create_task_record` after `model` (line 38):

```python
    attachments_context: list[dict] | None = None,
```

And change the `agent_state` value in the insert (line 63) to:

```python
                agent_state={
                    "agent_history": [],
                    "iteration_count": 0,
                    "model": resolved_model,
                    "attachments": attachments_context or [],
                },
```

- [ ] **Step 2: Render the attachments block in the agent loop**

In `apps/api/runtime/agent_loop.py`, add a formatter after `_format_inherited_context` (line 292):

```python
def _format_attachments_context(attachments: list[dict[str, Any]]) -> str:
    """Render parsed attachment previews as one immutable seed block."""
    lines = ["# Attached files", "The user attached these files. Their parsed text follows."]
    for a in attachments:
        name = str(a.get("filename") or "file")
        artifact_id = str(a.get("parsed_artifact_id") or a.get("attachment_id") or "")
        note = a.get("note")
        header = f"\n## {name}"
        if a.get("truncated"):
            header += f"  (truncated — use doc__read with artifact_id={artifact_id} for more)"
        if note:
            header += f"  [{note}]"
        lines.append(header)
        lines.append(str(a.get("preview") or ""))
    return "\n".join(lines)
```

Then in `_load_history`, inject it in the fresh-start branch. After the inherited-context block (line 308), before the goal append (line 309), add:

```python
    attachments = state.get("attachments") if isinstance(state, dict) else None
    if isinstance(attachments, list) and attachments:
        seed.append({"role": "user", "content": _format_attachments_context(attachments)})
```

- [ ] **Step 3: Write the seed-injection test**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it fails then passes**

Run: `cd apps/api && pytest tests/test_doc_parsing.py::test_load_history_injects_attachments_block -v`
Expected: PASS after Steps 1–2 (FAIL if run before implementing).

- [ ] **Step 5: Wire parse-on-send into chat.py**

In `apps/api/routers/chat.py`:

Add `attachment_ids` to `ChatRequest` (after `workspace_id`, line 38), matching the
repo's Pydantic idiom for list defaults (e.g. `routers/workflows.py`). Ensure `Field`
is imported from `pydantic` at the top of the file:

```python
    attachment_ids: list[str] = Field(default_factory=list)
```

Add a helper above `send_message` (after `_is_trivial_chat`, ~line 168):

```python
async def _parse_attachments(attachment_ids: list[str], conversation_id: str, org_id: str) -> list[dict]:
    """Parse each not-yet-parsed attachment, store its text, return seed-context entries.

    Already-parsed attachments reuse their stored parsed_text artifact instead of
    re-parsing (the spec's "cached so re-sends don't re-parse" contract).
    """
    from sqlalchemy import select

    from core.artifacts import get_artifact, read_artifact_content, save_artifact, set_parse_status
    from core.db import engine, reflect_table
    from parsing.engine import PREVIEW_CHAR_LIMIT, UNPARSEABLE_NOTE, parse_document

    out: list[dict] = []
    for att_id in attachment_ids:
        meta = await get_artifact(att_id)
        if not meta or str(meta.get("organization_id")) != str(org_id):
            continue

        # Cache hit: reuse the stored parsed_text child rather than re-parsing.
        if str(meta.get("parse_status")) == "parsed":
            artifacts = await reflect_table("artifacts")
            async with engine.begin() as conn:
                child = (
                    await conn.execute(
                        select(artifacts).where(
                            artifacts.c.parent_artifact_id == att_id,
                            artifacts.c.kind == "parsed_text",
                        )
                    )
                ).mappings().first()
            if child:
                full = (await read_artifact_content(str(child["id"])) or b"").decode("utf-8", errors="replace")
                out.append({
                    "attachment_id": att_id,
                    "parsed_artifact_id": str(child["id"]),
                    "filename": meta.get("title"),
                    "preview": full[:PREVIEW_CHAR_LIMIT],
                    "truncated": len(full) > PREVIEW_CHAR_LIMIT,
                    "note": None,
                })
                continue

        raw = await read_artifact_content(att_id) or b""
        doc = await parse_document(raw, str(meta.get("mime_type") or ""), str(meta.get("title") or "file"))
        # Distinguish unsupported type (unparseable) from a recognized type that
        # errored — corrupt/encrypted (failed) — per the spec's status contract.
        if doc.parser_used != "none":
            status = "parsed"
        elif doc.note == UNPARSEABLE_NOTE:
            status = "unparseable"
        else:
            status = "failed"
        parsed_artifact_id = None
        if doc.full_text:
            parsed_artifact_id = await save_artifact(
                doc.full_text, kind="parsed_text", title=f"{meta.get('title')} (text)",
                conversation_id=conversation_id, parent_artifact_id=att_id,
                parse_status="parsed", org_id=org_id, mime_type="text/plain",
            )
        await set_parse_status(att_id, status)
        out.append({
            "attachment_id": att_id,
            "parsed_artifact_id": parsed_artifact_id,
            "filename": meta.get("title"),
            "preview": doc.preview,
            "truncated": doc.truncated,
            "note": doc.note,
        })
    return out
```

In `send_message`, after computing `requester_context` (line 258) and before `explicit_memory`, parse attachments and force the loop route. Replace the routing decision (line 288) region with:

```python
    attachments_context: list[dict] = []
    if req.attachment_ids:
        attachments_context = await _parse_attachments(req.attachment_ids, conversation_id, member.organization_id)
```

(place this right after the `requester_context.workspace_id = req.workspace_id` line)

Then change the route gate (line 288) to force the loop when attachments are present:

```python
    route_through_loop = (
        intent["mode"] == "task" or not _is_trivial_chat(req.message) or bool(attachments_context)
    )
```

And pass attachments into the loop. Update the `_agent_loop_stream(...)` call (line 292) to forward them — add a parameter. First extend `_agent_loop_stream`'s signature (line 170) with:

```python
    attachments_context: list[dict] | None = None,
```

and forward it into `create_task_record` (line 191):

```python
    task_id = await create_task_record(
        goal=goal,
        member=member,
        triggered_by=conversation_id,
        persona_id=persona_id,
        workspace_id=workspace_id,
        model=model,
        attachments_context=attachments_context,
    )
```

Then in the `send_message` call site (line 292), pass `attachments_context=attachments_context`.

- [ ] **Step 6: Run the full doc-parsing suite**

Run: `cd apps/api && pytest tests/test_doc_parsing.py -v`
Expected: all PASS.

- [ ] **Step 7: Run chat routing tests for regressions**

Run: `cd apps/api && pytest tests/test_chat_routing.py -v`
Expected: existing tests still PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/api/routers/tasks.py apps/api/runtime/agent_loop.py apps/api/routers/chat.py apps/api/tests/test_doc_parsing.py
git commit -m "feat: parse attachments on send and inject previews into agent context"
```

---

## Task 11: Chat UI — attach files

**Files:**
- Modify: `apps/web/app/chat/page.tsx`
- Test: manual (UI)

- [ ] **Step 1: Inspect the send flow and apiFetch**

Run: `sed -n '780,1090p' apps/web/app/chat/page.tsx`
Expected: see `sendMessage()`, the `apiFetch("/chat/message", {...})` body, the `draft` state, and the textarea row at ~line 1039. Note how the request body is built so `attachment_ids` can be added.

- [ ] **Step 2: Add attachment state and an upload handler**

Near the other `useState` hooks in the chat component, add:

```tsx
const [attachments, setAttachments] = useState<{ id: string; name: string; size: number }[]>([]);
const fileInputRef = useRef<HTMLInputElement>(null);

async function uploadFiles(files: FileList) {
  // Use bare fetch, NOT apiFetch: apiFetch forces `Content-Type: application/json`
  // whenever a body is present (page.tsx:177), which breaks multipart uploads —
  // the browser must set the multipart boundary itself. Replicate apiFetch's auth.
  const token = getToken();
  for (const file of Array.from(files)) {
    const form = new FormData();
    form.append("file", file);
    if (conversationId) form.append("conversation_id", conversationId);
    const res = await fetch(`${apiBase()}/attachments`, {
      method: "POST",
      body: form,
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (res.ok) {
      const data = await res.json();
      setAttachments(prev => [...prev, { id: data.attachment_id, name: data.filename, size: data.size_bytes }]);
    }
  }
}
```

Use the existing conversation-id variable name found in Step 1 (replace `conversationId` if the component uses a different name). `getToken` and `apiBase` are already defined at the top of the file.

- [ ] **Step 3: Send attachment_ids and clear on send**

In `sendMessage()`, add `attachment_ids` to the `/chat/message` request body:

```tsx
attachment_ids: attachments.map(a => a.id),
```

After the request is dispatched (where `draft` is cleared), also clear attachments:

```tsx
setAttachments([]);
```

- [ ] **Step 4: Add the 📎 button, hidden input, and chips**

In the textarea row (~line 1039), add a paperclip button and a hidden file input beside the model selector / send button:

```tsx
<input
  ref={fileInputRef}
  type="file"
  multiple
  className="hidden"
  onChange={e => { if (e.target.files) void uploadFiles(e.target.files); e.target.value = ""; }}
/>
<button
  type="button"
  aria-label="Attach files"
  onClick={() => fileInputRef.current?.click()}
  className="surface border border-soft rounded-md px-2.5 py-1.5 text-[12.5px]"
>📎</button>
```

Above the textarea, render chips when attachments exist:

```tsx
{attachments.length > 0 && (
  <div className="flex flex-wrap gap-2 mb-2">
    {attachments.map(a => (
      <span key={a.id} className="surface border border-soft rounded-md px-2 py-1 text-[12px] flex items-center gap-1">
        {a.name}
        <button aria-label={`Remove ${a.name}`} onClick={() => setAttachments(prev => prev.filter(x => x.id !== a.id))}>×</button>
      </span>
    ))}
  </div>
)}
```

- [ ] **Step 5: Type-check the frontend**

Run: `cd apps/web && npx tsc --noEmit`
Expected: no new type errors. (If `useRef` isn't imported, add it to the React import.)

- [ ] **Step 6: Manual verification**

Start the stack (`docker-compose up -d`, API, web). In chat: click 📎, pick a PDF, see the chip, send "summarize this". Confirm the reply reflects the document content and that an artifact row exists.

- [ ] **Step 7: Commit**

```bash
git add apps/web/app/chat/page.tsx
git commit -m "feat: attach files in chat with upload, chips, and send"
```

---

## Task 12: Integration & isolation tests

**Files:**
- Test: `apps/api/tests/test_doc_parsing.py`

- [ ] **Step 1: Write the tenant-isolation test**

Append to `apps/api/tests/test_doc_parsing.py`:

```python
@pytest.mark.asyncio
async def test_attachment_not_parseable_across_orgs(monkeypatch):
    """An attachment owned by org A must not be parsed when requested as org B."""
    from routers import chat

    async def fake_get_artifact(att_id):
        return {"id": att_id, "organization_id": "orgA", "mime_type": "text/plain", "title": "secret.txt"}

    monkeypatch.setattr("core.artifacts.get_artifact", fake_get_artifact)

    out = await chat._parse_attachments(["att-A"], conversation_id="c1", org_id="orgB")
    assert out == []  # org mismatch → skipped, no parsed_text created
```

- [ ] **Step 2: Run the isolation test**

Run: `cd apps/api && pytest tests/test_doc_parsing.py::test_attachment_not_parseable_across_orgs -v`
Expected: PASS.

- [ ] **Step 3: Run the entire API test suite**

Run: `cd apps/api && pytest -q`
Expected: all tests PASS (no regressions in existing suites).

- [ ] **Step 4: Lint**

Run: `cd apps/api && ruff check . && ruff format --check .` (if ruff is configured for this repo; otherwise skip).
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add apps/api/tests/test_doc_parsing.py
git commit -m "test: tenant isolation for attachment parsing"
```

---

## Done criteria

- A user can attach a PDF/DOCX/XLSX/PPTX/CSV/TXT/image in chat and Chronos answers using its content.
- Large documents inject a preview; the agent can call `doc__read` for more.
- The agent can call `doc__parse` on workspace files and artifacts mid-task.
- Unknown/corrupt files degrade gracefully (`unparseable`/`failed`), never crash a turn.
- All parsing routes through `parse_document`; all agent file access routes through the ToolBroker; attachments stay tenant-isolated under `artifacts/{org_id}/`.
- `pytest -q` is green.
