"""Canonical redaction for durable audit and compliance records.

Audit evidence is intentionally long-lived and broadly visible to tenant
administrators.  A call site forgetting to scrub one nested credential must
not turn that evidence store into a secret store, so redaction is enforced at
the append boundary and again when older rows are exported.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit


REDACTED = "[REDACTED]"

_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "access_key",
    "authorization",
    "bearer",
    "client_secret",
    "command_secret",
    "cookie",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "session_token",
    "signing_key",
    "vault_ref",
)

_TOKEN_METADATA_KEYS = {
    "max_tokens",
    "token_budget",
    "token_count",
    "token_limit",
    "token_usage",
    "tokens_used",
}

_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)^bearer\s+\S+$"),
    re.compile(r"^(?:sk|rk)_(?:live|test)_[A-Za-z0-9_-]{8,}$"),
    re.compile(r"^sk-or-v1-[A-Za-z0-9_-]{12,}$"),
    re.compile(r"^(?:ghp|github_pat)_[A-Za-z0-9_]{12,}$"),
    re.compile(r"^chr_live_[a-f0-9]{20}_[A-Za-z0-9_-]{20,}$"),
    re.compile(r"^AKIA[A-Z0-9]{16}$"),
    re.compile(r"^[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}$"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def _secret_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    if normalized in _TOKEN_METADATA_KEYS:
        return False
    return (
        normalized == "token"
        or normalized.endswith("_token")
        or any(marker in normalized for marker in _SECRET_KEY_MARKERS)
    )


def _redact_url_credentials(value: str) -> str:
    if "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.username is None and parsed.password is None:
        return value
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, f"{REDACTED}@{host}", parsed.path, parsed.query, parsed.fragment))


def redact(value: Any) -> Any:
    """Return a JSON-compatible copy with credential material removed."""

    if isinstance(value, dict):
        return {
            str(key): REDACTED if _secret_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return f"[BINARY {len(value)} bytes]"
    if isinstance(value, str):
        candidate = value.strip()
        if any(pattern.search(candidate) for pattern in _SECRET_VALUE_PATTERNS):
            return REDACTED
        return _redact_url_credentials(value)
    return value
