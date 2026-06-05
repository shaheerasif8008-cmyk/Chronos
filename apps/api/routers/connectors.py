from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from connectors.framework.adapters import adapter_registry
from connectors.framework.approvals import ApprovalService
from connectors.framework.mcp import MCPDiscoveryService
from connectors.framework.planner import ToolOrchestrationPlanner
from connectors.framework.queue_factory import connector_execution_queue
from connectors.framework.queued_runtime import QueuedConnectorExecutionService
from connectors.framework.repository import DatabaseConnectorRepository
from connectors.framework.runtime import ConnectorExecutionService
from connectors.framework.seed import seed_builtin_connectors
from connectors.framework.tool_calling import execute_tool_call, get_available_tools_for_employee
from core import audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.exceptions import VaultError
from core.models import AgentContext, Member

router = APIRouter(prefix="/connectors", tags=["connectors"])


def _connectors_redirect(**params: str) -> RedirectResponse:
    url = f"{settings.frontend_base_url.rstrip('/')}/connectors"
    clean = {key: value for key, value in params.items() if value}
    if clean:
        url = f"{url}?{urlencode(clean)}"
    return RedirectResponse(url=url, status_code=302)


def _gmail_module():
    import importlib

    return importlib.import_module("connectors.gmail")


class InstallConnectorRequest(BaseModel):
    workspace_id: str = "default"


class PermissionRequest(BaseModel):
    workspace_id: str = "default"
    employee_id: str
    user_id: str | None = None
    action_name: str
    allowed_scopes: list[str] = Field(default_factory=list)
    approval_required: bool = False


class ExecuteConnectorRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str = "default"
    employee_id: str | None = None


class ToolCallRequest(BaseModel):
    tool_call: dict[str, Any]
    workspace_id: str = "default"
    employee_id: str | None = None


class ConnectorProofRequest(BaseModel):
    message: str = "Chronos connector proof"


class ResolveApprovalRequest(BaseModel):
    approved: bool
    note: str | None = None


class RegisterMCPServerRequest(BaseModel):
    name: str
    transport: str = Field(pattern="^(local|remote)$")
    command: str | None = None
    server_url: str | None = None


class PlanRequest(BaseModel):
    goal: str
    workspace_id: str = "default"
    employee_id: str | None = None


class ExecutePlanRequest(BaseModel):
    plan: dict[str, Any]
    workspace_id: str = "default"
    employee_id: str | None = None


class PolicyRequest(BaseModel):
    workspace_id: str | None = None
    employee_id: str | None = None
    role: str | None = None
    connector_id: str | None = None
    action_name: str | None = None
    risk_level: str | None = None
    decision: str = Field(pattern="^(allow|deny|require_approval)$")
    approval_mode: str = Field(default="single", pattern="^(single|admin|multi)$")
    conditions: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    enabled: bool = True


def repo() -> DatabaseConnectorRepository:
    return DatabaseConnectorRepository()


async def ensure_registry() -> DatabaseConnectorRepository:
    repository = repo()
    await seed_builtin_connectors(repository, tenant_id=settings.org_id)
    return repository


def clean_connector(row: dict[str, Any]) -> dict[str, Any]:
    category = "Internal" if row.get("provider") == "internal" else "Productivity"
    return {
        "id": row["id"],
        "name": {"internal_echo": "Runtime Diagnostics", "internal_time": "System Clock"}.get(row["id"], row.get("name") or row["id"]),
        "provider": row.get("provider"),
        "description": row.get("description") or "",
        "type": row.get("type") or "native",
        "category": category,
        "status": row.get("status") or "available",
        "auth_type": row.get("auth_type") or "none",
        "scopes": row.get("scopes") or [],
        "actions": row.get("actions") or [],
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
        "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
    }


def clean_action(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "description": row["description"],
        "parameters_schema": row["parameters_schema"],
        "output_schema": row.get("output_schema"),
        "required_permissions": row.get("required_permissions") or [],
        "risk_level": row["risk_level"],
        "approval_required": bool(row.get("approval_required")),
    }


@router.get("/agent-tools")
async def list_agent_tool_health(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    """Return live health/tier for the agent-loop connectors (gmail, browser, fs, code, mcp).

    This is separate from the framework registry health — it reports on the connectors
    that the ToolBroker's _route() function dispatches to directly during task execution.
    """
    await permissions.check(member, "list_connector_health", member.organization_id)
    from core.connector_health import check_connectors
    return await check_connectors()


@router.get("/catalog")
async def list_catalog(member: Member = Depends(get_current_member)) -> list[dict]:
    """Return the full app catalog with per-app configured + connected status."""
    from connectors.oauth_apps import available_apps
    from core.db import engine, reflect_table
    from sqlalchemy import select

    apps = available_apps()

    # Enrich with connection, health, and last-used state from the connector tables.
    connectors_table = await reflect_table("connectors")
    health_table = await reflect_table("connector_health")
    logs_table = await reflect_table("connector_execution_logs")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    connectors_table.c.provider,
                    connectors_table.c.account_handle,
                    connectors_table.c.status,
                ).where(
                    (connectors_table.c.organization_id == str(member.organization_id))
                    & (connectors_table.c.status == "active")
                )
            )
        ).mappings().all()
        health_rows = (
            await conn.execute(
                select(
                    health_table.c.connector_id,
                    health_table.c.status,
                    health_table.c.updated_at,
                ).where(health_table.c.organization_id == str(member.organization_id))
            )
        ).mappings().all()
        log_rows = (
            await conn.execute(
                select(
                    logs_table.c.connector_id,
                    logs_table.c.created_at,
                )
                .where(logs_table.c.organization_id == str(member.organization_id))
                .order_by(logs_table.c.created_at.desc())
            )
        ).mappings().all()

    connected: dict[str, str] = {row["provider"]: row["account_handle"] or "" for row in rows}
    health: dict[str, dict[str, Any]] = {row["connector_id"]: dict(row) for row in health_rows}
    last_used: dict[str, str] = {}
    for row in log_rows:
        cid = str(row["connector_id"])
        if cid not in last_used:
            last_used[cid] = str(row["created_at"]) if row.get("created_at") else ""
    for app in apps:
        app["connected"] = app["id"] in connected
        app["account_handle"] = connected.get(app["id"], "")
        app_health = health.get(app["id"]) or {}
        app["health_status"] = app_health.get("status") or ("connected" if app["connected"] else "not_connected")
        app["health_updated_at"] = str(app_health.get("updated_at")) if app_health.get("updated_at") else None
        app["last_used_at"] = last_used.get(app["id"], "")
    return apps


@router.post("/gmail/oauth-start")
async def gmail_oauth_start(member: Member = Depends(get_current_member)) -> dict[str, str]:
    """Initiate the Google OAuth2 Gmail flow for the current member.

    Returns a URL the frontend should redirect the user's browser to.  The state
    parameter is HMAC-signed so the callback can verify it without a DB lookup.
    """
    await permissions.check(member, "connect_gmail", member.organization_id)
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(
            status_code=503,
            detail="GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not configured",
        )
    try:
        url = await _gmail_module().oauth_start_url(
            member_id=str(member.id),
            org_id=str(member.organization_id),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await audit.log(
        "connector_oauth_start", str(member.id), "gmail.oauth_start",
        organization_id=member.organization_id,
        resource_type="connector", resource_id="gmail",
    )
    return {"url": url}


@router.get("/gmail/oauth-callback")
async def gmail_oauth_callback(
    code: str | None = Query(None),
    state: str = Query(...),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
) -> RedirectResponse:
    """Google OAuth2 callback — exchanges the code for tokens and stores them.

    The *state* parameter is an HMAC-signed token that encodes member_id + org_id.
    We verify the signature here to prevent CSRF and to avoid a DB lookup just to
    reconstruct the member context.
    """
    import uuid
    from connectors.vault import store as vault_store
    from core.db import engine, reflect_table
    from sqlalchemy import insert, select, update

    if error:
        return _connectors_redirect(
            connector_error=error_description or error,
            connector_provider="gmail",
        )
    if not code:
        return _connectors_redirect(
            connector_error="Google did not return an authorization code.",
            connector_provider="gmail",
        )

    # oauth_finish verifies the HMAC state internally and returns (member_id, org_id)
    # as part of the credential dict.
    try:
        credential_data = await _gmail_module().oauth_finish(code=code, state=state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    member_id: str = credential_data["member_id"]
    org_id: str = credential_data["org_id"]

    try:
        connector_id = f"gmail:{org_id}:{member_id}"
        vault_ref = await vault_store(
            connector_id=connector_id,
            credentials=credential_data,
            org_id=org_id,
        )

        # Upsert the connectors table so the framework registry knows this user's Gmail is live.
        connectors_table = await reflect_table("connectors")
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(connectors_table).where(
                        (connectors_table.c.organization_id == org_id)
                        & (connectors_table.c.provider == "gmail")
                    )
                )
            ).mappings().first()

            if existing:
                await conn.execute(
                    update(connectors_table)
                    .where(connectors_table.c.id == existing["id"])
                    .values(
                        vault_ref=vault_ref,
                        status="active",
                        account_handle=credential_data.get("email", ""),
                    )
                )
            else:
                await conn.execute(
                    insert(connectors_table).values(
                        id=str(uuid.uuid4()),
                        organization_id=org_id,
                        provider="gmail",
                        account_handle=credential_data.get("email", ""),
                        vault_ref=vault_ref,
                        status="active",
                        scopes=["gmail.read_inbox", "gmail.draft", "gmail.search"],
                        region=settings.region,
                    )
                )
    except VaultError as exc:
        return _connectors_redirect(
            connector_error=str(exc),
            connector_provider="gmail",
        )

    await audit.log(
        "connector_oauth_complete", member_id, "gmail.oauth_callback",
        organization_id=org_id,
        resource_type="connector", resource_id="gmail",
    )
    return _connectors_redirect(connector_success="gmail")


# ---------------------------------------------------------------------------
# Generic OAuth2 routes — work for any app in the catalog
# ---------------------------------------------------------------------------

@router.post("/{provider}/oauth-start")
async def generic_oauth_start(
    provider: str,
    member: Member = Depends(get_current_member),
) -> dict[str, str]:
    """Build a Google/Notion/Slack/… consent URL and return it.

    The frontend redirects the user's browser to the returned URL.
    """
    from connectors.oauth_apps import get_app, get_client_credentials
    from urllib.parse import urlencode

    # Gmail has its own route above — don't double-handle it here
    if provider == "gmail":
        raise HTTPException(status_code=400, detail="Use /connectors/gmail/oauth-start for Gmail")

    app = get_app(provider)
    if not app:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    if app.auth_type != "oauth2":
        raise HTTPException(status_code=501, detail=f"{app.name} uses {app.auth_type} setup, not OAuth2")

    await permissions.check(member, f"connect_{provider}", str(member.organization_id))

    client_id, client_secret = get_client_credentials(app)
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=503,
            detail=f"{app.client_id_env} / {app.client_secret_env} are not configured",
        )

    state = _gmail_module()._build_state(str(member.id), str(member.organization_id))
    redirect_uri = f"{settings.composio_callback_base_url}/connectors/{provider}/oauth-callback"

    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        **app.extra_auth_params,
    }
    if app.scopes:
        params["scope"] = " ".join(app.scopes)

    # Notion uses Basic auth for token exchange — its authorize URL is slightly different
    if provider == "notion":
        params.pop("response_type", None)
        params["response_type"] = "code"

    url = f"{app.auth_url}?{urlencode(params)}"
    await audit.log(
        "connector_oauth_start", str(member.id), f"{provider}.oauth_start",
        organization_id=member.organization_id,
        resource_type="connector", resource_id=provider,
    )
    return {"url": url}


@router.get("/{provider}/oauth-callback")
async def generic_oauth_callback(
    provider: str,
    code: str = Query(...),
    state: str = Query(...),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Exchange the OAuth2 code for tokens and store them in the vault."""
    import uuid
    import time
    from connectors.oauth_apps import get_app, get_client_credentials
    from connectors.vault import store as vault_store
    from core.db import engine, reflect_table
    from sqlalchemy import insert, select, update

    if provider == "gmail":
        raise HTTPException(status_code=400, detail="Use /connectors/gmail/oauth-callback for Gmail")

    if error:
        return _connectors_redirect(connector_error=error, connector_provider=provider)

    app = get_app(provider)
    if not app:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    if app.auth_type != "oauth2":
        raise HTTPException(status_code=501, detail=f"{app.name} uses {app.auth_type} setup, not OAuth2")

    try:
        member_id, org_id = _gmail_module()._verify_state(state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    client_id, client_secret = get_client_credentials(app)
    redirect_uri = f"{settings.composio_callback_base_url}/connectors/{provider}/oauth-callback"

    # Exchange code for tokens
    import httpx as _httpx
    token_data: dict[str, Any] = {}
    try:
        if provider == "notion":
            # Notion requires HTTP Basic auth
            import base64 as _b64
            basic = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            async with _httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    app.token_url,
                    headers={"Authorization": f"Basic {basic}", "Content-Type": "application/json"},
                    json={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri},
                )
        elif provider == "github":
            async with _httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    app.token_url,
                    headers={"Accept": "application/json"},
                    data={"client_id": client_id, "client_secret": client_secret,
                          "code": code, "redirect_uri": redirect_uri},
                )
        else:
            async with _httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    app.token_url,
                    data={"grant_type": "authorization_code", "code": code,
                          "client_id": client_id, "client_secret": client_secret,
                          "redirect_uri": redirect_uri},
                )

        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Token exchange failed: {resp.text[:300]}")
        token_data = resp.json()
    except _httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Token exchange request failed: {exc}") from exc

    access_token = token_data.get("access_token", "")
    if not access_token:
        raise HTTPException(status_code=502, detail="No access_token in provider response")

    # Build credentials dict for vault storage
    credentials: dict[str, Any] = {
        "provider": provider,
        "access_token": access_token,
        "expires_at": str(time.time() + token_data.get("expires_in", 0) - 300)
        if token_data.get("expires_in")
        else "0",
        "member_id": member_id,
        "org_id": org_id,
    }
    if token_data.get("refresh_token"):
        credentials["refresh_token"] = token_data["refresh_token"]
    if token_data.get("team", {}).get("name"):  # Slack workspace name
        credentials["account_handle"] = token_data["team"]["name"]
    if token_data.get("authed_user", {}).get("access_token"):
        # Slack v2 — use user token for DMs etc.
        credentials["user_access_token"] = token_data["authed_user"]["access_token"]

    # Fetch account handle (display email/username) from the provider
    account_handle = credentials.get("account_handle", "")
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            if provider in {"google_calendar", "google_drive"}:
                r = await client.get(
                    "https://www.googleapis.com/oauth2/v3/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if r.status_code == 200:
                    account_handle = r.json().get("email", "")
            elif provider == "github":
                r = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"},
                )
                if r.status_code == 200:
                    account_handle = r.json().get("login", "")
            elif provider == "notion":
                account_handle = token_data.get("owner", {}).get("user", {}).get("name", "")
            elif provider == "linear":
                r = await client.post(
                    "https://api.linear.app/graphql",
                    headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                    json={"query": "{ viewer { email } }"},
                )
                if r.status_code == 200:
                    account_handle = r.json().get("data", {}).get("viewer", {}).get("email", "")
    except Exception:
        pass  # non-fatal

    credentials["account_handle"] = account_handle
    connector_id = f"{provider}:{org_id}:{member_id}"
    try:
        vault_ref = await vault_store(connector_id=connector_id, credentials=credentials, org_id=org_id)

        # Upsert connectors table
        connectors_table = await reflect_table("connectors")
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    select(connectors_table).where(
                        (connectors_table.c.organization_id == org_id)
                        & (connectors_table.c.provider == provider)
                    )
                )
            ).mappings().first()

            if existing:
                await conn.execute(
                    update(connectors_table)
                    .where(connectors_table.c.id == existing["id"])
                    .values(vault_ref=vault_ref, status="active", account_handle=account_handle)
                )
            else:
                await conn.execute(
                    insert(connectors_table).values(
                        id=str(uuid.uuid4()),
                        organization_id=org_id,
                        provider=provider,
                        account_handle=account_handle,
                        vault_ref=vault_ref,
                        status="active",
                        scopes=app.scopes,
                        region=settings.region,
                    )
                )
    except VaultError as exc:
        return _connectors_redirect(
            connector_error=str(exc),
            connector_provider=provider,
        )

    await audit.log(
        "connector_oauth_complete", member_id, f"{provider}.oauth_callback",
        organization_id=org_id,
        resource_type="connector", resource_id=provider,
    )
    return _connectors_redirect(connector_success=provider)


@router.delete("/{provider}/disconnect")
async def disconnect_connector(
    provider: str,
    member: Member = Depends(get_current_member),
) -> dict[str, str]:
    """Revoke and remove a connected app."""
    from core.db import engine, reflect_table
    from sqlalchemy import select, update

    await permissions.check(member, f"disconnect_{provider}", str(member.organization_id))

    connectors_table = await reflect_table("connectors")
    async with engine.begin() as conn:
        await conn.execute(
            update(connectors_table)
            .where(
                (connectors_table.c.organization_id == str(member.organization_id))
                & (connectors_table.c.provider == provider)
            )
            .values(status="disconnected")
        )

    await audit.log(
        "connector_disconnected", str(member.id), f"{provider}.disconnect",
        organization_id=member.organization_id,
        resource_type="connector", resource_id=provider,
    )
    return {"status": "disconnected"}


@router.get("/")
async def list_connectors(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connectors", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_connectors(tenant_id=member.organization_id)
    return [clean_connector(row) for row in rows if row.get("actions")]


@router.get("/execution-logs")
async def list_connector_execution_logs(
    connector_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_execution_logs", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_execution_logs(tenant_id=member.organization_id, connector_id=connector_id, limit=limit)
    return [
        {
            **dict(row),
            "created_at": str(row["created_at"]) if row.get("created_at") else None,
        }
        for row in rows
    ]


@router.get("/approvals")
async def list_connector_approvals(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_approvals", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_approval_requests(tenant_id=member.organization_id, status=status, limit=limit)
    return [{**dict(row), "created_at": str(row.get("created_at")) if row.get("created_at") else None, "resolved_at": str(row.get("resolved_at")) if row.get("resolved_at") else None} for row in rows]


@router.post("/approvals/{approval_id}/resolve")
async def resolve_connector_approval(approval_id: str, req: ResolveApprovalRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "resolve_connector_approval", approval_id)
    repository = await ensure_registry()
    try:
        if req.approved:
            return await ApprovalService(repository).approve_and_enqueue(
                approval_id,
                tenant_id=member.organization_id,
                actor_id=member.id,
                queue=connector_execution_queue(),
                note=req.note,
            )
        row = await ApprovalService(repository).resolve(
            approval_id,
            tenant_id=member.organization_id,
            actor_id=member.id,
            approved=False,
            note=req.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return dict(row)


@router.get("/health")
async def list_connector_health(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_health", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_connector_health(tenant_id=member.organization_id)
    return [{**dict(row), "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None} for row in rows]


@router.get("/execution-traces")
async def list_connector_execution_traces(
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_execution_traces", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_execution_traces(tenant_id=member.organization_id, limit=limit)
    return [{**dict(row), "started_at": str(row.get("started_at")) if row.get("started_at") else None, "completed_at": str(row.get("completed_at")) if row.get("completed_at") else None} for row in rows]


@router.get("/execution-jobs")
async def list_connector_execution_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_execution_jobs", member.organization_id)
    repository = await ensure_registry()
    rows = await repository.list_execution_jobs(tenant_id=member.organization_id, status=status, limit=limit)
    return [{**dict(row), "created_at": str(row.get("created_at")) if row.get("created_at") else None, "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None} for row in rows]


@router.post("/execution-jobs/{job_id}/cancel")
async def cancel_connector_execution_job(job_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "cancel_connector_execution_job", job_id)
    repository = await ensure_registry()
    try:
        return await repository.cancel_execution_job(job_id, tenant_id=member.organization_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/execution-traces/{trace_id}")
async def get_connector_execution_trace(trace_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "get_connector_execution_trace", trace_id)
    repository = await ensure_registry()
    traces = await repository.list_execution_traces(tenant_id=member.organization_id, limit=100)
    trace = next((row for row in traces if row["id"] == trace_id), None)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"trace": dict(trace), "steps": await repository.list_trace_steps(trace_id)}


@router.post("/plans")
async def create_connector_plan(req: PlanRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_connector_plan", req.workspace_id)
    repository = await ensure_registry()
    plan = await ToolOrchestrationPlanner(repository).create_plan(
        req.goal,
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id or member.id,
    )
    return {"id": plan.id, "goal": plan.goal, "steps": [step.__dict__ for step in plan.steps]}


@router.post("/plans/execute")
async def execute_connector_plan(req: ExecutePlanRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    from connectors.framework.planner import ToolExecutionPlan, ToolExecutionStep

    await permissions.check(member, "execute_connector_plan", req.workspace_id)
    repository = await ensure_registry()
    raw_steps = req.plan.get("steps") or []
    plan = ToolExecutionPlan(
        id=req.plan.get("id") or "ad_hoc_plan",
        goal=req.plan.get("goal") or "",
        steps=[
            ToolExecutionStep(
                id=step.get("id") or f"step-{index + 1}",
                tool_name=step["tool_name"],
                arguments=step.get("arguments") or {},
                dependencies=step.get("dependencies") or [],
                approval_required=bool(step.get("approval_required")),
                parallel_safe=bool(step.get("parallel_safe")),
            )
            for index, step in enumerate(raw_steps)
        ],
    )
    return await ToolOrchestrationPlanner(repository).execute_plan(
        plan,
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id or member.id,
        user_id=member.id,
        queue=connector_execution_queue(),
    )


@router.get("/mcp")
async def list_mcp_servers(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "list_mcp_servers", member.organization_id)
    repository = await ensure_registry()
    return {
        "servers": await repository.list_mcp_servers(tenant_id=member.organization_id),
        "discovery_logs": await repository.list_mcp_discovery_logs(tenant_id=member.organization_id),
    }


@router.post("/mcp/register")
async def register_mcp_server(req: RegisterMCPServerRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "register_mcp_server", member.organization_id)
    if req.transport == "local" and not req.command:
        raise HTTPException(status_code=400, detail="Local MCP servers require a command")
    if req.transport == "remote" and not req.server_url:
        raise HTTPException(status_code=400, detail="Remote MCP servers require a server_url")
    repository = await ensure_registry()
    return await repository.register_mcp_server(
        tenant_id=member.organization_id,
        name=req.name,
        transport=req.transport,
        command=req.command,
        server_url=req.server_url,
    )


@router.post("/mcp/{server_id}/discover")
async def discover_mcp_server(server_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "discover_mcp_server", server_id)
    repository = await ensure_registry()
    return await MCPDiscoveryService(repository).discover(server_id, tenant_id=member.organization_id)


@router.get("/policies")
async def list_connector_policies(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_policies", member.organization_id)
    repository = await ensure_registry()
    return await repository.list_policies(tenant_id=member.organization_id)


@router.post("/policies")
async def create_connector_policy(req: PolicyRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_connector_policy", member.organization_id)
    repository = await ensure_registry()
    return await repository.create_policy(
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id,
        role=req.role,
        connector_id=req.connector_id,
        action_name=req.action_name,
        risk_level=req.risk_level,
        decision=req.decision,
        approval_mode=req.approval_mode,
        conditions=req.conditions,
        priority=req.priority,
        enabled=req.enabled,
    )


@router.delete("/policies/{policy_id}")
async def delete_connector_policy(policy_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "delete_connector_policy", policy_id)
    repository = await ensure_registry()
    await repository.delete_policy(policy_id, tenant_id=member.organization_id)
    return {"id": policy_id, "deleted": True}


@router.get("/tools")
async def list_available_tools(
    employee_id: str,
    workspace_id: str = Query(default="default"),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_tools", workspace_id)
    repository = await ensure_registry()
    return await get_available_tools_for_employee(
        repository,
        employee_id=employee_id,
        workspace_id=workspace_id,
        tenant_id=member.organization_id,
    )


@router.post("/tool-call")
async def execute_connector_tool_call(req: ToolCallRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "execute_connector_tool_call", req.workspace_id)
    repository = await ensure_registry()
    return await execute_tool_call(
        repository,
        req.tool_call,
        AgentContext(
            id=req.employee_id or member.id,
            org_id=member.organization_id,
            member_id=member.id,
            workspace_id=req.workspace_id,
        ),
    )


@router.post("/{connector_id}/install")
async def install_connector(connector_id: str, req: InstallConnectorRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "install_connector", connector_id)
    repository = await ensure_registry()
    connector = await repository.get_connector(connector_id, tenant_id=member.organization_id)
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    if connector.get("type") == "mcp":
        raise HTTPException(status_code=501, detail="MCP transport is not implemented for production execution")

    installed = await repository.install_connector(
        connector_id,
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        installed_by=member.id,
    )
    for action in await repository.list_actions(connector_id):
        await repository.grant_permission(
            tenant_id=member.organization_id,
            workspace_id=req.workspace_id,
            employee_id=member.id,
            user_id=member.id,
            connector_id=connector_id,
            action_name=action["name"],
            allowed_scopes=action.get("required_permissions") or [],
            approval_required=bool(action.get("approval_required")),
        )
    return clean_connector(installed)


@router.post("/{connector_id}/disable")
async def disable_connector(connector_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "disable_connector", connector_id)
    repository = await ensure_registry()
    if not await repository.get_connector(connector_id, tenant_id=member.organization_id):
        raise HTTPException(status_code=404, detail="Connector not found")
    await repository.disable_connector(connector_id, tenant_id=member.organization_id)
    return {"id": connector_id, "status": "disabled"}


@router.get("/{connector_id}/actions")
async def list_connector_actions(connector_id: str, member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_connector_actions", connector_id)
    repository = await ensure_registry()
    if not await repository.get_connector(connector_id, tenant_id=member.organization_id):
        raise HTTPException(status_code=404, detail="Connector not found")
    return [clean_action(row) for row in await repository.list_actions(connector_id)]


@router.post("/{connector_id}/permissions")
async def grant_connector_permission(connector_id: str, req: PermissionRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "grant_connector_permission", connector_id)
    repository = await ensure_registry()
    action = await repository.get_action(connector_id, req.action_name)
    if not action:
        raise HTTPException(status_code=404, detail="Connector action not found")
    return await repository.grant_permission(
        tenant_id=member.organization_id,
        workspace_id=req.workspace_id,
        employee_id=req.employee_id,
        user_id=req.user_id,
        connector_id=connector_id,
        action_name=req.action_name,
        allowed_scopes=req.allowed_scopes,
        approval_required=req.approval_required,
    )


@router.delete("/{connector_id}/permissions/{action_name}")
async def revoke_connector_permission(
    connector_id: str,
    action_name: str,
    workspace_id: str = Query(default="default"),
    employee_id: str = Query(...),
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_connector_permission", connector_id)
    repository = await ensure_registry()
    await repository.revoke_permission(
        tenant_id=member.organization_id,
        workspace_id=workspace_id,
        employee_id=employee_id,
        connector_id=connector_id,
        action_name=action_name,
    )
    return {"connector_id": connector_id, "action_name": action_name, "revoked": True}


@router.post("/{connector_id}/actions/{action_name}/execute")
async def execute_connector_action(
    connector_id: str,
    action_name: str,
    req: ExecuteConnectorRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "execute_connector_action", connector_id)
    repository = await ensure_registry()
    result = await QueuedConnectorExecutionService(repository, connector_execution_queue()).enqueue(
        connector_id=connector_id,
        action_name=action_name,
        arguments=req.arguments,
        context=AgentContext(
            id=req.employee_id or member.id,
            org_id=member.organization_id,
            member_id=member.id,
            workspace_id=req.workspace_id,
        ),
    )
    return {
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


# Compatibility helper retained for old tests only. It now routes through the
# real internal connector framework instead of claiming Gmail/browser support.
async def execute_connector_proof(
    *,
    connector_id: str,
    provider: str,
    member: Member,
    req: ConnectorProofRequest | None = None,
) -> dict[str, Any]:
    repository = await ensure_registry()
    installed = await repository.install_connector(
        "internal_echo",
        tenant_id=member.organization_id,
        workspace_id="default",
        installed_by=member.id,
    )
    await repository.grant_permission(
        tenant_id=member.organization_id,
        workspace_id="default",
        employee_id=member.id,
        user_id=member.id,
        connector_id=installed["id"],
        action_name="echo",
        allowed_scopes=["internal.echo"],
        approval_required=False,
    )
    result = await ConnectorExecutionService(repository, adapter_registry()).execute(
        connector_id="internal_echo",
        action_name="echo",
        arguments={"message": (req.message if req else "Chronos connector proof")},
        context=AgentContext(id=member.id, org_id=member.organization_id, member_id=member.id, workspace_id="default"),
    )
    return {"status": result.status, "detail": result.output or result.error, "tool": "internal_echo.echo"}
