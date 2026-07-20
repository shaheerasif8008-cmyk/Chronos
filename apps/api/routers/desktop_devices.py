"""Web-session and device-token APIs for the authenticated desktop bridge."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from core import permissions
from core.auth import get_current_member
from core.desktop_bridge import DesktopBridgeError, desktop_bridge
from core.models import Member


router = APIRouter(prefix="/desktop-devices", tags=["desktop-devices"])
_DEVICE_ADMIN_ROLES = {"admin", "owner"}


def _visible_member_id(member: Member) -> str | None:
    return None if member.role in _DEVICE_ADMIN_ROLES else str(member.id)


def _http_error(exc: DesktopBridgeError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail})


def _device_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing device authorization")
    try:
        scheme, token = authorization.split(" ", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid device authorization") from exc
    if scheme.lower() not in {"bearer", "device"} or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid device authorization")
    return token.strip()


async def _authenticated_device(
    device_id: str,
    authorization: str | None,
) -> dict[str, Any]:
    try:
        return await desktop_bridge.authenticate(
            _device_token(authorization), expected_device_id=device_id
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


class PairDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair_code: str = Field(min_length=8, max_length=32)
    name: str = Field(min_length=1, max_length=120)
    platform: str = Field(min_length=1, max_length=30)
    client_version: str | None = Field(default=None, max_length=80)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_version: str | None = Field(default=None, max_length=80)
    capabilities: dict[str, Any] | None = None


class PollCommandsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=1, ge=1, le=10)
    lease_seconds: int = Field(default=30, ge=10, le=60)


class SubmitResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str = Field(min_length=1, max_length=160)
    status: str
    error_code: str | None = Field(default=None, max_length=120)
    result_b64: str
    signature: str = Field(min_length=64, max_length=64)


class RegisterGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_grant_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)


@router.post("/pair-codes", status_code=201)
async def create_pair_code(member: Member = Depends(get_current_member)) -> dict[str, Any]:
    await permissions.check(member, "create_desktop_pair_code", member.organization_id)
    try:
        return await desktop_bridge.create_pair_code(
            organization_id=str(member.organization_id), member_id=str(member.id)
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/pair", status_code=201)
async def pair_device(req: PairDeviceRequest) -> dict[str, Any]:
    """Exchange one short-lived, one-time user code for device credentials."""

    try:
        return await desktop_bridge.pair_device(
            pair_code=req.pair_code,
            name=req.name,
            platform=req.platform,
            client_version=req.client_version,
            capabilities=req.capabilities,
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.get("/")
async def list_devices(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await permissions.check(member, "list_desktop_devices", member.organization_id)
    return await desktop_bridge.list_devices(
        organization_id=str(member.organization_id), member_id=_visible_member_id(member)
    )


@router.post("/{device_id}/heartbeat")
async def heartbeat(
    req: HeartbeatRequest,
    device_id: str = Path(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = await _authenticated_device(device_id, authorization)
    try:
        return await desktop_bridge.heartbeat(
            device,
            client_version=req.client_version,
            capabilities=req.capabilities,
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/{device_id}/commands/poll")
async def poll_commands(
    req: PollCommandsRequest,
    device_id: str = Path(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = await _authenticated_device(device_id, authorization)
    try:
        commands = await desktop_bridge.lease(
            device, limit=req.limit, lease_seconds=req.lease_seconds
        )
        return {"commands": commands}
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/{device_id}/commands/{command_id}/result")
async def submit_result(
    req: SubmitResultRequest,
    device_id: str = Path(min_length=1, max_length=64),
    command_id: str = Path(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = await _authenticated_device(device_id, authorization)
    try:
        return await desktop_bridge.submit_result(
            device,
            command_id=command_id,
            nonce=req.nonce,
            status=req.status,
            error_code=req.error_code,
            result_b64=req.result_b64,
            signature=req.signature,
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/{device_id}/grants", status_code=201)
async def register_grant(
    req: RegisterGrantRequest,
    device_id: str = Path(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = await _authenticated_device(device_id, authorization)
    try:
        return await desktop_bridge.register_grant(
            device,
            client_grant_id=req.client_grant_id,
            display_name=req.display_name,
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/{device_id}/grants/{client_grant_id}/revoke")
async def revoke_grant_from_device(
    device_id: str = Path(min_length=1, max_length=64),
    client_grant_id: str = Path(min_length=1, max_length=128),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = await _authenticated_device(device_id, authorization)
    try:
        return await desktop_bridge.revoke_grant_from_device(
            device, client_grant_id=client_grant_id
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/{device_id}/disconnect")
async def disconnect_device(
    device_id: str = Path(min_length=1, max_length=64),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    device = await _authenticated_device(device_id, authorization)
    try:
        return await desktop_bridge.disconnect(device)
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.get("/{device_id}/grants")
async def list_device_grants(
    device_id: str,
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    await permissions.check(member, "list_desktop_device_grants", device_id)
    try:
        return await desktop_bridge.list_grants(
            organization_id=str(member.organization_id),
            device_id=device_id,
            member_id=_visible_member_id(member),
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/grants/{grant_id}/revoke")
async def revoke_grant_from_web(
    grant_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_local_computer_grant", grant_id)
    try:
        # Administrators can see every device but the existing grant ownership
        # model is member-scoped.  Resolve the owner from the visible device/grant
        # listing without ever broadening to another tenant.
        if member.role in _DEVICE_ADMIN_ROLES:
            devices = await desktop_bridge.list_devices(
                organization_id=str(member.organization_id), member_id=None
            )
            owner_id: str | None = None
            for device in devices:
                grants = await desktop_bridge.list_grants(
                    organization_id=str(member.organization_id),
                    device_id=str(device["id"]),
                    member_id=None,
                )
                match = next((grant for grant in grants if str(grant["id"]) == grant_id), None)
                if match:
                    owner_id = str(match["member_id"])
                    break
            if owner_id is None:
                raise DesktopBridgeError("grant_not_found", "Folder grant not found", status_code=404)
        else:
            owner_id = str(member.id)
        return await desktop_bridge.revoke_grant_from_web(
            organization_id=str(member.organization_id),
            member_id=owner_id,
            grant_id=grant_id,
            actor_id=str(member.id),
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc


@router.post("/{device_id}/revoke")
async def revoke_device_from_web(
    device_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "revoke_desktop_device", device_id)
    try:
        return await desktop_bridge.revoke_device(
            device_id=device_id,
            organization_id=str(member.organization_id),
            member_id=_visible_member_id(member),
            actor_id=str(member.id),
        )
    except DesktopBridgeError as exc:
        raise _http_error(exc) from exc
