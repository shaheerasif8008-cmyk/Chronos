from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, insert, select, update
from sqlalchemy.sql import func

from connectors import vault
from connectors.gmail import oauth_finish, oauth_start_url
from core import audit, permissions, tool_broker
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import AgentContext, Member, ToolResult

router = APIRouter(prefix="/connectors", tags=["connectors"])


class ConnectorOut(BaseModel):
    id: str
    provider: str
    account_handle: str | None
    status: str
    connected_at: str | None
    last_used_at: str | None


class ConnectorProofRequest(BaseModel):
    to: str = "operator@example.com"
    subject: str = "Chronos connector proof"
    body: str = "This draft proves Gmail actions route through the Chronos tool broker."
    url: str = "https://example.com"


async def _row_to_out(row: dict) -> ConnectorOut:
    return ConnectorOut(
        id=row["id"],
        provider=row["provider"],
        account_handle=row.get("account_handle"),
        status=row["status"],
        connected_at=str(row["connected_at"]) if row.get("connected_at") else None,
        last_used_at=str(row["last_used_at"]) if row.get("last_used_at") else None,
    )


def _proof_tool_args(provider: str, req: ConnectorProofRequest) -> tuple[str, dict]:
    if provider == "gmail":
        return "gmail.draft", {
            "to": req.to,
            "subject": req.subject,
            "body": req.body,
        }
    if provider == "browser":
        return "browser.fetch", {"url": req.url}
    raise ValueError(f"No proof action for provider: {provider}")


async def _mark_connector_used(connector_id: str) -> None:
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        await conn.execute(
            update(connectors)
            .where(connectors.c.id == connector_id)
            .values(last_used_at=func.now())
        )


async def _audit_connector_proof(
    *,
    member_id: str,
    tool: str | None,
    connector_id: str,
    status: str,
) -> None:
    await audit.log(
        "connector_proof",
        member_id,
        tool or "connector.proof",
        resource_type="connectors",
        resource_id=connector_id,
        payload={"tool": tool},
        decision=status,
    )


async def execute_connector_proof(
    *,
    connector_id: str,
    provider: str,
    member: Member,
    req: ConnectorProofRequest | None = None,
) -> dict:
    proof_req = req or ConnectorProofRequest()
    try:
        tool, args = _proof_tool_args(provider, proof_req)
    except ValueError as exc:
        return {"status": "unknown_provider", "detail": str(exc), "tool": None}

    agent = AgentContext(id=f"connector-proof:{member.id}", org_id=member.organization_id, member_id=member.id)
    try:
        result = await tool_broker.execute(agent, tool, args)
        status = "ok"
        detail = result.summary
    except Exception as exc:
        status = "error"
        detail = str(exc)

    await _mark_connector_used(connector_id)
    await _audit_connector_proof(
        member_id=member.id,
        tool=tool,
        connector_id=connector_id,
        status=status,
    )
    return {"status": status, "detail": detail, "tool": tool}


@router.get("/")
async def list_connectors(member: Member = Depends(get_current_member)) -> list[ConnectorOut]:
    await permissions.check(member, "list_connectors", member.organization_id)
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    connectors.c.id,
                    connectors.c.provider,
                    connectors.c.account_handle,
                    connectors.c.status,
                    connectors.c.connected_at,
                    connectors.c.last_used_at,
                ).where(connectors.c.organization_id == member.organization_id)
                .order_by(connectors.c.connected_at.desc())
            )
        ).mappings().all()
    return [await _row_to_out(dict(r)) for r in rows]


@router.get("/{connector_id}")
async def get_connector(connector_id: str, member: Member = Depends(get_current_member)) -> ConnectorOut:
    await permissions.check(member, "read_connector", connector_id)
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(
                    connectors.c.id,
                    connectors.c.provider,
                    connectors.c.account_handle,
                    connectors.c.status,
                    connectors.c.connected_at,
                    connectors.c.last_used_at,
                ).where(
                    connectors.c.id == connector_id,
                    connectors.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found")
    return await _row_to_out(dict(row))


@router.delete("/{connector_id}")
async def disconnect_connector(connector_id: str, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "delete_connector", connector_id)
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(connectors.c.vault_ref).where(
                    connectors.c.id == connector_id,
                    connectors.c.organization_id == member.organization_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Connector not found")
        vault_ref = row
        await conn.execute(
            delete(connectors).where(connectors.c.id == connector_id)
        )

    # Invalidate vault cache
    await vault.delete(vault_ref, actor_id=member.id, org_id=member.organization_id)
    await audit.log("connector_delete", member.id, "connector.disconnect",
                    resource_type="connectors", resource_id=connector_id)
    return {"id": connector_id, "disconnected": True}


@router.post("/{connector_id}/test")
async def test_connector(
    connector_id: str,
    req: ConnectorProofRequest | None = None,
    member: Member = Depends(get_current_member),
) -> dict:
    await permissions.check(member, "test_connector", connector_id)
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(connectors.c.provider).where(
                    connectors.c.id == connector_id,
                    connectors.c.organization_id == member.organization_id,
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found")

    return await execute_connector_proof(
        connector_id=connector_id,
        provider=row["provider"],
        member=member,
        req=req,
    )


@router.post("/browser/enable")
async def enable_browser_connector(member: Member = Depends(get_current_member)) -> ConnectorOut:
    await permissions.check(member, "connect_browser", member.organization_id)
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        existing = (
            await conn.execute(
                select(
                    connectors.c.id,
                    connectors.c.provider,
                    connectors.c.account_handle,
                    connectors.c.status,
                    connectors.c.connected_at,
                    connectors.c.last_used_at,
                ).where(
                    connectors.c.organization_id == member.organization_id,
                    connectors.c.provider == "browser",
                    connectors.c.status == "active",
                )
            )
        ).mappings().first()
        if existing is not None:
            return await _row_to_out(dict(existing))

        result = await conn.execute(
            insert(connectors).values(
                organization_id=member.organization_id,
                region=member.region,
                provider="browser",
                account_handle="local browser sandbox",
                vault_ref="browser:local",
                status="active",
                scopes=["browser.fetch", "browser.search", "browser.extract_contacts"],
            ).returning(
                connectors.c.id,
                connectors.c.provider,
                connectors.c.account_handle,
                connectors.c.status,
                connectors.c.connected_at,
                connectors.c.last_used_at,
            )
        )
        row = result.mappings().first()

    await audit.log(
        "connector_connected",
        member.id,
        "connector.browser.enabled",
        resource_type="connectors",
        resource_id=row["id"] if row else "browser",
    )
    return await _row_to_out(dict(row))


# ---------------------------------------------------------------------------
# Gmail OAuth flow
# ---------------------------------------------------------------------------

@router.get("/gmail/oauth-start")
async def gmail_oauth_start(member: Member = Depends(get_current_member)) -> RedirectResponse:
    await permissions.check(member, "connect_gmail", member.organization_id)
    try:
        url = await oauth_start_url(member_id=member.id, org_id=member.organization_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    await audit.log("connector_oauth_start", member.id, "connector.gmail.oauth_start",
                    resource_type="connectors", resource_id="gmail")
    return RedirectResponse(url=url)


@router.get("/gmail/oauth-url")
async def gmail_oauth_url(member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "connect_gmail", member.organization_id)
    try:
        url = await oauth_start_url(member_id=member.id, org_id=member.organization_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    await audit.log("connector_oauth_start", member.id, "connector.gmail.oauth_start",
                    resource_type="connectors", resource_id="gmail")
    return {"url": url}


@router.get("/gmail/oauth-callback")
async def gmail_oauth_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),   # member_id passed through Composio state
    error: str = Query(default=""),
) -> dict:
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")

    org_id = settings.org_id   # Phase 1: single-tenant
    member_id = state or "unknown"

    try:
        credentials = await oauth_finish(code=code, state=state, org_id=org_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    # Store credentials in vault
    vault_ref = await vault.store(
        connector_id=f"gmail:{member_id}",
        credentials=credentials,
        org_id=org_id,
    )

    # Persist connector record
    connectors = await reflect_table("connectors")
    async with engine.begin() as conn:
        result = await conn.execute(
            insert(connectors).values(
                organization_id=org_id,
                provider="gmail",
                account_handle=member_id,
                vault_ref=vault_ref,
                status="active",
                scopes=["gmail.read", "gmail.compose", "gmail.send"],
            ).returning(connectors.c.id)
        )
        connector_id = result.scalar_one()

    await audit.log("connector_connected", member_id, "connector.gmail.connected",
                    resource_type="connectors", resource_id=str(connector_id))

    return {"status": "connected", "connector_id": str(connector_id), "provider": "gmail"}
