from __future__ import annotations
import base64
import json
import logging
import random
from typing import Any

import asyncio
import litellm

from core.config import settings

logger = logging.getLogger(__name__)

# Retry/backoff configuration (Category 10).
_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

CHAT_MODEL_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "gpt-5.4-mini",
        "label": "GPT-5.4 Mini",
        "model": "openrouter/openai/gpt-5.4-mini",
        "description": "OpenAI GPT-5.4 Mini through OpenRouter.",
        "capabilities": ["chat", "tool_use", "agent_loop", "structured_json"],
        "tool_use": True,
        "fallback_for": [],
        "policy": "Selected explicitly by the user in chat.",
    },
    {
        "id": "gpt-5.4-nano",
        "label": "GPT-5.4 Nano",
        "model": "openrouter/openai/gpt-5.4-nano",
        "description": "OpenAI GPT-5.4 Nano through OpenRouter.",
        "capabilities": ["chat", "tool_use", "agent_loop", "structured_json"],
        "tool_use": True,
        "fallback_for": [],
        "policy": "Selected explicitly by the user in chat.",
    },
    {
        "id": "deepseek-v4-pro",
        "label": "DeepSeek V4 Pro",
        "model": "openrouter/deepseek/deepseek-v4-pro",
        "description": "DeepSeek V4 Pro through OpenRouter.",
        "capabilities": ["chat", "tool_use", "agent_loop", "structured_json"],
        "tool_use": True,
        "fallback_for": [],
        "policy": "Selected explicitly by the user in chat.",
    },
    {
        "id": "deepseek-v4-flash",
        "label": "DeepSeek V4 Flash",
        "model": "openrouter/deepseek/deepseek-v4-flash",
        "description": "DeepSeek V4 Flash through OpenRouter.",
        "capabilities": ["chat", "tool_use", "agent_loop", "structured_json"],
        "tool_use": True,
        "fallback_for": [],
        "policy": "Selected explicitly by the user in chat.",
    },
)


async def _with_retry(coro_factory, *, max_retries: int = _MAX_RETRIES) -> Any:
    """Execute an async callable with exponential backoff on retryable errors.

    coro_factory: a zero-argument async callable that returns the coroutine to retry.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except litellm.RateLimitError as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = _BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, 0.5)
            logger.warning("LLM rate limit (attempt %d/%d), retrying in %.1fs", attempt + 1, max_retries, delay)
            await asyncio.sleep(delay)
        except Exception as exc:
            # Non-retryable error — raise immediately.
            raise
    raise last_exc  # type: ignore[misc]


def _model_status(*, configured: bool, preferred_live: bool = False) -> str:
    if configured and preferred_live:
        return "available"
    if configured:
        return "degraded"
    return "unconfigured"


def available_chat_models() -> list[dict[str, Any]]:
    return [
        {
            **model,
            "status": _model_status(configured=bool(model["model"]), preferred_live=bool(settings.openrouter_api_key)),
        }
        for model in CHAT_MODEL_OPTIONS
    ]


def normalize_chat_model(model_id: str | None) -> str:
    if not model_id:
        raise ValueError("model is required")
    requested = model_id
    allowed = {model["id"] for model in available_chat_models()}
    if requested not in allowed:
        raise ValueError(f"unknown chat model: {requested}")
    return requested


def default_chat_model_id() -> str:
    return CHAT_MODEL_OPTIONS[0]["id"]


def chat_model_string(model_id: str) -> str:
    selected = normalize_chat_model(model_id)
    by_id = {model["id"]: model["model"] for model in CHAT_MODEL_OPTIONS}
    return by_id[selected]


def resolve_agent_model(model_id: str | None) -> str:
    """Map the chat selector id to the concrete litellm model string."""
    return chat_model_string(normalize_chat_model(model_id))


def backup_completion_kwargs(messages: list[dict[str, str]], *, stream: bool = False) -> dict[str, Any]:
    if settings.openrouter_api_key:
        return {
            "model": settings.openrouter_model,
            "api_key": settings.openrouter_api_key,
            "api_base": settings.openrouter_api_base,
            "messages": messages,
            "stream": stream,
        }
    return {
        "model": settings.backup_model,
        "api_key": settings.backup_api_key,
        "messages": messages,
        "stream": stream,
    }


def agent_completion_kwargs(messages: list[dict[str, str]], *, stream: bool = False) -> dict[str, Any]:
    return model_kwargs(settings.agent_model, messages=messages, stream=stream)


def model_kwargs(model: str, *, messages: list[dict[str, str]], stream: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
    if model.startswith("openrouter/") or settings.openrouter_api_key:
        kwargs["api_key"] = settings.openrouter_api_key
        kwargs["api_base"] = settings.openrouter_api_base
    return kwargs


def selected_completion_kwargs(model_id: str, messages: list[dict[str, str]], *, stream: bool = False) -> dict[str, Any]:
    return model_kwargs(chat_model_string(model_id), messages=messages, stream=stream)


def _choice_delta_content(chunk: Any) -> str:
    if isinstance(chunk, dict):
        return chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
    return chunk.choices[0].delta.content or ""


def _message_content(response: Any) -> str:
    if isinstance(response, dict):
        return response.get("choices", [{}])[0].get("message", {}).get("content") or ""
    return response.choices[0].message.content or ""


def _message(response: Any) -> Any:
    if isinstance(response, dict):
        return response.get("choices", [{}])[0].get("message", {})
    return response.choices[0].message


def _tool_calls(message: Any) -> list[Any]:
    if isinstance(message, dict):
        return message.get("tool_calls") or []
    return getattr(message, "tool_calls", None) or []


def _delta(chunk: Any) -> Any:
    if isinstance(chunk, dict):
        return chunk.get("choices", [{}])[0].get("delta", {})
    return chunk.choices[0].delta


def _delta_content(delta: Any) -> str:
    if isinstance(delta, dict):
        return delta.get("content") or ""
    return getattr(delta, "content", None) or ""


def _delta_tool_calls(delta: Any) -> list[Any]:
    if isinstance(delta, dict):
        return delta.get("tool_calls") or []
    return getattr(delta, "tool_calls", None) or []


def _frag_fields(frag: Any) -> tuple[int, str | None, str | None, str]:
    if isinstance(frag, dict):
        fn = frag.get("function") or {}
        return int(frag.get("index") or 0), frag.get("id"), fn.get("name"), fn.get("arguments") or ""
    fn = getattr(frag, "function", None)
    return (
        int(getattr(frag, "index", 0) or 0),
        getattr(frag, "id", None),
        getattr(fn, "name", None),
        getattr(fn, "arguments", "") or "",
    )


async def stream_step(messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str):
    kwargs = model_kwargs(model, messages=messages, stream=True)
    kwargs["tools"] = tools
    kwargs["tool_choice"] = "auto"
    stream = await _with_retry(lambda: litellm.acompletion(**kwargs))

    text_parts: list[str] = []
    acc: dict[int, dict[str, Any]] = {}
    async for chunk in stream:
        delta = _delta(chunk)
        content = _delta_content(delta)
        if content:
            text_parts.append(content)
            yield {"type": "token", "content": content}
        for frag in _delta_tool_calls(delta):
            idx, fid, name, args = _frag_fields(frag)
            slot = acc.setdefault(idx, {"id": None, "name": None, "args": ""})
            if fid:
                slot["id"] = fid
            if name:
                slot["name"] = name
            slot["args"] += args

    if acc:
        calls = [
            {"id": s["id"] or f"call_{i}", "name": s["name"] or "", "args_str": s["args"] or "{}"}
            for i, s in sorted(acc.items())
        ]
        yield {"type": "tool_calls", "calls": calls}
    else:
        yield {"type": "text_done", "text": "".join(text_parts)}


async def stream_completion(messages: list[dict[str, str]], *, model_id: str | None = None):
    selected = normalize_chat_model(model_id)
    try:
        stream = await litellm.acompletion(**selected_completion_kwargs(selected, messages, stream=True))
    except Exception:
        yield "Chronos is connected, but the selected AI provider is unavailable right now. Choose another model in the selector."
        return

    async for chunk in stream:
        token = _choice_delta_content(chunk)
        if token:
            yield token


async def stream_step(
    history: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    model: str,
):
    """Stream a single agent-loop LLM step, yielding typed events.

    Yields:
        {"type": "token", "content": str}      — each streamed text chunk
        {"type": "text_done", "text": str}     — full text when no tool calls
        {"type": "tool_calls", "calls": list}  — normalised tool call dicts
    """
    kwargs = model_kwargs(model, messages=history, stream=True)
    kwargs["tools"] = tools
    kwargs["tool_choice"] = "auto"

    stream = await _with_retry(lambda: litellm.acompletion(**kwargs))

    full_text = ""
    raw_tool_calls: dict[int, dict[str, Any]] = {}

    async for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if choice is None:
            continue
        delta = choice.delta if hasattr(choice, "delta") else {}

        # Accumulate text tokens
        token = (delta.content if hasattr(delta, "content") else None) or ""
        if token:
            full_text += token
            yield {"type": "token", "content": token}

        # Accumulate streaming tool call fragments
        tc_deltas = (delta.tool_calls if hasattr(delta, "tool_calls") else None) or []
        for tc in tc_deltas:
            idx = tc.index if hasattr(tc, "index") else 0
            if idx not in raw_tool_calls:
                raw_tool_calls[idx] = {"id": "", "name": "", "args_str": ""}
            entry = raw_tool_calls[idx]
            if hasattr(tc, "id") and tc.id:
                entry["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn:
                if getattr(fn, "name", None):
                    entry["name"] += fn.name
                if getattr(fn, "arguments", None):
                    entry["args_str"] += fn.arguments

    if raw_tool_calls:
        calls = [
            {
                "id": v["id"] or f"call_{i}",
                "name": v["name"],
                "args_str": v["args_str"] or "{}",
            }
            for i, v in sorted(raw_tool_calls.items())
        ]
        yield {"type": "tool_calls", "calls": calls}
    else:
        yield {"type": "text_done", "text": full_text}


async def complete_json(prompt: str, *, model: str | None = None) -> str:
    """Complete with JSON response format, falling back through selected → agent → error.

    Retries on rate-limit errors with exponential backoff before falling back.
    """
    messages = [{"role": "user", "content": prompt}]
    selected = model or settings.fast_model

    try:
        kwargs = model_kwargs(selected, messages=messages, stream=False)
        kwargs["response_format"] = {"type": "json_object"}
        response = await _with_retry(lambda: litellm.acompletion(**kwargs), max_retries=0)
        return _message_content(response)
    except Exception:
        pass

    # Selected model failed — fall back to primary agent model.
    # Omit response_format since some providers don't support it; rely on prompt instruction.
    if selected != settings.agent_model:
        try:
            kwargs = agent_completion_kwargs(messages, stream=False)
            response = await _with_retry(lambda: litellm.acompletion(**kwargs))
            return _message_content(response)
        except Exception:
            pass

    raise RuntimeError("All models failed for complete_json — check OPENROUTER_API_KEY and model config")


async def complete_text(prompt: str, *, model: str | None = None) -> str:
    """Complete with plain text response, falling back through selected → agent → error."""
    messages = [{"role": "user", "content": prompt}]
    selected = model or settings.fast_model

    try:
        kwargs = model_kwargs(selected, messages=messages, stream=False)
        response = await _with_retry(lambda: litellm.acompletion(**kwargs))
        return _message_content(response)
    except Exception:
        pass

    if selected != settings.agent_model:
        try:
            kwargs = agent_completion_kwargs(messages, stream=False)
            response = await _with_retry(lambda: litellm.acompletion(**kwargs))
            return _message_content(response)
        except Exception:
            pass

    raise RuntimeError("All models failed for complete_text — check OPENROUTER_API_KEY and model config")


async def vision_ocr(image_bytes: bytes, mime: str) -> str:
    """Extract text from an image via a litellm vision model.

    Returns "" when no vision model is configured or the call fails — OCR is
    best-effort and must never raise into a chat turn or task step.
    """
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


async def tool_call(messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, model: str | None = None) -> dict[str, Any]:
    """Ask the agent model for the next tool call or final response.

    Uses retry/backoff on rate-limit errors before giving up.
    """
    selected = model or settings.agent_model or settings.openrouter_model
    kwargs = model_kwargs(selected, messages=messages, stream=False)
    kwargs["tools"] = tools
    kwargs["tool_choice"] = "auto"
    response = await _with_retry(lambda: litellm.acompletion(**kwargs))
    message = _message(response)
    calls = _tool_calls(message)
    if calls:
        call = calls[0]
        if isinstance(call, dict):
            function = call.get("function") or {}
            name = function.get("name")
            raw_args = function.get("arguments") or "{}"
        else:
            function = call.function
            name = function.name
            raw_args = function.arguments or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args = {}
        return {"type": "tool_call", "tool": name, "args": args if isinstance(args, dict) else {}}

    content = _message_content(response)
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and parsed.get("type") in {"final", "tool_call"}:
            return parsed
    except Exception:
        pass
    return {"type": "final", "result": {"answer": content}}
