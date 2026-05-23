from typing import Any

import asyncio
import litellm

from core.config import settings


def available_chat_models() -> list[dict[str, str]]:
    models = [
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
    requested = model_id or "auto"
    allowed = {model["id"] for model in available_chat_models()}
    return requested if requested in allowed else "auto"


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


def model_kwargs(model: str, *, messages: list[dict[str, str]], stream: bool = False) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
    if model.startswith("openrouter/") or settings.openrouter_api_key:
        kwargs["api_key"] = settings.openrouter_api_key
        kwargs["api_base"] = settings.openrouter_api_base
    return kwargs


def selected_completion_kwargs(model_id: str, messages: list[dict[str, str]], *, stream: bool = False) -> dict[str, Any]:
    selected = normalize_chat_model(model_id)
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
    """Complete with JSON response format, falling back through fast → main → error."""
    messages = [{"role": "user", "content": prompt}]

    # Try fast model first
    fast = model or settings.fast_model
    try:
        kwargs = model_kwargs(fast, messages=messages, stream=False)
        kwargs["response_format"] = {"type": "json_object"}
        response = await litellm.acompletion(**kwargs)
        return _message_content(response)
    except Exception:
        pass

    # Fast model failed (rate limit, misconfiguration, etc.) — fall back to main model
    # Don't use response_format since some models don't support it; rely on prompt instruction
    if fast != settings.openrouter_model:
        try:
            kwargs = backup_completion_kwargs(messages, stream=False)
            response = await litellm.acompletion(**kwargs)
            return _message_content(response)
        except Exception:
            pass

    raise RuntimeError("All models failed for complete_json — check OPENROUTER_API_KEY and model config")


async def complete_text(prompt: str, *, model: str | None = None) -> str:
    """Complete with plain text response, falling back through fast → main → error."""
    messages = [{"role": "user", "content": prompt}]

    fast = model or settings.fast_model
    try:
        response = await litellm.acompletion(
            **model_kwargs(fast, messages=messages, stream=False)
        )
        return _message_content(response)
    except Exception:
        pass

    if fast != settings.openrouter_model:
        try:
            response = await litellm.acompletion(**backup_completion_kwargs(messages, stream=False))
            return _message_content(response)
        except Exception:
            pass

    raise RuntimeError("All models failed for complete_text — check OPENROUTER_API_KEY and model config")
