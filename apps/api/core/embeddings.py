import hashlib
import json

import httpx
import litellm

from core.config import settings
from core.redis import redis_client

_redis = redis_client
EMBEDDING_TTL_SECONDS = 24 * 60 * 60


async def embed(text: str) -> list[float]:
    cache_key = hashlib.sha256(text.encode()).hexdigest()
    cached = await _redis.get(cache_key)
    if cached:
        return [float(value) for value in json.loads(cached)]

    if settings.embedding_model.startswith("openrouter/") or settings.openrouter_api_key:
        response = await _openrouter_embedding(text)
    else:
        response = await litellm.aembedding(model=settings.embedding_model, input=[text])
    if isinstance(response, dict):
        vector = response["data"][0]["embedding"]
    else:
        vector = response.data[0]["embedding"]
    await _redis.setex(cache_key, EMBEDDING_TTL_SECONDS, json.dumps(vector))
    return [float(value) for value in vector]


async def _openrouter_embedding(text: str) -> dict:
    model = settings.embedding_model.removeprefix("openrouter/")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            f"{settings.openrouter_api_base.rstrip('/')}/embeddings",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.frontend_base_url.rstrip("/"),
                "X-Title": "Chronos",
            },
            json={"model": model, "input": text, "dimensions": settings.embedding_dimensions},
        )
        response.raise_for_status()
        return response.json()
