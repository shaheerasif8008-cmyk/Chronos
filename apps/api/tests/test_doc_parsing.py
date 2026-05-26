import base64
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
