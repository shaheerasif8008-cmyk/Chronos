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


def available_chat_models() -> list[dict[str, str]]:
    models = [
        {
            "id": "agent",
            "label": "Primary",
            "model": settings.agent_model,
            "description": "Use the configured primary agent model.",
        },
        {
            "id": "auto",
            "label": "Auto",
            "model": settings.local_llm_model,
            "description": "Try local first, then fallback provider.",
        },
        {
            "id": "local",
            "label": "Local",
            "model": settings.local_llm_model,
            "description": "Use the configured local model.",
        },
    ]
    if settings.openrouter_api_key:
        models.append(
            {
                "id": "openrouter",
                "label": "OpenRouter",
                "model": settings.openrouter_model,
                "description": "Use the configured OpenRouter chat model.",
            }
        )
    elif settings.backup_api_key:
        models.append(
            {
                "id": "backup",
                "label": "Backup",
                "model": settings.backup_model,
                "description": "Use the configured backup model.",
            }
        )
    models.append(
        {
            "id": "fast",
            "label": "Fast",
            "model": settings.fast_model,
            "description": "Use the configured fast model.",
        }
    )
    return models


def normalize_chat_model(model_id: str | None) -> str:
    requested = model_id or "agent"
    allowed = {model["id"] for model in available_chat_models()}
    return requested if requested in allowed else "agent"


def resolve_agent_model(model_id: str | None) -> str:
    """Map a chat-model id (agent/fast/local/openrouter/backup/auto) to the
    concrete litellm model string the agent loop should use for tool calling.
    """
    mid = normalize_chat_model(model_id)
    if mid == "fast":
        return settings.fast_model
    if mid == "local":
        return settings.local_llm_model
    if mid in {"openrouter", "backup"}:
        return settings.openrouter_model if settings.openrouter_api_key else settings.backup_model
    # "agent" and "auto" both resolve to the primary agent model (tool-capable).
    return settings.agent_model


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
    selected = normalize_chat_model(model_id)
    if selected == "agent":
        return agent_completion_kwargs(messages, stream=stream)
    if selected == "local":
        return {
            "model": settings.local_llm_model,
            "api_base": settings.local_llm_base_url,
            "messages": messages,
            "stream": stream,
        }
    if selected in {"openrouter", "backup"}:
        return backup_completion_kwargs(messages, stream=stream)
    if selected == "fast":
        return model_kwargs(settings.fast_model, messages=messages, stream=stream)
    raise ValueError("auto does not map to one completion request")


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


async def stream_completion(messages: list[dict[str, str]], *, model_id: str | None = None):
    selected = normalize_chat_model(model_id)
    if selected != "auto":
        try:
            request = litellm.acompletion(**selected_completion_kwargs(selected, messages, stream=True))
            if selected == "local":
                stream = await asyncio.wait_for(request, timeout=settings.local_llm_timeout_seconds)
            else:
                stream = await request
        except Exception:
            yield "Chronos is connected, but the selected AI provider is unavailable right now. Try Auto or another model."
            return
        async for chunk in stream:
            token = _choice_delta_content(chunk)
            if token:
                yield token
        return

    try:
        stream = await asyncio.wait_for(
            litellm.acompletion(
                model=settings.local_llm_model,
                api_base=settings.local_llm_base_url,
                messages=messages,
                stream=True,
            ),
            timeout=settings.local_llm_timeout_seconds,
        )
    except (Exception, asyncio.TimeoutError):
        try:
            stream = await litellm.acompletion(**backup_completion_kwargs(messages, stream=True))
        except Exception:
            yield "Chronos is connected, but the AI provider is unavailable right now. The local runtime, memory, task, approval, and connector tools are still available."
            return

    async for chunk in stream:
        token = _choice_delta_content(chunk)
        if token:
            yield token


async def complete_json(prompt: str, *, model: str | None = None) -> str:
    """Complete with JSON response format, falling back through selected → agent → error.

    Retries on rate-limit errors with exponential backoff before falling back.
    """
    messages = [{"role": "user", "content": prompt}]
    selected = model or settings.fast_model

    try:
        kwargs = model_kwargs(selected, messages=messages, stream=False)
        kwargs["response_format"] = {"type": "json_object"}
        response = await _with_retry(lambda: litellm.acompletion(**kwargs))
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
