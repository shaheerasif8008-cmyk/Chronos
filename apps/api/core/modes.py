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
    "default", "chat", "research", "agent", "browser", "computer",
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
        "id": "chat",
        "label": "Direct answer",
        "description": "Answers in the conversation without promoting the turn to a durable task.",
        "capabilities": ["chat", "memory", "artifacts"],
        "status": "available",
        "creates_task": False,
    },
    {
        "id": "research",
        "label": "Research",
        "description": "Plans a source-gathering task and returns grounded findings.",
        "capabilities": ["web_search", "source_review", "citations"],
        "status": "available",
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
        "description": "Runs a governed E2B cloud computer with terminal, files, screenshots, desktop input, pause, and resume.",
        "capabilities": ["terminal", "files", "desktop", "screenshots", "pause_resume", "artifacts"],
        "status": "available",
        "creates_task": True,
    },
    {
        "id": "data",
        "label": "Data",
        "description": "Analyzes uploaded CSV, JSON, and spreadsheet data in an isolated Python workspace with durable results.",
        "capabilities": ["code_python", "datasets", "charts", "artifacts"],
        "status": "available",
        "creates_task": True,
    },
    {
        "id": "image",
        "label": "Image",
        "description": "Generate images from text descriptions. Results appear as image artifacts in the chat.",
        "capabilities": ["image_generate", "artifacts"],
        "status": "available",
        "creates_task": False,
    },
    {
        "id": "voice",
        "label": "Voice",
        "description": "Transcribes recorded audio and produces governed text-to-speech output with durable attachments.",
        "capabilities": ["speech_to_text", "text_to_speech", "attachments"],
        "status": "available",
        "creates_task": False,
    },
    {
        "id": "coding",
        "label": "Coding",
        "description": "Uses a persistent tenant-isolated repo workspace for inspect, edit, test, diff, commit, and approval-bound pull requests.",
        "capabilities": ["repo_workspace", "git", "tests", "diff", "commit", "pull_request", "artifacts"],
        "status": "available",
        "creates_task": True,
    },
)


def available_modes() -> list[dict]:
    """Return product-facing composer modes.

    Chronos is chat-first: the model self-routes within a single default mode
    (running code, driving a browser, kicking off research/tasks on its own),
    so only ``default`` is advertised to the UI. The other historical modes
    remain valid inputs to :func:`normalize_mode` for backward compatibility
    with stored records, but they are no longer surfaced as a user choice.
    """
    return [dict(mode) for mode in _MODE_METADATA if mode["id"] == "default"]


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
