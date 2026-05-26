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
