"""Acceptance proof for doc.summarize and doc.compare (Task 2 — Document Intelligence).

Tests verify:
- doc.summarize and doc.compare route through the tool broker (audit trail asserted).
- Returned citations have (char_start, char_end) that index a real quote inside the
  source text: full_text[char_start:char_end] == quote.
- doc.compare returns citations into BOTH source documents.
- Unparseable input yields an honest warning with no fabricated summary.
- PDF, DOCX, XLSX, and CSV are all covered.
- LLM calls are monkeypatched — tests are fully deterministic.
"""
from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core.models import AgentContext


# ---------------------------------------------------------------------------
# Helpers: build minimal fixture bytes per file class
# ---------------------------------------------------------------------------

def _make_pdf_bytes() -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_docx_bytes(text: str) -> bytes:
    from docx import Document
    d = Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _make_xlsx_bytes(value: str) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    wb.active["A1"] = value
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_csv_bytes(text: str) -> bytes:
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# Fixture: stub the tool broker's infrastructure (audit/permissions/rate limits)
# but leave the REAL DocConnector executing. Stub only the LLM inside summarize.
# ---------------------------------------------------------------------------

def _patch_broker_infra(monkeypatch):
    """Patch broker's audit, permissions, tool_policy, connector_tier.

    Returns list of audited event types so tests can assert audit presence.
    """
    from core import tool_broker as tb

    audited: list[str] = []

    async def fake_log(event_type, actor, action, **kw):
        audited.append(event_type)

    async def fake_check(*a, **k):
        return True

    async def fake_tool_policy(*a, **k):
        return {}

    monkeypatch.setattr(tb.audit, "log", fake_log)
    monkeypatch.setattr(tb.permissions, "check", fake_check)
    monkeypatch.setattr(tb, "tool_policy", fake_tool_policy)
    monkeypatch.setattr(tb, "connector_tier", AsyncMock(return_value="live"))
    return audited


def _make_agent(org_id: str = "default") -> AgentContext:
    return AgentContext(id="a1", org_id=org_id, task_id="t1", member_id="m1")


def _make_artifact_mocks(monkeypatch, raw: bytes, mime: str, title: str, artifact_id: str = "art-1",
                         org_id: str = "default"):
    """Monkeypatch get_artifact and read_artifact_content inside parsing.tool."""
    import parsing.tool as doctool

    async def fake_get(aid):
        if aid == artifact_id:
            return {"organization_id": org_id, "mime_type": mime, "title": title, "kind": "attachment"}
        return None

    async def fake_read(aid):
        if aid == artifact_id:
            return raw
        return b""

    monkeypatch.setattr(doctool, "get_artifact", fake_get)
    monkeypatch.setattr(doctool, "read_artifact_content", fake_read)


def _stub_llm_summarize(monkeypatch, full_text: str, quote: str):
    """Stub core.llm.complete_json for summarize to return a single section quoting `quote`."""
    import core.llm as llm_mod

    response = json.dumps({
        "sections": [
            {"heading": "Overview", "text": "This document covers key topics.", "quote": quote}
        ]
    })

    async def fake_complete_json(prompt, *, model=None):
        return response

    monkeypatch.setattr(llm_mod, "complete_json", fake_complete_json)


def _stub_llm_compare(monkeypatch, quote_a: str, quote_b: str):
    """Stub core.llm.complete_json for compare to return one item quoting from each doc."""
    import core.llm as llm_mod

    response = json.dumps({
        "items": [
            {
                "type": "similarity",
                "description": "Both documents cover the same subject.",
                "quote_a": quote_a,
                "quote_b": quote_b,
            }
        ]
    })

    async def fake_complete_json(prompt, *, model=None):
        return response

    monkeypatch.setattr(llm_mod, "complete_json", fake_complete_json)


# ---------------------------------------------------------------------------
# Helpers: rate-limit isolation — give each test its own org_id to avoid
# hitting the 10/min per-org rate limit when all tests run together.
# ---------------------------------------------------------------------------

_ORG_COUNTER = iter(range(1000))


def _unique_org() -> str:
    return f"test-org-{next(_ORG_COUNTER)}"


# ---------------------------------------------------------------------------
# Autouse fixture: reset DB engine pool before each test (same as test_doc_parsing)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def dispose_db_engine():
    import core.db as _db
    _db.engine.sync_engine.pool.dispose()
    yield


# ---------------------------------------------------------------------------
# TESTS: doc.summarize via broker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_pdf_through_broker(monkeypatch):
    """PDF: summarize returns cited section whose span matches source text."""
    from core import tool_broker as tb

    org_id = _unique_org()
    audited = _patch_broker_infra(monkeypatch)

    # PDF needs _pdf_page_texts patched (pypdf can't author text-bearing pages).
    source_text = "Quarterly earnings rose by fifteen percent in Q3."
    with patch("parsing.engine._pdf_page_texts", return_value=[source_text]):
        _make_artifact_mocks(monkeypatch, _make_pdf_bytes(), "application/pdf", "report.pdf",
                             artifact_id="pdf-1", org_id=org_id)
        _stub_llm_summarize(monkeypatch, full_text=source_text, quote="earnings rose by fifteen percent")

        agent = _make_agent(org_id)
        result = await tb.execute(agent, "doc.summarize", {"artifact_id": "pdf-1"})

    assert result.data["summary"] is not None
    citations = result.data["citations"]
    assert len(citations) >= 1, "Expected at least one citation"

    # Core proof: span must index the actual quote in the source text
    # (we need to reparse to get full_text for verification)
    for cit in citations:
        start, end, quote = cit["char_start"], cit["char_end"], cit["quote"]
        assert source_text[start:end] == quote, (
            f"Citation span [{start}:{end}] does not match quote {quote!r} in source"
        )
        assert cit["source_artifact_id"] == "pdf-1"

    assert "tool_call" in audited
    assert "tool_result" in audited


@pytest.mark.asyncio
async def test_summarize_docx_through_broker(monkeypatch):
    """DOCX: summarize returns cited section whose span matches source text."""
    from core import tool_broker as tb

    org_id = _unique_org()
    audited = _patch_broker_infra(monkeypatch)

    source_text = "The software license agreement expires on December 31st."
    raw = _make_docx_bytes(source_text)

    _make_artifact_mocks(monkeypatch, raw, "", "agreement.docx", artifact_id="docx-1", org_id=org_id)
    _stub_llm_summarize(monkeypatch, full_text=source_text, quote="license agreement expires")

    agent = _make_agent(org_id)
    result = await tb.execute(agent, "doc.summarize", {"artifact_id": "docx-1"})

    assert result.data["summary"] is not None
    citations = result.data["citations"]
    assert len(citations) >= 1

    # Re-derive full_text by parsing real bytes to validate offsets
    from parsing.engine import parse_document
    doc = await parse_document(raw, "", "agreement.docx")
    for cit in citations:
        start, end, quote = cit["char_start"], cit["char_end"], cit["quote"]
        assert doc.full_text[start:end] == quote, (
            f"DOCX citation span [{start}:{end}] does not match quote {quote!r}"
        )

    assert "tool_call" in audited and "tool_result" in audited


@pytest.mark.asyncio
async def test_summarize_xlsx_through_broker(monkeypatch):
    """XLSX: summarize returns cited section whose span matches source text."""
    from core import tool_broker as tb

    org_id = _unique_org()
    audited = _patch_broker_infra(monkeypatch)

    cell_value = "Revenue Q4 2025"
    raw = _make_xlsx_bytes(cell_value)

    _make_artifact_mocks(monkeypatch, raw, "", "model.xlsx", artifact_id="xlsx-1", org_id=org_id)
    _stub_llm_summarize(monkeypatch, full_text=cell_value, quote="Revenue Q4")

    agent = _make_agent(org_id)
    result = await tb.execute(agent, "doc.summarize", {"artifact_id": "xlsx-1"})

    assert result.data["summary"] is not None
    citations = result.data["citations"]
    assert len(citations) >= 1

    from parsing.engine import parse_document
    doc = await parse_document(raw, "", "model.xlsx")
    for cit in citations:
        start, end, quote = cit["char_start"], cit["char_end"], cit["quote"]
        assert doc.full_text[start:end] == quote, (
            f"XLSX citation span [{start}:{end}] does not match quote {quote!r}"
        )

    assert "tool_call" in audited and "tool_result" in audited


@pytest.mark.asyncio
async def test_summarize_csv_through_broker(monkeypatch):
    """CSV: summarize returns cited section whose span matches source text."""
    from core import tool_broker as tb

    org_id = _unique_org()
    audited = _patch_broker_infra(monkeypatch)

    csv_text = "name,score\nAlice,95\nBob,87\n"
    raw = _make_csv_bytes(csv_text)

    _make_artifact_mocks(monkeypatch, raw, "text/csv", "data.csv", artifact_id="csv-1", org_id=org_id)
    _stub_llm_summarize(monkeypatch, full_text=csv_text, quote="Alice,95")

    agent = _make_agent(org_id)
    result = await tb.execute(agent, "doc.summarize", {"artifact_id": "csv-1"})

    assert result.data["summary"] is not None
    citations = result.data["citations"]
    assert len(citations) >= 1

    for cit in citations:
        start, end, quote = cit["char_start"], cit["char_end"], cit["quote"]
        assert csv_text[start:end] == quote, (
            f"CSV citation span [{start}:{end}] does not match quote {quote!r}"
        )

    assert "tool_call" in audited and "tool_result" in audited


# ---------------------------------------------------------------------------
# TEST: doc.compare — citations into BOTH documents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compare_two_docs_citations_into_both(monkeypatch):
    """doc.compare returns citations pointing into both source documents."""
    from core import tool_broker as tb
    import parsing.tool as doctool

    org_id = _unique_org()
    _patch_broker_infra(monkeypatch)

    text_a = "Contract A covers liability and indemnification clauses."
    text_b = "Contract B outlines warranty terms and service levels."
    raw_a = _make_docx_bytes(text_a)
    raw_b = _make_docx_bytes(text_b)

    quote_a = "liability and indemnification"
    quote_b = "warranty terms"

    async def fake_get(aid):
        if aid == "art-a":
            return {"organization_id": org_id, "mime_type": "", "title": "contract_a.docx", "kind": "attachment"}
        if aid == "art-b":
            return {"organization_id": org_id, "mime_type": "", "title": "contract_b.docx", "kind": "attachment"}
        return None

    async def fake_read(aid):
        if aid == "art-a":
            return raw_a
        if aid == "art-b":
            return raw_b
        return b""

    monkeypatch.setattr(doctool, "get_artifact", fake_get)
    monkeypatch.setattr(doctool, "read_artifact_content", fake_read)
    _stub_llm_compare(monkeypatch, quote_a=quote_a, quote_b=quote_b)

    agent = _make_agent(org_id)
    result = await tb.execute(agent, "doc.compare", {"artifact_id_a": "art-a", "artifact_id_b": "art-b"})

    citations = result.data["citations"]
    assert len(citations) >= 2, "Expected citations into both documents"

    source_ids = {c["source_artifact_id"] for c in citations}
    assert "art-a" in source_ids, "Expected at least one citation into doc A"
    assert "art-b" in source_ids, "Expected at least one citation into doc B"

    # Validate offsets against real parsed text
    from parsing.engine import parse_document
    doc_a_parsed = await parse_document(raw_a, "", "contract_a.docx")
    doc_b_parsed = await parse_document(raw_b, "", "contract_b.docx")
    text_by_id = {"art-a": doc_a_parsed.full_text, "art-b": doc_b_parsed.full_text}
    for cit in citations:
        src = cit["source_artifact_id"]
        start, end, quote = cit["char_start"], cit["char_end"], cit["quote"]
        assert text_by_id[src][start:end] == quote, (
            f"Compare citation span [{start}:{end}] in {src} does not match {quote!r}"
        )


# ---------------------------------------------------------------------------
# TEST: unparseable input → honest warning, no fabricated summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_unparseable_returns_warning_no_fabricated_summary(monkeypatch):
    """Unknown binary type must return honest warning, not a fabricated summary."""
    from core import tool_broker as tb

    org_id = _unique_org()
    _patch_broker_infra(monkeypatch)

    raw = b"\x00\x01\x02\x03unknown binary"
    _make_artifact_mocks(monkeypatch, raw, "application/x-thing", "blob.bin",
                         artifact_id="unknown-1", org_id=org_id)

    # LLM must NOT be called for unparseable docs. If it is, the test fails.
    import core.llm as llm_mod
    llm_called = []

    async def llm_should_not_be_called(*a, **k):
        llm_called.append(True)
        return "{}"

    monkeypatch.setattr(llm_mod, "complete_json", llm_should_not_be_called)

    agent = _make_agent(org_id)
    result = await tb.execute(agent, "doc.summarize", {"artifact_id": "unknown-1"})

    assert result.data["summary"] is None, "Unparseable doc must not produce a summary"
    assert "warning" in result.data
    assert result.data["citations"] == []
    assert result.data["parser_used"] == "none"
    assert not llm_called, "LLM must NOT be called for unparseable documents"


# ---------------------------------------------------------------------------
# TEST: cite span validation — wrong quotes are dropped, not silently passed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_citation_spans_dropped_when_quote_not_in_source(monkeypatch):
    """If model returns a quote that doesn't appear in source, it must be dropped."""
    from parsing.tool import _derive_citations

    full_text = "Real content: cats and dogs are popular pets."
    claims = [
        {"quote": "cats and dogs"},          # exists — should be kept
        {"quote": "unicorns and dragons"},   # not in source — must be dropped
    ]
    citations = _derive_citations(full_text, "art-x", claims)
    assert len(citations) == 1
    assert citations[0]["quote"] == "cats and dogs"
    assert full_text[citations[0]["char_start"]:citations[0]["char_end"]] == "cats and dogs"


# ---------------------------------------------------------------------------
# TEST: tool registry entries exist and convert to broker name
# ---------------------------------------------------------------------------

def test_doc_summarize_compare_registered():
    from runtime.tool_registry import ALL_TOOLS, SUBAGENT_TOOLS, to_broker_name, tool_name

    names = {tool_name(s) for s in ALL_TOOLS}
    assert "doc__summarize" in names
    assert "doc__compare" in names

    sub_names = {tool_name(s) for s in SUBAGENT_TOOLS}
    assert "doc__summarize" in sub_names
    assert "doc__compare" in sub_names

    assert to_broker_name("doc__summarize") == "doc.summarize"
    assert to_broker_name("doc__compare") == "doc.compare"
