"""Admin APIs for custom HTTPS connectors and signed inbound webhooks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from connectors.framework.repository import DatabaseConnectorRepository
from core import custom_integrations, permissions
from core.auth import get_current_member
from core.models import Member
from core.settings_store import require_admin


router = APIRouter(tags=["custom-integrations"])


class CustomHTTPActionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    method: str = Field(default="GET")
    path: str = Field(default="/", max_length=1_024)
    request_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    response_schema: dict[str, Any] | None = None
    idempotency_header: str | None = Field(default=None, max_length=64)


class CustomHTTPCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=2_048)
    auth_header: str = Field(default="Authorization", min_length=1, max_length=64)
    auth_token: str = Field(min_length=1, max_length=8_192)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    actions: list[CustomHTTPActionRequest] = Field(min_length=1, max_length=50)


class WebhookCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    event_type: str = Field(min_length=1, max_length=128)
    workspace_id: str = Field(default="default", min_length=1, max_length=128)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=600)
    workflow_id: str | None = Field(default=None, max_length=128)


class WebhookStatusRequest(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class WebhookBindRequest(BaseModel):
    workflow_id: str = Field(min_length=1, max_length=128)


def _admin(member: Member, action: str) -> None:
    require_admin(member)


async def _authorize_admin(member: Member, action: str) -> None:
    _admin(member, action)
    await permissions.check(member, action, member.organization_id)


def _http_error(exc: custom_integrations.CustomIntegrationError) -> HTTPException:
    message = str(exc)
    if "rate limit exceeded" in message:
        return HTTPException(status_code=429, detail=message)
    if "rate limiter is unavailable" in message:
        return HTTPException(status_code=503, detail=message)
    if "not found or disabled" in message:
        return HTTPException(status_code=404, detail=message)
    if "signature" in message or "timestamp" in message:
        return HTTPException(status_code=401, detail=message)
    if "1 MiB" in message:
        return HTTPException(status_code=413, detail=message)
    return HTTPException(status_code=400, detail=message)


async def _bind_webhook_workflow(
    *, endpoint: dict[str, Any], workflow_id: str, organization_id: str
) -> dict[str, Any]:
    repository = DatabaseConnectorRepository()
    workflow = await repository.get_workflow(workflow_id, tenant_id=organization_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    existing = await repository.list_workflow_triggers(
        workflow_id, tenant_id=organization_id, status="active"
    )
    for trigger in existing:
        config = trigger.get("config") or {}
        if (
            trigger.get("trigger_type") == "webhook"
            and config.get("source") == endpoint["trigger_source"]
            and config.get("event_type") == endpoint["event_type"]
        ):
            return trigger
    return await repository.create_workflow_trigger(
        tenant_id=organization_id,
        workflow_id=workflow_id,
        trigger_type="webhook",
        config={
            "source": endpoint["trigger_source"],
            "event_type": endpoint["event_type"],
            "webhook_endpoint_id": endpoint["id"],
        },
        status="active",
    )


@router.get("/connectors/custom-http")
async def list_custom_http(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await _authorize_admin(member, "manage_custom_http_connectors")
    return await custom_integrations.list_custom_http_connectors(str(member.organization_id))


@router.post("/connectors/custom-http")
async def create_custom_http(
    req: CustomHTTPCreateRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_custom_http_connectors")
    try:
        return await custom_integrations.create_custom_http_connector(
            organization_id=str(member.organization_id),
            region=str(member.region),
            member_id=str(member.id),
            name=req.name,
            base_url=req.base_url,
            auth_header=req.auth_header,
            auth_token=req.auth_token,
            actions=[action.model_dump() for action in req.actions],
            workspace_id=req.workspace_id,
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc


@router.post("/connectors/custom-http/{connector_id}/health")
async def custom_http_health(
    connector_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_custom_http_connectors")
    try:
        return await custom_integrations.healthcheck_custom_http(
            connector_id, organization_id=str(member.organization_id)
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc


@router.delete("/connectors/custom-http/{connector_id}")
async def disable_custom_http(
    connector_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_custom_http_connectors")
    try:
        await custom_integrations.disable_custom_http_connector(
            connector_id,
            organization_id=str(member.organization_id),
            member_id=str(member.id),
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc
    return {"connector_id": connector_id, "status": "disabled"}


@router.get("/connectors/webhook-endpoints")
async def list_webhook_endpoints(
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await _authorize_admin(member, "manage_webhook_endpoints")
    return await custom_integrations.list_webhook_endpoints(str(member.organization_id))


@router.post("/connectors/webhook-endpoints")
async def create_webhook_endpoint(
    req: WebhookCreateRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_webhook_endpoints")
    try:
        endpoint = await custom_integrations.create_webhook_endpoint(
            organization_id=str(member.organization_id),
            region=str(member.region),
            member_id=str(member.id),
            name=req.name,
            event_type=req.event_type,
            workspace_id=req.workspace_id,
            rate_limit_per_minute=req.rate_limit_per_minute,
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc
    if req.workflow_id:
        endpoint["workflow_trigger"] = await _bind_webhook_workflow(
            endpoint=endpoint,
            workflow_id=req.workflow_id,
            organization_id=str(member.organization_id),
        )
    return endpoint


@router.post("/connectors/webhook-endpoints/{endpoint_id}/bind")
async def bind_webhook_endpoint(
    endpoint_id: str,
    req: WebhookBindRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_webhook_endpoints")
    endpoints = await custom_integrations.list_webhook_endpoints(str(member.organization_id))
    endpoint = next((item for item in endpoints if item["id"] == endpoint_id), None)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return await _bind_webhook_workflow(
        endpoint=endpoint,
        workflow_id=req.workflow_id,
        organization_id=str(member.organization_id),
    )


@router.post("/connectors/webhook-endpoints/{endpoint_id}/rotate")
async def rotate_webhook_endpoint(
    endpoint_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_webhook_endpoints")
    try:
        return await custom_integrations.rotate_webhook_secret(
            endpoint_id,
            organization_id=str(member.organization_id),
            member_id=str(member.id),
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc


@router.patch("/connectors/webhook-endpoints/{endpoint_id}")
async def update_webhook_endpoint(
    endpoint_id: str,
    req: WebhookStatusRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_webhook_endpoints")
    try:
        return await custom_integrations.set_webhook_status(
            endpoint_id,
            organization_id=str(member.organization_id),
            member_id=str(member.id),
            status=req.status,
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc


@router.post("/connectors/webhook-endpoints/{endpoint_id}/test")
async def test_webhook_endpoint(
    endpoint_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await _authorize_admin(member, "manage_webhook_endpoints")
    try:
        return await custom_integrations.test_webhook_endpoint(
            endpoint_id,
            organization_id=str(member.organization_id),
            member_id=str(member.id),
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc


async def _bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > custom_integrations.MAX_WEBHOOK_PAYLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Webhook payload exceeded the 1 MiB limit")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > custom_integrations.MAX_WEBHOOK_PAYLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Webhook payload exceeded the 1 MiB limit")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhooks/inbound/{public_id}")
async def inbound_webhook(public_id: str, request: Request) -> dict[str, Any]:
    payload = await _bounded_body(request)
    try:
        return await custom_integrations.receive_webhook(
            public_id=public_id,
            timestamp=request.headers.get("x-chronos-timestamp", ""),
            signature=request.headers.get("x-chronos-signature", ""),
            external_event_id=request.headers.get("x-chronos-event-id", ""),
            payload_bytes=payload,
        )
    except custom_integrations.CustomIntegrationError as exc:
        raise _http_error(exc) from exc
