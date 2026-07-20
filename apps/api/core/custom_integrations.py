"""Production custom HTTP connectors and signed inbound webhook delivery.

Security invariants:

* Connector authentication and webhook bodies live only in the encrypted vault.
* Every outbound request is restricted to a configured public HTTPS origin,
  re-resolved immediately before use, never follows redirects, and has bounded
  request/response sizes.
* Inbound deliveries require a timestamped HMAC, a stable event id, Redis rate
  limiting, and database idempotency before a workflow can start.
* Durable audit records contain only metadata, hashes, and untrusted-content
  classifications; never credentials or webhook payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from connectors import vault
from connectors.framework.models import ConnectorActionDef, ConnectorDef, ConnectorResult
from connectors.framework.repository import DatabaseConnectorRepository
from core import audit
from core.audit_redaction import redact
from core.config import settings
from core.db import engine, reflect_table
from core.redis import redis_client
from core.ssrf import UnsafeURLError, assert_safe_url
from core.untrusted_content import scan_untrusted_content


MAX_HTTP_RESPONSE_BYTES = 1_048_576
MAX_HTTP_REQUEST_BYTES = 262_144
MAX_WEBHOOK_PAYLOAD_BYTES = 1_048_576
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300
_ACTION_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVENT_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_READ_METHODS = {"GET", "HEAD"}
_ALLOWED_METHODS = _READ_METHODS | {"POST", "PUT", "PATCH", "DELETE"}
_PATH_TOKEN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")


class CustomIntegrationError(ValueError):
    """Safe, user-visible configuration or delivery error."""


class AmbiguousCustomHTTPWrite(RuntimeError):
    """The request was dispatched but its mutation outcome is unknown."""


def _public_https_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() != "https":
        raise CustomIntegrationError("Base URL must use public HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise CustomIntegrationError("Base URL must contain a host and no embedded credentials")
    if parsed.query or parsed.fragment:
        raise CustomIntegrationError("Base URL cannot contain a query string or fragment")
    try:
        assert_safe_url(candidate)
    except UnsafeURLError as exc:
        raise CustomIntegrationError(f"Base URL is not a public destination: {exc}") from exc
    return candidate


def _safe_relative_path(value: str) -> str:
    path = value.strip()
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or "\\" in path
        or any(part == ".." for part in parsed.path.split("/"))
    ):
        raise CustomIntegrationError(
            "Action path must be an absolute path on the configured host without query, fragment, or traversal"
        )
    return parsed.path


def _json_object_schema(value: dict[str, Any]) -> dict[str, Any]:
    schema = dict(value or {"type": "object"})
    if schema.get("type") != "object":
        raise CustomIntegrationError("Action request schema must have type object")
    if len(json.dumps(schema, separators=(",", ":"), default=str).encode()) > 32_768:
        raise CustomIntegrationError("Action request schema is too large")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise CustomIntegrationError("Action request schema properties/required are invalid")
    if any(not isinstance(item, str) or item not in properties for item in required):
        raise CustomIntegrationError("Every required field must exist in schema properties")
    schema.setdefault("additionalProperties", False)
    return schema


def normalize_action_definition(raw: dict[str, Any]) -> dict[str, Any]:
    name = str(raw.get("name") or "").strip().lower()
    if not _ACTION_NAME.fullmatch(name):
        raise CustomIntegrationError(
            "Action names must start with a letter and contain only lowercase letters, numbers, or underscores"
        )
    method = str(raw.get("method") or "GET").strip().upper()
    if method not in _ALLOWED_METHODS:
        raise CustomIntegrationError("Action method is not supported")
    path = _safe_relative_path(str(raw.get("path") or "/"))
    schema = _json_object_schema(dict(raw.get("request_schema") or {"type": "object"}))
    risk = "read" if method in _READ_METHODS else ("destructive" if method == "DELETE" else "write")
    idempotency_header = str(raw.get("idempotency_header") or "").strip() or None
    if idempotency_header and (
        not re.fullmatch(r"[A-Za-z0-9-]{1,64}", idempotency_header)
        or idempotency_header.lower()
        in {
            "authorization",
            "cookie",
            "host",
            "content-length",
            "connection",
            "transfer-encoding",
        }
    ):
        raise CustomIntegrationError("Action idempotency header name is invalid or reserved")
    if method in _READ_METHODS:
        idempotency_header = None
    return {
        "name": name,
        "description": str(raw.get("description") or "").strip()[:500],
        "method": method,
        "path": path,
        "request_schema": schema,
        "response_schema": raw.get("response_schema") if isinstance(raw.get("response_schema"), dict) else None,
        "risk_level": risk,
        "approval_required": method not in _READ_METHODS,
        "idempotency_header": idempotency_header,
    }


def _connector_id() -> str:
    return f"custom_http_{uuid.uuid4().hex}"


def _secret_fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


def _public_webhook_url(public_id: str) -> str:
    return f"{settings.oauth_callback_base_url.rstrip('/')}/webhooks/inbound/{public_id}"


def _custom_public(row: dict[str, Any], actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "connector_id": str(row["connector_id"]),
        "name": str(row["name"]),
        "base_url": str(row["base_url"]),
        "status": str(row["status"]),
        "auth_configured": True,
        "last_health_status": row.get("last_health_status"),
        "last_health_at": row.get("last_health_at"),
        "created_at": row.get("created_at"),
        "actions": [
            {
                "name": action["action_name"],
                "description": action.get("description") or "",
                "method": action["method"],
                "path": action["path"],
                "request_schema": action.get("request_schema") or {},
                "risk_level": action["risk_level"],
                "approval_required": bool(action["approval_required"]),
                "idempotency_header": action.get("idempotency_header"),
            }
            for action in actions
        ],
    }


async def create_custom_http_connector(
    *,
    organization_id: str,
    region: str,
    member_id: str,
    name: str,
    base_url: str,
    auth_header: str,
    auth_token: str,
    actions: list[dict[str, Any]],
    workspace_id: str = "default",
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name or len(clean_name) > 120:
        raise CustomIntegrationError("Connector name is required and must be 120 characters or fewer")
    safe_base = _public_https_base_url(base_url)
    header = auth_header.strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
        raise CustomIntegrationError("Authentication header name is invalid")
    if header.lower() in {"host", "content-length", "connection", "transfer-encoding"}:
        raise CustomIntegrationError("Authentication header is reserved")
    token = auth_token.strip()
    if not token or len(token) > 8_192 or "\r" in token or "\n" in token:
        raise CustomIntegrationError("Authentication token is required and must not contain line breaks")
    normalized_actions = [normalize_action_definition(action) for action in actions]
    if not normalized_actions or len(normalized_actions) > 50:
        raise CustomIntegrationError("Define between 1 and 50 actions")
    if len({action["name"] for action in normalized_actions}) != len(normalized_actions):
        raise CustomIntegrationError("Action names must be unique")

    connector_id = _connector_id()
    connector = ConnectorDef(
        id=connector_id,
        name=clean_name,
        provider="custom_http",
        description=f"Custom HTTPS API at {urlsplit(safe_base).hostname}",
        type="api_key",
        auth_type="api_key",
        scopes=["custom_http.read", "custom_http.write"],
        actions=[
            ConnectorActionDef(
                name=action["name"],
                description=action["description"] or f"{action['method']} {action['path']}",
                parameters_schema=action["request_schema"],
                output_schema=action["response_schema"],
                required_permissions=[
                    "custom_http.read" if action["risk_level"] == "read" else "custom_http.write"
                ],
                risk_level=action["risk_level"],
                approval_required=action["approval_required"],
            )
            for action in normalized_actions
        ],
    )
    repository = DatabaseConnectorRepository()
    await repository.upsert_connector_definition(connector, tenant_id=organization_id)

    vault_ref: str | None = None
    custom_rows = await reflect_table("custom_http_connectors")
    action_rows = await reflect_table("custom_http_actions")
    credentials = await reflect_table("connector_credentials")
    connectors = await reflect_table("connectors")
    try:
        vault_ref = await vault.store(
            connector_id=connector_id,
            credentials={
                "kind": "custom_http",
                "base_url": safe_base,
                "auth_header": header,
                "auth_token": token,
            },
            org_id=organization_id,
        )
        async with engine.begin() as conn:
            custom_row = (
                await conn.execute(
                    insert(custom_rows)
                    .values(
                        organization_id=organization_id,
                        region=region,
                        connector_id=connector_id,
                        name=clean_name,
                        base_url=safe_base,
                        status="active",
                        created_by=member_id,
                    )
                    .returning(custom_rows)
                )
            ).mappings().one()
            for action in normalized_actions:
                await conn.execute(
                    insert(action_rows).values(
                        organization_id=organization_id,
                        custom_http_connector_id=custom_row["id"],
                        action_name=action["name"],
                        description=action["description"],
                        method=action["method"],
                        path=action["path"],
                        request_schema=action["request_schema"],
                        response_schema=action["response_schema"],
                        risk_level=action["risk_level"],
                        approval_required=action["approval_required"],
                        idempotency_header=action["idempotency_header"],
                    )
                )
            await conn.execute(
                insert(credentials).values(
                    organization_id=organization_id,
                    region=region,
                    workspace_id=workspace_id,
                    employee_id=member_id,
                    user_id=member_id,
                    connector_id=connector_id,
                    vault_ref=vault_ref,
                    status="active",
                )
            )
            await conn.execute(
                update(connectors)
                .where(
                    connectors.c.id == connector_id,
                    connectors.c.organization_id == organization_id,
                )
                .values(vault_ref=vault_ref)
            )

        installed = await repository.install_connector(
            connector_id,
            tenant_id=organization_id,
            workspace_id=workspace_id,
            installed_by=member_id,
        )
        for action in normalized_actions:
            await repository.grant_permission(
                tenant_id=organization_id,
                workspace_id=workspace_id,
                employee_id=member_id,
                user_id=member_id,
                connector_id=connector_id,
                action_name=action["name"],
                allowed_scopes=[
                    "custom_http.read" if action["risk_level"] == "read" else "custom_http.write"
                ],
                approval_required=action["approval_required"],
            )
        await audit.log(
            "custom_http_connector_created",
            member_id,
            "custom_http.create",
            organization_id=organization_id,
            resource_type="connector",
            resource_id=connector_id,
            payload={
                "host": urlsplit(safe_base).hostname,
                "action_names": [action["name"] for action in normalized_actions],
            },
        )
        return _custom_public(
            {**dict(installed), **dict(custom_row)},
            [{"action_name": action["name"], **action} for action in normalized_actions],
        )
    except Exception:
        if vault_ref:
            await vault.delete(vault_ref, actor_id=member_id, org_id=organization_id)
        raise


async def list_custom_http_connectors(organization_id: str) -> list[dict[str, Any]]:
    custom_rows = await reflect_table("custom_http_connectors")
    action_rows = await reflect_table("custom_http_actions")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(custom_rows)
                .where(custom_rows.c.organization_id == organization_id)
                .order_by(custom_rows.c.created_at.desc())
            )
        ).mappings().all()
        result: list[dict[str, Any]] = []
        for row in rows:
            actions = (
                await conn.execute(
                    select(action_rows)
                    .where(
                        action_rows.c.organization_id == organization_id,
                        action_rows.c.custom_http_connector_id == row["id"],
                    )
                    .order_by(action_rows.c.action_name.asc())
                )
            ).mappings().all()
            result.append(_custom_public(dict(row), [dict(action) for action in actions]))
    return result


async def disable_custom_http_connector(
    connector_id: str, *, organization_id: str, member_id: str
) -> None:
    custom_rows = await reflect_table("custom_http_connectors")
    connectors = await reflect_table("connectors")
    credentials = await reflect_table("connector_credentials")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(custom_rows).where(
                    custom_rows.c.connector_id == connector_id,
                    custom_rows.c.organization_id == organization_id,
                )
            )
        ).mappings().first()
        if not row:
            raise CustomIntegrationError("Custom HTTP connector not found")
        credential_rows = (
            await conn.execute(
                select(credentials.c.vault_ref).where(
                    credentials.c.organization_id == organization_id,
                    credentials.c.connector_id == connector_id,
                    credentials.c.status == "active",
                )
            )
        ).all()
        await conn.execute(
            update(custom_rows)
            .where(custom_rows.c.id == row["id"], custom_rows.c.organization_id == organization_id)
            .values(status="disabled", updated_at=text("NOW()"))
        )
        await conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id, connectors.c.organization_id == organization_id)
            .values(status="disabled", updated_at=text("NOW()"))
        )
        await conn.execute(
            update(credentials)
            .where(
                credentials.c.organization_id == organization_id,
                credentials.c.connector_id == connector_id,
            )
            .values(status="revoked", updated_at=text("NOW()"))
        )
    for (vault_ref,) in credential_rows:
        await vault.delete(str(vault_ref), actor_id=member_id, org_id=organization_id)
    await audit.log(
        "custom_http_connector_disabled",
        member_id,
        "custom_http.disable",
        organization_id=organization_id,
        resource_type="connector",
        resource_id=connector_id,
    )


def _format_action_path(path: str, arguments: dict[str, Any]) -> str:
    values = arguments.get("path_params") or {}
    if not isinstance(values, dict):
        raise CustomIntegrationError("path_params must be an object")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise CustomIntegrationError(f"Missing path parameter: {key}")
        return quote(str(values[key]), safe="")

    rendered = _PATH_TOKEN.sub(replace, path)
    if _PATH_TOKEN.search(rendered):
        raise CustomIntegrationError("Not all path parameters were provided")
    return _safe_relative_path(rendered)


async def _bounded_response(response: httpx.Response) -> tuple[bytes, bool]:
    length = response.headers.get("content-length")
    if length:
        try:
            if int(length) > MAX_HTTP_RESPONSE_BYTES:
                raise CustomIntegrationError("Remote response exceeded the 1 MiB limit")
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_HTTP_RESPONSE_BYTES:
            raise CustomIntegrationError("Remote response exceeded the 1 MiB limit")
        chunks.append(chunk)
    return b"".join(chunks), False


class TenantCustomHTTPAdapter:
    """Dynamic framework adapter for one tenant-created custom HTTP connector."""

    def __init__(self, connector_id: str) -> None:
        self.connector_id = connector_id

    async def validate_credentials(self, credentials: dict[str, Any]) -> bool:
        return bool(
            credentials.get("kind") == "custom_http"
            and credentials.get("base_url")
            and credentials.get("auth_header")
            and credentials.get("auth_token")
        )

    async def execute(
        self,
        action_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> ConnectorResult:
        credentials = dict(context.get("credentials") or {})
        actor = dict(context.get("actor") or {})
        organization_id = str(actor.get("org_id") or "")
        try:
            result = await execute_custom_http_action(
                connector_id=self.connector_id,
                action_name=action_name,
                arguments=arguments,
                credentials=credentials,
                organization_id=organization_id,
                provider_idempotency_key=str(
                    context.get("provider_idempotency_key") or ""
                ),
            )
        except AmbiguousCustomHTTPWrite as exc:
            return ConnectorResult(status="ambiguous", error=str(exc))
        except CustomIntegrationError as exc:
            return ConnectorResult(status="failure", error=str(exc))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return ConnectorResult(
                status="failure", error="Custom HTTP connection failed before dispatch"
            )
        except httpx.HTTPError:
            if context.get("write_operation_id"):
                return ConnectorResult(
                    status="ambiguous",
                    error="Custom HTTP transport failed after dispatch; provider outcome is unknown",
                )
            return ConnectorResult(status="failure", error="Custom HTTP transport failed")
        return ConnectorResult(status="success", output=result)


async def execute_custom_http_action(
    *,
    connector_id: str,
    action_name: str,
    arguments: dict[str, Any],
    credentials: dict[str, Any],
    organization_id: str,
    provider_idempotency_key: str = "",
) -> dict[str, Any]:
    if not organization_id:
        raise CustomIntegrationError("Tenant context is required")
    custom_rows = await reflect_table("custom_http_connectors")
    action_rows = await reflect_table("custom_http_actions")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(custom_rows, action_rows)
                .join(
                    action_rows,
                    action_rows.c.custom_http_connector_id == custom_rows.c.id,
                )
                .where(
                    custom_rows.c.organization_id == organization_id,
                    custom_rows.c.connector_id == connector_id,
                    custom_rows.c.status == "active",
                    action_rows.c.organization_id == organization_id,
                    action_rows.c.action_name == action_name,
                )
            )
        ).mappings().first()
    if not row:
        raise CustomIntegrationError("Custom HTTP action not found or disabled")

    base_url = _public_https_base_url(str(credentials.get("base_url") or ""))
    if hmac.compare_digest(base_url, str(row["base_url"])) is False:
        raise CustomIntegrationError("Connector destination no longer matches its approved configuration")
    path = _format_action_path(str(row["path"]), arguments)
    url = f"{base_url}{path}"
    # Re-resolve at execution time. Redirects are never followed, so credentials
    # cannot be forwarded to a second origin.
    try:
        assert_safe_url(url)
    except UnsafeURLError as exc:
        raise CustomIntegrationError(f"Custom HTTP request blocked: {exc}") from exc

    params = arguments.get("params")
    body = arguments.get("body")
    if params is not None and not isinstance(params, dict):
        raise CustomIntegrationError("params must be an object")
    if body is not None and not isinstance(body, (dict, list)):
        raise CustomIntegrationError("body must be a JSON object or array")
    method = str(row["method"])
    if method in _READ_METHODS and body is not None:
        raise CustomIntegrationError("Read actions cannot send a request body")
    if body is not None and len(json.dumps(body, separators=(",", ":"), default=str).encode()) > MAX_HTTP_REQUEST_BYTES:
        raise CustomIntegrationError("Request body exceeded the 256 KiB limit")

    auth_header = str(credentials.get("auth_header") or "")
    auth_token = str(credentials.get("auth_token") or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", auth_header) or not auth_token:
        raise CustomIntegrationError("Connector authentication is unavailable")
    headers = {
        auth_header: auth_token,
        "Accept": "application/json, text/plain;q=0.9",
        "User-Agent": "Chronos-Custom-Connector/1.0",
    }
    idempotency_header = str(row.get("idempotency_header") or "")
    if method not in _READ_METHODS and idempotency_header and provider_idempotency_key:
        headers[idempotency_header] = provider_idempotency_key
    timeout = httpx.Timeout(20.0, connect=8.0, read=15.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        max_redirects=0,
        trust_env=False,
    ) as client:
        async with client.stream(
            method,
            url,
            headers=headers,
            params=params,
            json=body,
        ) as response:
            if 300 <= response.status_code < 400:
                if method not in _READ_METHODS:
                    raise AmbiguousCustomHTTPWrite(
                        "Remote API redirected after dispatch; mutation outcome is unknown"
                    )
                raise CustomIntegrationError("Remote redirects are blocked for credential safety")
            if response.status_code >= 400:
                if method not in _READ_METHODS and response.status_code >= 500:
                    raise AmbiguousCustomHTTPWrite(
                        f"Remote API returned HTTP {response.status_code}; mutation outcome is unknown"
                    )
                raise CustomIntegrationError(f"Remote API returned HTTP {response.status_code}")
            try:
                raw, _ = await _bounded_response(response)
            except (CustomIntegrationError, httpx.HTTPError) as exc:
                if method not in _READ_METHODS:
                    raise AmbiguousCustomHTTPWrite(
                        "Remote response failed after dispatch; mutation outcome is unknown"
                    ) from exc
                raise
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()

    if not raw:
        data: Any = None
    elif content_type == "application/json" or content_type.endswith("+json"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            if method not in _READ_METHODS:
                raise AmbiguousCustomHTTPWrite(
                    "Remote response was invalid after dispatch; mutation outcome is unknown"
                ) from exc
            raise CustomIntegrationError("Remote API returned invalid JSON") from exc
    elif content_type.startswith("text/"):
        data = raw.decode("utf-8", errors="replace")
    else:
        data = {"binary_bytes": len(raw), "content_type": content_type or "application/octet-stream"}

    safe_data = redact(data)
    scan_input = json.dumps(safe_data, default=str, ensure_ascii=False)[:10_000]
    return {
        "status_code": response.status_code,
        "content_type": content_type,
        "data": safe_data,
        "untrusted_content": scan_untrusted_content(
            scan_input,
            source=f"custom_http:{connector_id}:{action_name}",
        ),
    }


async def healthcheck_custom_http(
    connector_id: str, *, organization_id: str
) -> dict[str, Any]:
    custom_rows = await reflect_table("custom_http_connectors")
    credentials_table = await reflect_table("connector_credentials")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(custom_rows).where(
                    custom_rows.c.organization_id == organization_id,
                    custom_rows.c.connector_id == connector_id,
                )
            )
        ).mappings().first()
        credential = (
            await conn.execute(
                select(credentials_table.c.vault_ref).where(
                    credentials_table.c.organization_id == organization_id,
                    credentials_table.c.connector_id == connector_id,
                    credentials_table.c.status == "active",
                )
            )
        ).scalar_one_or_none()
    if not row or row["status"] != "active" or not credential:
        raise CustomIntegrationError("Custom HTTP connector not found or disabled")
    creds = await vault.get(str(credential), org_id=organization_id)
    base_url = _public_https_base_url(str(creds.get("base_url") or ""))
    started = time.perf_counter()
    status = "unhealthy"
    code: int | None = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(
                "HEAD",
                base_url,
                headers={
                    str(creds["auth_header"]): str(creds["auth_token"]),
                    "User-Agent": "Chronos-Custom-Connector/1.0",
                },
            )
        code = response.status_code
        status = "healthy" if response.status_code < 500 and not (300 <= response.status_code < 400) else "unhealthy"
    except httpx.HTTPError:
        status = "unhealthy"
    checked_at = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(
            update(custom_rows)
            .where(
                custom_rows.c.organization_id == organization_id,
                custom_rows.c.connector_id == connector_id,
            )
            .values(last_health_status=status, last_health_at=checked_at, updated_at=text("NOW()"))
        )
    return {
        "status": status,
        "http_status": code,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "checked_at": checked_at,
    }


def _webhook_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "name": str(row["name"]),
        "workspace_id": str(row["workspace_id"]),
        "event_type": str(row["event_type"]),
        "trigger_source": str(row["source"]),
        "url": _public_webhook_url(str(row["public_id"])),
        "secret_fingerprint": str(row["secret_fingerprint"]),
        "status": str(row["status"]),
        "rate_limit_per_minute": int(row["rate_limit_per_minute"]),
        "last_received_at": row.get("last_received_at"),
        "last_error_code": row.get("last_error_code"),
        "rotated_at": row.get("rotated_at"),
        "created_at": row.get("created_at"),
    }


async def create_webhook_endpoint(
    *,
    organization_id: str,
    region: str,
    member_id: str,
    name: str,
    event_type: str,
    workspace_id: str = "default",
    rate_limit_per_minute: int = 60,
) -> dict[str, Any]:
    clean_name = name.strip()
    clean_event = event_type.strip()
    if not clean_name or len(clean_name) > 120:
        raise CustomIntegrationError("Webhook name is required and must be 120 characters or fewer")
    if not _EVENT_VALUE.fullmatch(clean_event):
        raise CustomIntegrationError("Event type contains unsupported characters")
    if not 1 <= rate_limit_per_minute <= 600:
        raise CustomIntegrationError("Webhook rate limit must be between 1 and 600 per minute")
    public_id = secrets.token_urlsafe(24)
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    source = f"webhook:{public_id}"
    vault_ref = await vault.store(
        connector_id=f"webhook:{public_id}",
        credentials={"kind": "webhook_signing_secret", "secret": secret},
        org_id=organization_id,
    )
    table = await reflect_table("webhook_endpoints")
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    insert(table)
                    .values(
                        organization_id=organization_id,
                        region=region,
                        public_id=public_id,
                        workspace_id=workspace_id,
                        name=clean_name,
                        source=source,
                        event_type=clean_event,
                        secret_vault_ref=vault_ref,
                        secret_fingerprint=_secret_fingerprint(secret),
                        status="active",
                        rate_limit_per_minute=rate_limit_per_minute,
                        created_by=member_id,
                    )
                    .returning(table)
                )
            ).mappings().one()
    except Exception:
        await vault.delete(vault_ref, actor_id=member_id, org_id=organization_id)
        raise
    await audit.log(
        "webhook_endpoint_created",
        member_id,
        "webhook.create",
        organization_id=organization_id,
        resource_type="webhook_endpoint",
        resource_id=str(row["id"]),
        payload={"event_type": clean_event, "workspace_id": workspace_id},
    )
    return {**_webhook_public(dict(row)), "signing_secret": secret}


async def list_webhook_endpoints(organization_id: str) -> list[dict[str, Any]]:
    table = await reflect_table("webhook_endpoints")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(table)
                .where(table.c.organization_id == organization_id)
                .order_by(table.c.created_at.desc())
            )
        ).mappings().all()
    return [_webhook_public(dict(row)) for row in rows]


async def rotate_webhook_secret(
    endpoint_id: str, *, organization_id: str, member_id: str
) -> dict[str, Any]:
    table = await reflect_table("webhook_endpoints")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(table).where(
                    table.c.id == endpoint_id,
                    table.c.organization_id == organization_id,
                )
            )
        ).mappings().first()
    if not row:
        raise CustomIntegrationError("Webhook endpoint not found")
    secret = f"whsec_{secrets.token_urlsafe(32)}"
    await vault.update(
        str(row["secret_vault_ref"]),
        {"kind": "webhook_signing_secret", "secret": secret},
        actor_id=member_id,
    )
    async with engine.begin() as conn:
        updated = (
            await conn.execute(
                update(table)
                .where(table.c.id == endpoint_id, table.c.organization_id == organization_id)
                .values(
                    secret_fingerprint=_secret_fingerprint(secret),
                    rotated_at=text("NOW()"),
                    updated_at=text("NOW()"),
                )
                .returning(table)
            )
        ).mappings().one()
    await audit.log(
        "webhook_secret_rotated",
        member_id,
        "webhook.rotate",
        organization_id=organization_id,
        resource_type="webhook_endpoint",
        resource_id=endpoint_id,
    )
    return {**_webhook_public(dict(updated)), "signing_secret": secret}


async def set_webhook_status(
    endpoint_id: str, *, organization_id: str, member_id: str, status: str
) -> dict[str, Any]:
    if status not in {"active", "disabled"}:
        raise CustomIntegrationError("Webhook status is invalid")
    table = await reflect_table("webhook_endpoints")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(table)
                .where(table.c.id == endpoint_id, table.c.organization_id == organization_id)
                .values(status=status, updated_at=text("NOW()"))
                .returning(table)
            )
        ).mappings().first()
    if not row:
        raise CustomIntegrationError("Webhook endpoint not found")
    await audit.log(
        "webhook_endpoint_status_changed",
        member_id,
        "webhook.status",
        organization_id=organization_id,
        resource_type="webhook_endpoint",
        resource_id=endpoint_id,
        payload={"status": status},
    )
    return _webhook_public(dict(row))


async def _enforce_webhook_rate_limit(row: dict[str, Any]) -> None:
    window = int(time.time() // 60)
    key = f"webhook:rate:{row['id']}:{window}"
    try:
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 120)
    except Exception as exc:
        raise CustomIntegrationError("Webhook rate limiter is unavailable") from exc
    if count > int(row["rate_limit_per_minute"]):
        raise CustomIntegrationError("Webhook rate limit exceeded")


def _verify_webhook_signature(
    *, secret: str, timestamp: str, signature: str, payload: bytes, now: float | None = None
) -> None:
    try:
        timestamp_value = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise CustomIntegrationError("Webhook timestamp is invalid") from exc
    current = int(now if now is not None else time.time())
    if abs(current - timestamp_value) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS:
        raise CustomIntegrationError("Webhook timestamp is outside the five-minute window")
    supplied = signature.removeprefix("v1=").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise CustomIntegrationError("Webhook signature is invalid")
    expected = hmac.new(
        secret.encode(),
        str(timestamp_value).encode() + b"." + payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        raise CustomIntegrationError("Webhook signature is invalid")


async def _claim_and_dispatch_webhook_event(
    *, row: dict[str, Any], event_row: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    events = await reflect_table("webhook_events")
    async with engine.begin() as conn:
        claimed = (
            await conn.execute(
                update(events)
                .where(
                    events.c.id == event_row["id"],
                    events.c.organization_id == row["organization_id"],
                    events.c.status.in_(["received", "failed"]),
                )
                .values(status="processing")
                .returning(events.c.id)
            )
        ).scalar_one_or_none()
    if claimed is None:
        return {
            "accepted": True,
            "duplicate": True,
            "event_id": str(event_row["id"]),
            "workflow_run_ids": list(event_row.get("workflow_run_ids") or []),
        }

    from connectors.framework.queue_factory import connector_execution_queue
    from connectors.framework.workflows import WorkflowRuntime

    repository = DatabaseConnectorRepository()
    runtime = WorkflowRuntime(repository, connector_execution_queue())
    persisted_payload = {
        "webhook_event_id": str(event_row["id"]),
        "payload_digest": str(event_row["payload_digest"]),
        "payload_vault_ref": str(event_row["payload_vault_ref"]),
        "untrusted_content": event_row.get("untrusted_scan") or {},
    }
    try:
        runs = await runtime.dispatch_event(
            tenant_id=str(row["organization_id"]),
            source=str(row["source"]),
            event_type=str(row["event_type"]),
            payload=payload,
            persisted_payload=persisted_payload,
            idempotency_key=str(event_row["id"]),
        )
        run_ids = [str(run["id"]) for run in runs]
        async with engine.begin() as conn:
            await conn.execute(
                update(events)
                .where(
                    events.c.id == event_row["id"],
                    events.c.organization_id == row["organization_id"],
                )
                .values(
                    status="processed",
                    workflow_run_ids=run_ids,
                    processed_at=text("NOW()"),
                )
            )
        return {
            "accepted": True,
            "duplicate": False,
            "event_id": str(event_row["id"]),
            "workflow_run_ids": run_ids,
        }
    except Exception:
        async with engine.begin() as conn:
            await conn.execute(
                update(events)
                .where(
                    events.c.id == event_row["id"],
                    events.c.organization_id == row["organization_id"],
                )
                .values(status="failed")
            )
        raise


async def receive_webhook(
    *,
    public_id: str,
    timestamp: str,
    signature: str,
    external_event_id: str,
    payload_bytes: bytes,
) -> dict[str, Any]:
    if not external_event_id or len(external_event_id) > 200 or not _EVENT_VALUE.fullmatch(external_event_id):
        raise CustomIntegrationError("X-Chronos-Event-ID is required and contains unsupported characters")
    if not payload_bytes or len(payload_bytes) > MAX_WEBHOOK_PAYLOAD_BYTES:
        raise CustomIntegrationError("Webhook payload must be between 1 byte and 1 MiB")
    endpoints = await reflect_table("webhook_endpoints")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(endpoints).where(endpoints.c.public_id == public_id)
            )
        ).mappings().first()
    if not row or row["status"] != "active":
        raise CustomIntegrationError("Webhook endpoint not found or disabled")
    await _enforce_webhook_rate_limit(dict(row))
    secret_data = await vault.get(str(row["secret_vault_ref"]), org_id=str(row["organization_id"]))
    _verify_webhook_signature(
        secret=str(secret_data.get("secret") or ""),
        timestamp=timestamp,
        signature=signature,
        payload=payload_bytes,
    )
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise CustomIntegrationError("Webhook payload must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise CustomIntegrationError("Webhook payload must be a JSON object")

    digest = hashlib.sha256(payload_bytes).hexdigest()
    scan = scan_untrusted_content(
        payload_bytes.decode("utf-8", errors="replace")[:10_000],
        source=f"webhook:{row['id']}",
    )
    payload_vault_ref = await vault.store(
        connector_id=f"webhook-event:{row['id']}",
        credentials={"kind": "webhook_payload", "payload": payload},
        org_id=str(row["organization_id"]),
    )
    events = await reflect_table("webhook_events")
    try:
        async with engine.begin() as conn:
            event_row = (
                await conn.execute(
                    insert(events)
                    .values(
                        organization_id=row["organization_id"],
                        region=row["region"],
                        endpoint_id=row["id"],
                        external_event_id=external_event_id,
                        payload_digest=digest,
                        payload_bytes=len(payload_bytes),
                        payload_vault_ref=payload_vault_ref,
                        untrusted_scan=scan,
                        status="received",
                    )
                    .returning(events)
                )
            ).mappings().one()
    except IntegrityError:
        await vault.delete(
            payload_vault_ref,
            actor_id=f"webhook:{row['id']}",
            org_id=str(row["organization_id"]),
        )
        async with engine.begin() as conn:
            event_row = (
                await conn.execute(
                    select(events).where(
                        events.c.endpoint_id == row["id"],
                        events.c.external_event_id == external_event_id,
                    )
                )
            ).mappings().one()
    async with engine.begin() as conn:
        await conn.execute(
            update(endpoints)
            .where(endpoints.c.id == row["id"])
            .values(last_received_at=text("NOW()"), last_error_code=None, updated_at=text("NOW()"))
        )
    await audit.log(
        "webhook_event_verified",
        "external_webhook",
        "webhook.receive",
        organization_id=str(row["organization_id"]),
        resource_type="webhook_event",
        resource_id=str(event_row["id"]),
        payload={
            "endpoint_id": str(row["id"]),
            "external_event_id_hash": hashlib.sha256(external_event_id.encode()).hexdigest(),
            "payload_digest": digest,
            "payload_bytes": len(payload_bytes),
            "untrusted_risk": scan.get("risk"),
        },
    )
    return await _claim_and_dispatch_webhook_event(
        row=dict(row), event_row=dict(event_row), payload=payload
    )


async def test_webhook_endpoint(
    endpoint_id: str, *, organization_id: str, member_id: str
) -> dict[str, Any]:
    endpoints = await reflect_table("webhook_endpoints")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(endpoints).where(
                    endpoints.c.id == endpoint_id,
                    endpoints.c.organization_id == organization_id,
                    endpoints.c.status == "active",
                )
            )
        ).mappings().first()
    if not row:
        raise CustomIntegrationError("Webhook endpoint not found or disabled")
    payload = {"chronos_test": True, "sent_by_member_id": member_id}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    scan = scan_untrusted_content(raw.decode(), source=f"webhook-test:{endpoint_id}")
    payload_vault_ref = await vault.store(
        connector_id=f"webhook-test:{endpoint_id}",
        credentials={"kind": "webhook_payload", "payload": payload},
        org_id=organization_id,
    )
    events = await reflect_table("webhook_events")
    async with engine.begin() as conn:
        event_row = (
            await conn.execute(
                insert(events)
                .values(
                    organization_id=organization_id,
                    region=row["region"],
                    endpoint_id=endpoint_id,
                    external_event_id=f"test_{uuid.uuid4().hex}",
                    payload_digest=digest,
                    payload_bytes=len(raw),
                    payload_vault_ref=payload_vault_ref,
                    untrusted_scan=scan,
                    status="received",
                )
                .returning(events)
            )
        ).mappings().one()
    result = await _claim_and_dispatch_webhook_event(
        row=dict(row), event_row=dict(event_row), payload=payload
    )
    await audit.log(
        "webhook_endpoint_tested",
        member_id,
        "webhook.test",
        organization_id=organization_id,
        resource_type="webhook_endpoint",
        resource_id=endpoint_id,
        payload={"workflow_run_count": len(result["workflow_run_ids"])},
    )
    return result
