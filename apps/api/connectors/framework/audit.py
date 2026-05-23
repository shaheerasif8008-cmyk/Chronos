from __future__ import annotations

from typing import Any


_SECRET_KEYS = {"api_key", "apikey", "token", "access_token", "refresh_token", "secret", "password", "credential"}


def redact_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in _SECRET_KEYS or any(marker in key.lower() for marker in ("secret", "token", "password", "api_key")):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_arguments(item)
        return redacted
    if isinstance(value, list):
        return [redact_arguments(item) for item in value]
    return value
