from __future__ import annotations

"""Image generation connector — routes through tool_broker.execute only.

Provider abstraction: all provider calls go through _call_provider(), which is
a single swappable async function. Tests stub it via monkeypatch. The connector
itself is a pure provider wrapper: it calls the provider, saves the resulting
image(s) as artifacts, and returns artifact ids + metadata. It does NOT contain
business logic, rate limiting, or permissions — those all live in the broker.

Choice: the connector saves artifacts (rather than returning raw bytes in
ToolResult.data) because ToolResult.data is not designed to carry binary blobs
and returning a list of artifact ids keeps the caller interface clean and
consistent with how other connectors that produce durable outputs behave.
Generation metadata (prompt, size, count, provider, model) is stored in the
artifact title and returned in ToolResult.data — no schema migration required.
"""

from typing import Any

from core.config import settings
from core.models import ToolResult

_DEFAULT_SIZE = "1024x1024"
_DEFAULT_COUNT = 1
_MAX_COUNT = 4  # enforced by _check_safety_limits in broker; also capped here


async def _call_provider(prompt: str, size: str, count: int, style: str | None = None) -> list[bytes]:
    """Call the configured image provider and return a list of raw image bytes.

    This function is the single stubbable seam for provider I/O.  In tests,
    monkeypatch ``connectors.image_gen._call_provider`` to return deterministic
    bytes without making real network calls.

    Args:
        prompt: Text description of the image(s) to generate.
        size: Image dimensions string (e.g. "1024x1024").
        count: Number of images to generate (1–4).
        style: Optional provider style hint (forwarded only when set).

    Returns:
        A list of raw PNG/JPEG image bytes, one entry per generated image.

    Raises:
        RuntimeError: When the provider returns an unexpected response shape.
    """
    import litellm  # type: ignore[import]

    # litellm exposes image_generation() which mirrors the OpenAI Images API.
    # n (count), size, and model map directly to the litellm call.
    extra: dict[str, Any] = {"style": style} if style else {}
    response = await litellm.aimage_generation(
        model=settings.image_model,
        prompt=prompt,
        n=count,
        size=size,
        **extra,
    )
    # Each item has a b64_json or url field.
    blobs: list[bytes] = []
    for item in response.data:
        if hasattr(item, "b64_json") and item.b64_json:
            import base64
            blobs.append(base64.b64decode(item.b64_json))
        elif hasattr(item, "url") and item.url:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(item.url)
                r.raise_for_status()
                blobs.append(r.content)
        else:
            raise RuntimeError(f"image_generation response item has neither b64_json nor url: {item!r}")
    return blobs


class ImageGenConnector:
    """Connector for ``image.generate`` — the only tool in this provider."""

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute an image generation tool call.

        Args:
            tool: Must be "image.generate".
            args: Tool arguments, including broker-injected keys
                ``__connector_tier``, ``__org_id``, ``__task_id``.

        Returns:
            ToolResult with data containing artifact ids, generation metadata,
            and an honest status.  When no image provider is configured, returns
            an "unavailable" ToolResult rather than raising.
        """
        args.pop("__connector_tier", None)
        org_id: str = str(args.pop("__org_id", "default") or "default")
        task_id: str | None = args.pop("__task_id", None)

        if tool != "image.generate":
            raise ValueError(f"Unknown image tool: {tool}")

        prompt: str = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(
                data={"status": "error", "reason": "prompt is required"},
                summary="image.generate: prompt is required",
            )

        size: str = str(args.get("size") or _DEFAULT_SIZE)
        count: int = min(int(args.get("count") or _DEFAULT_COUNT), _MAX_COUNT)
        style: str | None = args.get("style") or None

        # Honest degraded leaf: no provider configured.
        if not settings.image_model:
            return ToolResult(
                data={
                    "status": "unavailable",
                    "fallback_reason": "no image provider configured",
                    "images": [],
                },
                summary=(
                    "Image generation is not available: no image provider is configured. "
                    "Set IMAGE_MODEL in your environment to enable image generation."
                ),
            )

        # Call the provider (stubbed in tests via monkeypatch). A provider/network
        # error must degrade honestly into an error ToolResult — never propagate and
        # break the broker call / SSE stream.
        try:
            image_bytes_list = await _call_provider(prompt, size, count, style)
        except Exception as exc:
            return ToolResult(
                data={"status": "error", "reason": f"provider call failed: {type(exc).__name__}"},
                summary=f"Image generation failed: {exc}",
            )

        # An empty provider response (e.g. content-policy suppression) is NOT a
        # success — distinguish it honestly so the caller doesn't show "0 images" as ok.
        if not image_bytes_list:
            return ToolResult(
                data={"status": "error", "reason": "provider returned no images", "artifact_ids": []},
                summary="Image generation produced no images (provider returned an empty result).",
            )

        # Save each generated image as an artifact.
        from core.artifacts import save_artifact

        artifact_ids: list[str] = []
        for i, image_bytes in enumerate(image_bytes_list):
            suffix = f" ({i + 1}/{len(image_bytes_list)})" if len(image_bytes_list) > 1 else ""
            title = f"Generated: {prompt[:60]}{suffix}"
            artifact_id = await save_artifact(
                image_bytes,
                kind="image",
                title=title,
                task_id=task_id,
                org_id=org_id,
                mime_type="image/png",
                created_by="image_gen_connector",
            )
            artifact_ids.append(artifact_id)

        generation_meta: dict[str, Any] = {
            "prompt": prompt,
            "size": size,
            "count": count,
            "model": settings.image_model,
            "provider": settings.image_model.split("/")[0] if "/" in settings.image_model else settings.image_model,
        }
        if style:
            generation_meta["style"] = style

        return ToolResult(
            data={
                "status": "success",
                "artifact_ids": artifact_ids,
                "count": len(artifact_ids),
                "generation_meta": generation_meta,
            },
            summary=(
                f"Generated {len(artifact_ids)} image(s) for prompt: {prompt[:80]!r}"
            ),
        )


image_gen_connector = ImageGenConnector()
