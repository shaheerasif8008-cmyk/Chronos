"""Centralized chat-mode normalization.

All entry points that accept a user-supplied ``mode`` value should call
:func:`normalize_mode` before persisting or forwarding the value.  Keeping
the logic in one place means every code path (chat messages, task records,
…) applies identical validation rules.
"""
import logging

logger = logging.getLogger(__name__)

ALLOWED_MODES: frozenset[str] = frozenset({
    "default", "research", "agent", "browser", "computer",
    "data", "image", "voice", "coding",
})


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
