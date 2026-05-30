"""OpenFGA authorization adapter.

Thin async HTTP client over the OpenFGA Check/Write API. This is the engine
behind the permission seam (``core/permissions.check``). It is intentionally
self-contained: no OpenFGA SDK dependency, just httpx against the documented
HTTP API, so the only new runtime requirement is a running OpenFGA server.

The authorization model lives here as ``AUTHORIZATION_MODEL`` (OpenFGA 1.1
type-definition JSON). Types:

    user
    organization        member, admin
    project              org, owner, editor, viewer,
                         can_view  = owner | editor | viewer | admin-from-org
                         can_edit  = owner | editor | admin-from-org
                         can_manage= owner | admin-from-org

Enforcement is opt-in: ``permissions.check`` only calls in here when
``settings.permissions_enforce`` is true and ``settings.openfga_api_url`` is set.
When enforcement is on and the server is unreachable, checks fail CLOSED
(``AuthzUnavailable`` → the caller denies) — an operator who enabled
enforcement must keep OpenFGA running.
"""
from __future__ import annotations

import httpx

from core.config import settings
from core.exceptions import ChronosError

_TIMEOUT = 5.0

# Resolved at bootstrap (or lazily) and cached for the process lifetime.
_store_id: str | None = None
_model_id: str | None = None


class AuthzUnavailable(ChronosError):
    """Raised when the OpenFGA server cannot be reached or returns an error."""


AUTHORIZATION_MODEL: dict = {
    "schema_version": "1.1",
    "type_definitions": [
        {"type": "user"},
        {
            "type": "organization",
            "relations": {
                "member": {"this": {}},
                "admin": {"this": {}},
            },
            "metadata": {
                "relations": {
                    "member": {"directly_related_user_types": [{"type": "user"}]},
                    "admin": {"directly_related_user_types": [{"type": "user"}]},
                }
            },
        },
        {
            "type": "project",
            "relations": {
                "org": {"this": {}},
                "owner": {"this": {}},
                "editor": {"this": {}},
                "viewer": {"this": {}},
                "can_view": {
                    "union": {
                        "child": [
                            {"computedUserset": {"relation": "owner"}},
                            {"computedUserset": {"relation": "editor"}},
                            {"computedUserset": {"relation": "viewer"}},
                            {
                                "tupleToUserset": {
                                    "tupleset": {"relation": "org"},
                                    "computedUserset": {"relation": "admin"},
                                }
                            },
                        ]
                    }
                },
                "can_edit": {
                    "union": {
                        "child": [
                            {"computedUserset": {"relation": "owner"}},
                            {"computedUserset": {"relation": "editor"}},
                            {
                                "tupleToUserset": {
                                    "tupleset": {"relation": "org"},
                                    "computedUserset": {"relation": "admin"},
                                }
                            },
                        ]
                    }
                },
                "can_manage": {
                    "union": {
                        "child": [
                            {"computedUserset": {"relation": "owner"}},
                            {
                                "tupleToUserset": {
                                    "tupleset": {"relation": "org"},
                                    "computedUserset": {"relation": "admin"},
                                }
                            },
                        ]
                    }
                },
            },
            "metadata": {
                "relations": {
                    "org": {"directly_related_user_types": [{"type": "organization"}]},
                    "owner": {"directly_related_user_types": [{"type": "user"}]},
                    "editor": {"directly_related_user_types": [{"type": "user"}]},
                    "viewer": {"directly_related_user_types": [{"type": "user"}]},
                }
            },
        },
    ],
}


def is_enabled() -> bool:
    """True when authorization is configured and enforcement is switched on."""
    return bool(settings.openfga_api_url) and settings.permissions_enforce


def _base_url() -> str:
    return settings.openfga_api_url.rstrip("/")


async def _request(method: str, path: str, json: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=_TIMEOUT) as client:
            resp = await client.request(method, path, json=json)
    except httpx.HTTPError as exc:  # connection refused, timeout, DNS, etc.
        raise AuthzUnavailable(f"OpenFGA request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AuthzUnavailable(f"OpenFGA {method} {path} → {resp.status_code}: {resp.text}")
    return resp.json() if resp.content else {}


async def ensure_store_and_model() -> tuple[str, str]:
    """Resolve (or create) the store and authorization model, caching their ids.

    Precedence: explicit settings ids → existing store named 'chronos' → create.
    The model is written if no model id is configured. Idempotent enough to run
    at every startup; cheap once cached.
    """
    global _store_id, _model_id
    if _store_id and _model_id:
        return _store_id, _model_id

    store_id = settings.openfga_store_id or None
    if not store_id:
        listing = await _request("GET", "/stores")
        for store in listing.get("stores", []):
            if store.get("name") == "chronos":
                store_id = store["id"]
                break
    if not store_id:
        created = await _request("POST", "/stores", json={"name": "chronos"})
        store_id = created["id"]
    _store_id = store_id

    model_id = settings.openfga_model_id or None
    if not model_id:
        written = await _request(
            "POST", f"/stores/{store_id}/authorization-models", json=AUTHORIZATION_MODEL
        )
        model_id = written["authorization_model_id"]
    _model_id = model_id
    return _store_id, _model_id


async def check(user: str, relation: str, obj: str) -> bool:
    """Return whether ``user`` has ``relation`` on ``obj`` per the live model."""
    store_id, model_id = await ensure_store_and_model()
    result = await _request(
        "POST",
        f"/stores/{store_id}/check",
        json={
            "authorization_model_id": model_id,
            "tuple_key": {"user": user, "relation": relation, "object": obj},
        },
    )
    return bool(result.get("allowed"))


async def write_tuples(tuples: list[tuple[str, str, str]]) -> None:
    """Write relationship tuples: list of (user, relation, object)."""
    if not tuples:
        return
    store_id, model_id = await ensure_store_and_model()
    await _request(
        "POST",
        f"/stores/{store_id}/write",
        json={
            "authorization_model_id": model_id,
            "writes": {
                "tuple_keys": [
                    {"user": u, "relation": r, "object": o} for u, r, o in tuples
                ]
            },
        },
    )


async def delete_tuples(tuples: list[tuple[str, str, str]]) -> None:
    """Delete relationship tuples: list of (user, relation, object)."""
    if not tuples:
        return
    store_id, _ = await ensure_store_and_model()
    await _request(
        "POST",
        f"/stores/{store_id}/write",
        json={
            "deletes": {
                "tuple_keys": [
                    {"user": u, "relation": r, "object": o} for u, r, o in tuples
                ]
            }
        },
    )


def reset_cache() -> None:
    """Clear cached store/model ids — used by tests."""
    global _store_id, _model_id
    _store_id = None
    _model_id = None
