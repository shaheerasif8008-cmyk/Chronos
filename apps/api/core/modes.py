"""Centralized chat-mode normalization.

All entry points that accept a user-supplied ``mode`` value should call
:func:`normalize_mode` before persisting or forwarding the value.  Keeping
the logic in one place means every code path (chat messages, task records,
…) applies identical validation rules.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

ALLOWED_MODES: frozenset[str] = frozenset({
    "default", "research", "agent", "browser", "computer",
    "data", "image", "voice", "coding",
})

_MODE_METADATA: tuple[dict, ...] = (
    {
        "id": "default",
        "label": "Default",
        "description": "General assistant chat with automatic task routing when needed.",
        "capabilities": ["chat", "memory", "artifacts"],
        "status": "available",
        "creates_task": False,
    },
    {
        "id": "research",
        "label": "Research",
        "description": "Plans a source-gathering task and returns grounded findings.",
        "capabilities": ["web_search", "source_review", "citations"],
        "status": "foundation",
        "creates_task": True,
    },
    {
        "id": "agent",
        "label": "Agent",
        "description": "Runs the governed native tool loop for multi-step work.",
        "capabilities": ["tool_use", "approvals", "activity_trace"],
        "status": "available",
        "creates_task": True,
    },
    {
        "id": "browser",
        "label": "Browser",
        "description": "Runs persistent governed browser sessions with navigation, form actions, screenshots, takeover, and revocation.",
        "capabilities": ["web_search", "fetch", "navigate", "click", "type", "screenshots", "takeover"],
        "status": "available",
        "creates_task": True,
    },
    {
        "id": "computer",
        "label": "Computer",
        "description": "Reserved for sandboxed computer sessions when that runtime ships.",
        "capabilities": [],
        "status": "unavailable",
        "creates_task": True,
    },
    {
        "id": "data",
        "label": "Data",
        "description": "Uses code tools for data questions; a full data workspace is still pending.",
        "capabilities": ["code_python", "artifacts"],
        "status": "foundation",
        "creates_task": True,
    },
    {
        "id": "image",
        "label": "Image",
        "description": "Generate images from text descriptions. Results appear as image artifacts in the chat.",
        "capabilities": ["image_generate", "artifacts"],
        "status": "foundation",
        "creates_task": False,
    },
    {
        "id": "voice",
        "label": "Voice",
        "description": "Reserved for speech-to-text and text-to-speech workflows.",
        "capabilities": [],
        "status": "unavailable",
        "creates_task": False,
    },
    {
        "id": "coding",
        "label": "Coding",
        "description": "Uses code-oriented context and tools; repo workspaces are still pending.",
        "capabilities": ["code_python", "artifacts"],
        "status": "foundation",
        "creates_task": True,
    },
)


def available_modes() -> list[dict]:
    """Return product-facing metadata for all composer modes."""
    return [dict(mode) for mode in _MODE_METADATA]


def normalize_mode(value: str | None) -> str:
    """Coerce a raw mode value to a known mode string.

    Rules:
    - ``None`` → ``"default"`` (silent)
    - empty / whitespace-only after strip → ``"default"`` (silent)
    - non-empty but unrecognized → ``"default"`` with a ``WARNING`` log
    - valid value (after strip + lowercase) → that value

    Args:
        value: Raw mode string supplied by the caller, or ``None``.

    Returns:
        A member of :data:`ALLOWED_MODES`, guaranteed to be ``"default"``
        when the input is absent or unrecognized.
    """
    if value is None:
        return "default"
    stripped = value.strip().lower()
    if not stripped:
        return "default"
    if stripped in ALLOWED_MODES:
        return stripped
    logger.warning("Unknown chat mode %r; coercing to 'default'", value)
    return "default"
