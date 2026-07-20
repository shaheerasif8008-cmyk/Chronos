"""OpenFGA authorization adapter.

Thin async HTTP client over the OpenFGA Check/Write API. This is the engine
behind the permission seam (``core/permissions.check``). It is intentionally
self-contained: no OpenFGA SDK dependency, just httpx against the documented
HTTP API, so the only new runtime requirement is a running OpenFGA server.

The authorization model lives here as ``AUTHORIZATION_MODEL`` (OpenFGA 1.1
type-definition JSON). Types:

    user
    organization        member, admin
    project/workspace/
    task/conversation    org, owner, editor, viewer,
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

from contextlib import asynccontextmanager
import json as jsonlib
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

from core.config import settings
from core.db import engine
from core.exceptions import ChronosError

_TIMEOUT = 5.0
_BOOTSTRAP_LOCK_NAMESPACE = 1128813135  # signed-safe int32: "CHRO"
_BOOTSTRAP_LOCK_KEY = 4605761  # signed-safe int32: "FGA"
_STORE_NAME = "chronos"

# Resolved at bootstrap (or lazily) and cached for the process lifetime.
_store_id: str | None = None
_model_id: str | None = None


class AuthzUnavailable(ChronosError):
    """Raised when the OpenFGA server cannot be reached or returns an error."""


def _scoped_resource_type(type_name: str) -> dict:
    """Type def for an org-scoped resource with owner/editor/viewer roles.

    can_view/can_edit/can_manage compose the direct roles with org-admin
    inheritance, so an organization admin can act on every resource of this type.
    Used for both ``project`` and ``workspace`` so the model is consistent.
    """
    return {
        "type": type_name,
        "relations": {
            "org": {"this": {}},
            "organization_viewer": {"this": {}},
            "owner": {"this": {}},
            "editor": {"this": {}},
            "viewer": {"this": {}},
            "can_view": {
                "union": {
                    "child": [
                        {"computedUserset": {"relation": "owner"}},
                        {"computedUserset": {"relation": "editor"}},
                        {"computedUserset": {"relation": "viewer"}},
                        {"computedUserset": {"relation": "organization_viewer"}},
                        {"tupleToUserset": {"tupleset": {"relation": "org"},
                                            "computedUserset": {"relation": "admin"}}},
                    ]
                }
            },
            "can_edit": {
                "union": {
                    "child": [
                        {"computedUserset": {"relation": "owner"}},
                        {"computedUserset": {"relation": "editor"}},
                        {"tupleToUserset": {"tupleset": {"relation": "org"},
                                            "computedUserset": {"relation": "admin"}}},
                    ]
                }
            },
            "can_manage": {
                "union": {
                    "child": [
                        {"computedUserset": {"relation": "owner"}},
                        {"tupleToUserset": {"tupleset": {"relation": "org"},
                                            "computedUserset": {"relation": "admin"}}},
                    ]
                }
            },
        },
        "metadata": {
            "relations": {
                "org": {"directly_related_user_types": [{"type": "organization"}]},
                "organization_viewer": {
                    "directly_related_user_types": [
                        {"type": "organization", "relation": "member"}
                    ]
                },
                "owner": {"directly_related_user_types": [{"type": "user"}]},
                "editor": {"directly_related_user_types": [{"type": "user"}]},
                "viewer": {"directly_related_user_types": [{"type": "user"}]},
            }
        },
    }


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
        _scoped_resource_type("project"),
        _scoped_resource_type("workspace"),
        _scoped_resource_type("task"),
        _scoped_resource_type("conversation"),
    ],
}


def is_enabled() -> bool:
    """True when authorization is configured and enforcement is switched on."""
    return bool(settings.openfga_api_url) and settings.permissions_enforce


def _base_url() -> str:
    return settings.openfga_api_url.rstrip("/")


def _request_headers() -> dict[str, str]:
    token = settings.openfga_api_token.strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _request(method: str, path: str, json: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(base_url=_base_url(), timeout=_TIMEOUT) as client:
            resp = await client.request(method, path, json=json, headers=_request_headers())
    except httpx.HTTPError as exc:  # connection refused, timeout, DNS, etc.
        raise AuthzUnavailable(f"OpenFGA request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise AuthzUnavailable(f"OpenFGA {method} {path} → {resp.status_code}: {resp.text}")
    return resp.json() if resp.content else {}


async def healthcheck() -> bool:
    """Return whether OpenFGA and its datastore report SERVING."""
    if not is_enabled():
        return False
    result = await _request("GET", "/healthz")
    return str(result.get("status") or "").upper() == "SERVING"


@asynccontextmanager
async def _bootstrap_advisory_lock():
    """Serialize store/model reconciliation across every API replica.

    The transaction-scoped PostgreSQL advisory lock is released automatically on
    commit, rollback, connection loss, or task termination. Keeping the lock in
    the application database avoids introducing a second coordination system at
    the exact point where authorization is being bootstrapped.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :lock_key)"),
            {"namespace": _BOOTSTRAP_LOCK_NAMESPACE, "lock_key": _BOOTSTRAP_LOCK_KEY},
        )
        yield


async def _matching_store_id() -> str | None:
    """Return one deterministic existing Chronos store, following pagination."""
    continuation_token = ""
    matches: list[str] = []
    while True:
        query = {"page_size": 100}
        if continuation_token:
            query["continuation_token"] = continuation_token
        result = await _request("GET", f"/stores?{urlencode(query)}")
        matches.extend(
            str(store["id"])
            for store in result.get("stores", [])
            if store.get("name") == _STORE_NAME and store.get("id")
        )
        continuation_token = str(result.get("continuation_token") or "")
        if not continuation_token:
            break
    # Duplicate stores can exist from an older racy deployment. Every replica
    # converges on the lexicographically earliest ULID instead of splitting.
    return min(matches) if matches else None


def _canonical_model(model: dict) -> str:
    """Canonicalize the semantic model payload while ignoring server-owned ids."""
    payload = {
        "schema_version": model.get("schema_version"),
        "type_definitions": model.get("type_definitions") or [],
        "conditions": model.get("conditions") or {},
    }
    return jsonlib.dumps(payload, sort_keys=True, separators=(",", ":"))


async def _matching_model_id(store_id: str) -> str | None:
    """Reuse an existing model whose canonical JSON matches the shipped model."""
    expected = _canonical_model(AUTHORIZATION_MODEL)
    continuation_token = ""
    while True:
        query = {"page_size": 100}
        if continuation_token:
            query["continuation_token"] = continuation_token
        result = await _request(
            "GET",
            f"/stores/{store_id}/authorization-models?{urlencode(query)}",
        )
        matching_ids = [
            str(model["id"])
            for model in result.get("authorization_models", [])
            if model.get("id") and _canonical_model(model) == expected
        ]
        if matching_ids:
            return max(matching_ids)
        continuation_token = str(result.get("continuation_token") or "")
        if not continuation_token:
            return None


async def ensure_store_and_model() -> tuple[str, str]:
    """Resolve or reconcile one store/model pair, then cache it per process.

    Explicit ids take precedence. Otherwise a PostgreSQL advisory lock makes the
    list/create sequence safe across replicas, and a canonical model comparison
    reuses the matching immutable model instead of writing a duplicate at every
    cold start.
    """
    global _store_id, _model_id
    if _store_id and _model_id:
        return _store_id, _model_id

    configured_store = settings.openfga_store_id.strip()
    configured_model = settings.openfga_model_id.strip()
    if configured_store and configured_model:
        _store_id, _model_id = configured_store, configured_model
        return _store_id, _model_id

    async with _bootstrap_advisory_lock():
        # A coroutine in this process may have completed while this caller was
        # waiting on the cross-process lock.
        if _store_id and _model_id:
            return _store_id, _model_id

        store_id = configured_store or await _matching_store_id()
        if not store_id:
            created = await _request("POST", "/stores", json={"name": _STORE_NAME})
            store_id = str(created["id"])

        model_id = configured_model or await _matching_model_id(store_id)
        if not model_id:
            written = await _request(
                "POST",
                f"/stores/{store_id}/authorization-models",
                json=AUTHORIZATION_MODEL,
            )
            model_id = str(written["authorization_model_id"])

        _store_id, _model_id = store_id, model_id
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
