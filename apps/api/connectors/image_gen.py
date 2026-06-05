from __future__ import annotations

"""Image generation and editing connector — routes through tool_broker.execute only.

Provider abstraction: all provider calls go through _call_provider() and
_call_edit_provider(), which are single swappable async functions. Tests stub
them via monkeypatch. The connector itself is a pure provider wrapper: it calls
the provider, saves the resulting image(s) as artifacts or new artifact versions,
and returns artifact ids + metadata. It does NOT contain business logic, rate
limiting, or permissions — those all live in the broker.

Choice: the connector saves artifacts (rather than returning raw bytes in
ToolResult.data) because ToolResult.data is not designed to carry binary blobs
and returning a list of artifact ids keeps the caller interface clean and
consistent with how other connectors that produce durable outputs behave.
Generation metadata (prompt, size, count, provider, model) is stored in the
artifact title and returned in ToolResult.data — no schema migration required.

image.edit is non-destructive: it calls create_version() on the source artifact,
preserving all prior version bytes. The source bytes remain readable at their
original version number.
"""

import re
from typing import Any

from core.config import settings
from core.models import ToolResult

_DEFAULT_SIZE = "1024x1024"
_DEFAULT_COUNT = 1
_MAX_COUNT = 4  # enforced by _check_safety_limits in broker; also capped here

#: UUID heuristic for distinguishing a mask artifact id from inline base64 mask data.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


async def _call_edit_provider(
    image_bytes: bytes,
    prompt: str,
    mask: bytes | None = None,
    operation: str = "edit",
) -> list[bytes]:
    """Call the configured image provider to edit an existing image and return result bytes.

    This function is the single stubbable seam for image-edit provider I/O. In tests,
    monkeypatch ``connectors.image_gen._call_edit_provider`` to return deterministic
    bytes without making real network calls.

    Args:
        image_bytes: Raw bytes of the source image to edit.
        prompt: Text instruction describing the desired edit.
        mask: Optional raw bytes of a mask image (transparent areas indicate regions
            to edit). Pass None for a full-image edit.
        operation: Edit operation hint ("edit", "variation", "background"). Forwarded
            to the provider where supported.

    Returns:
        A list of raw PNG/JPEG image bytes (typically one) for the edited image.

    Raises:
        RuntimeError: When the provider returns an unexpected response shape.
    """
    import base64
    import litellm  # type: ignore[import]

    b64_image = base64.b64encode(image_bytes).decode()
    # litellm image_generation with an image parameter performs an edit/variation.
    # The exact API surface depends on the underlying provider (OpenAI edits, DALL·E,
    # stability, etc.).  We pass what we have and let litellm map it.
    extra: dict[str, Any] = {}
    if mask is not None:
        extra["mask"] = base64.b64encode(mask).decode()
    if operation and operation != "edit":
        extra["operation"] = operation

    response = await litellm.aimage_generation(
        model=settings.image_model,
        prompt=prompt,
        image=b64_image,
        n=1,
        **extra,
    )
    blobs: list[bytes] = []
    for item in response.data:
        if hasattr(item, "b64_json") and item.b64_json:
            blobs.append(base64.b64decode(item.b64_json))
        elif hasattr(item, "url") and item.url:
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(item.url)
                r.raise_for_status()
                blobs.append(r.content)
        else:
            raise RuntimeError(f"image edit response item has neither b64_json nor url: {item!r}")
    return blobs


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
    """Connector for ``image.generate`` and ``image.edit`` tools."""

    async def execute(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute an image tool call (generate or edit).

        Args:
            tool: "image.generate" or "image.edit".
            args: Tool arguments, including broker-injected keys
                ``__connector_tier``, ``__org_id``, ``__task_id``.

        Returns:
            ToolResult with data containing artifact ids / version metadata,
            and an honest status.  When no image provider is configured, returns
            an "unavailable" ToolResult rather than raising.
        """
        args.pop("__connector_tier", None)
        org_id: str = str(args.pop("__org_id", "default") or "default")
        task_id: str | None = args.pop("__task_id", None)

        if tool == "image.generate":
            return await self._execute_generate(args, org_id=org_id, task_id=task_id)
        elif tool == "image.edit":
            return await self._execute_edit(args, org_id=org_id, task_id=task_id)
        else:
            raise ValueError(f"Unknown image tool: {tool}")

    async def _execute_generate(
        self, args: dict[str, Any], *, org_id: str, task_id: str | None
    ) -> ToolResult:
        """Handle ``image.generate``.

        Args:
            args: Tool arguments (prompt, size, count, style).
            org_id: Tenant scope injected by the broker.
            task_id: Current task id injected by the broker (may be None).

        Returns:
            ToolResult with generated artifact ids or an honest degraded result.
        """

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

    async def _execute_edit(
        self, args: dict[str, Any], *, org_id: str, task_id: str | None
    ) -> ToolResult:
        """Handle ``image.edit`` — non-destructive: writes a new artifact version.

        Validates arguments, resolves and org-checks the source artifact, reads its
        bytes, calls the edit provider, and persists the result as a new version of
        the source artifact via ``create_version()``. The original bytes remain
        accessible at their prior version number.

        Args:
            args: Tool arguments (artifact_id, prompt, optional mask, optional
                operation).
            org_id: Tenant scope injected by the broker.
            task_id: Current task id injected by the broker (may be None).

        Returns:
            ToolResult with the source artifact_id, new version number, edit
            params, and an honest status; or an "unavailable"/"error" result when
            the provider is unconfigured or fails.
        """
        artifact_id: str = str(args.get("artifact_id") or "").strip()
        if not artifact_id:
            return ToolResult(
                data={"status": "error", "reason": "artifact_id is required"},
                summary="image.edit: artifact_id is required",
            )

        prompt: str = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(
                data={"status": "error", "reason": "prompt is required"},
                summary="image.edit: prompt is required",
            )

        operation: str = str(args.get("operation") or "edit")

        # Honest degraded leaf: no provider configured — skip before any DB I/O.
        if not settings.image_model:
            return ToolResult(
                data={
                    "status": "unavailable",
                    "fallback_reason": "no image provider configured",
                },
                summary=(
                    "Image editing is not available: no image provider is configured. "
                    "Set IMAGE_MODEL in your environment to enable image editing."
                ),
            )

        # Resolve and org-check the source artifact.
        from core.artifacts import get_artifact, read_artifact_content

        meta = await get_artifact(artifact_id)
        if meta is None or meta.get("is_deleted"):
            return ToolResult(
                data={"status": "error", "reason": "artifact not found"},
                summary=f"image.edit: artifact {artifact_id!r} not found",
            )
        if str(meta.get("organization_id", "")) != str(org_id):
            return ToolResult(
                data={"status": "error", "reason": "artifact not found"},
                summary=f"image.edit: artifact {artifact_id!r} not found in this organization",
            )

        source_bytes = await read_artifact_content(artifact_id)
        if not source_bytes:
            return ToolResult(
                data={"status": "error", "reason": "artifact content could not be read"},
                summary=f"image.edit: could not read content for artifact {artifact_id!r}",
            )

        # Resolve optional mask — may be an artifact id or raw base64 string.
        mask_bytes: bytes | None = None
        mask_raw: str | None = args.get("mask") or None
        if mask_raw:
            mask_bytes = await self._resolve_mask(mask_raw, org_id)

        # Call the provider (stubbed in tests via monkeypatch). A provider/network
        # error must degrade honestly into an error ToolResult — never propagate.
        try:
            edited_list = await _call_edit_provider(source_bytes, prompt, mask=mask_bytes, operation=operation)
        except Exception as exc:
            return ToolResult(
                data={"status": "error", "reason": f"provider call failed: {type(exc).__name__}"},
                summary=f"Image editing failed: {exc}",
            )

        if not edited_list:
            return ToolResult(
                data={"status": "error", "reason": "provider returned no edited image"},
                summary="Image editing produced no output (provider returned an empty result).",
            )

        edited_bytes = edited_list[0]

        # Persist as a NEW version of the source artifact (non-destructive).
        model_name: str = settings.image_model
        provider_name: str = model_name.split("/")[0] if "/" in model_name else model_name
        edit_summary = f"AI edit ({operation}): {prompt[:120]}"

        from core.artifact_versions import create_version

        try:
            updated_head = await create_version(
                artifact_id,
                edited_bytes,
                org_id=org_id,
                mime_type="image/png",
                edit_summary=edit_summary,
                created_by="image_gen_connector",
            )
        except Exception as exc:
            # create_version can raise ValueError (not found / TOCTOU delete), RuntimeError
            # (version contention retry exhaustion), or DB errors. None of these may
            # propagate into the broker/SSE stream — degrade honestly.
            return ToolResult(
                data={"status": "error", "reason": f"version creation failed: {type(exc).__name__}"},
                summary=f"Image editing failed to save version: {exc}",
            )

        edit_meta: dict[str, Any] = {
            "prompt": prompt,
            "operation": operation,
            "model": model_name,
            "provider": provider_name,
        }

        return ToolResult(
            data={
                "status": "success",
                "artifact_id": artifact_id,
                "version": updated_head["version"],
                "edit_meta": edit_meta,
            },
            summary=(
                f"Edited image artifact {artifact_id!r} (v{updated_head['version']}): {prompt[:80]!r}"
            ),
        )

    async def _resolve_mask(self, mask_raw: str, org_id: str) -> bytes | None:
        """Resolve a mask argument to raw bytes.

        The mask may be:
        - An artifact id (UUID-like string) — reads the artifact bytes after an
          org check (never crosses tenant boundary).
        - A base64-encoded string — decoded directly.

        Args:
            mask_raw: Raw mask value from tool args.
            org_id: Caller's org id for the artifact org check.

        Returns:
            Decoded mask bytes, or None if resolution fails.
        """
        import base64

        if _UUID_RE.match(mask_raw.strip()):
            from core.artifacts import get_artifact, read_artifact_content
            meta = await get_artifact(mask_raw.strip())
            if meta is None or str(meta.get("organization_id", "")) != str(org_id):
                return None  # cross-org or missing — silently ignore the mask
            return await read_artifact_content(mask_raw.strip())

        # Attempt base64 decode.
        try:
            return base64.b64decode(mask_raw)
        except Exception:
            return None


image_gen_connector = ImageGenConnector()
