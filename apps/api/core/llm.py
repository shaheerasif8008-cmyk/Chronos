from typing import Any

import litellm

from core.config import settings


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


def _choice_delta_content(chunk: Any) -> str:
    if isinstance(chunk, dict):
        return chunk.get("choices", [{}])[0].get("delta", {}).get("content") or ""
    return chunk.choices[0].delta.content or ""


def _message_content(response: Any) -> str:
    if isinstance(response, dict):
        return response.get("choices", [{}])[0].get("message", {}).get("content") or ""
    return response.choices[0].message.content or ""


async def stream_completion(messages: list[dict[str, str]]):
    try:
        stream = await litellm.acompletion(
            model=settings.local_llm_model,
            api_base=settings.local_llm_base_url,
            messages=messages,
            stream=True,
        )
    except Exception:
        stream = await litellm.acompletion(**backup_completion_kwargs(messages, stream=True))

    async for chunk in stream:
        token = _choice_delta_content(chunk)
        if token:
            yield token


async def complete_json(prompt: str, *, model: str | None = None) -> str:
    messages = [{"role": "user", "content": prompt}]
    kwargs = model_kwargs(model or settings.fast_model, messages=messages, stream=False)
    kwargs["response_format"] = {"type": "json_object"}
    response = await litellm.acompletion(**kwargs)
    return _message_content(response)


async def complete_text(prompt: str, *, model: str | None = None) -> str:
    messages = [{"role": "user", "content": prompt}]
    response = await litellm.acompletion(
        **model_kwargs(model or settings.fast_model, messages=messages, stream=False)
    )
    return _message_content(response)
