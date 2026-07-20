"""Native organization administration endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from core import admin_lifecycle, organization_api_keys, permissions
from core.auth import get_current_member
from core.models import Member


router = APIRouter(prefix="/settings/admin-lifecycle", tags=["settings", "admin-lifecycle"])


class NamedResource(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class MemberMutation(BaseModel):
    member_id: str = Field(min_length=1, max_length=200)


class WorkspaceMemberMutation(MemberMutation):
    role: Literal["owner", "editor", "viewer"]


class DestructiveConfirmation(BaseModel):
    confirmation: str = Field(min_length=1, max_length=300)


class OwnershipTransfer(DestructiveConfirmation):
    target_member_id: str = Field(min_length=1, max_length=200)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[Literal["read", "write", "admin"]] = Field(min_length=1, max_length=3)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=6000)
    expires_at: datetime | None = None


def _raise_lifecycle(exc: Exception) -> None:
    if isinstance(exc, admin_lifecycle.LifecycleNotFound):
        raise HTTPException(status_code=404, detail="Resource not found") from exc
    if isinstance(exc, admin_lifecycle.LifecycleHeld):
        raise HTTPException(status_code=409, detail="An active legal hold blocks workspace deletion") from exc
    if isinstance(exc, (admin_lifecycle.LifecycleConflict, ValueError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    raise exc


async def _admin(actor: Member, action: str) -> None:
    await permissions.check(actor, action, actor.organization_id)


@router.get("/groups")
async def get_groups(actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_native_groups")
    return await admin_lifecycle.list_groups(actor)


@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def post_group(req: NamedResource, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_native_groups")
    try:
        return await admin_lifecycle.create_group(actor, name=req.name, description=req.description)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.patch("/groups/{group_id}")
async def patch_group(group_id: str, req: NamedResource, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_native_groups")
    try:
        return await admin_lifecycle.update_group(actor, group_id, name=req.name, description=req.description)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group(group_id: str, actor: Member = Depends(get_current_member)) -> Response:
    await _admin(actor, "manage_native_groups")
    try:
        await admin_lifecycle.delete_group(actor, group_id)
    except Exception as exc:
        _raise_lifecycle(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/groups/{group_id}/members")
async def put_group_member(group_id: str, req: MemberMutation, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_native_groups")
    try:
        await admin_lifecycle.add_group_member(actor, group_id, req.member_id)
    except Exception as exc:
        _raise_lifecycle(exc)
    return {"ok": True}


@router.delete("/groups/{group_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_member(group_id: str, member_id: str, actor: Member = Depends(get_current_member)) -> Response:
    await _admin(actor, "manage_native_groups")
    try:
        await admin_lifecycle.remove_group_member(actor, group_id, member_id)
    except Exception as exc:
        _raise_lifecycle(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/workspaces")
async def get_workspaces(actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    return await admin_lifecycle.list_workspaces(actor)


@router.get("/accessible-workspaces")
async def get_accessible_workspaces(actor: Member = Depends(get_current_member)):
    await permissions.check(actor, "list_accessible_workspaces", actor.organization_id)
    return await admin_lifecycle.list_accessible_workspaces(actor)


@router.post("/workspaces", status_code=status.HTTP_201_CREATED)
async def post_workspace(req: NamedResource, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    try:
        return await admin_lifecycle.create_workspace(actor, name=req.name, description=req.description)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.patch("/workspaces/{workspace_id}")
async def patch_workspace(workspace_id: str, req: NamedResource, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    try:
        return await admin_lifecycle.update_workspace(actor, workspace_id, name=req.name, description=req.description)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.put("/workspaces/{workspace_id}/members")
async def put_workspace_member(workspace_id: str, req: WorkspaceMemberMutation, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    try:
        await admin_lifecycle.set_workspace_member(actor, workspace_id, req.member_id, req.role)
    except Exception as exc:
        _raise_lifecycle(exc)
    return {"ok": True}


@router.delete("/workspaces/{workspace_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_member(workspace_id: str, member_id: str, actor: Member = Depends(get_current_member)) -> Response:
    await _admin(actor, "manage_workspaces")
    try:
        await admin_lifecycle.remove_workspace_member(actor, workspace_id, member_id)
    except Exception as exc:
        _raise_lifecycle(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/workspaces/{workspace_id}/archive")
async def post_workspace_archive(workspace_id: str, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    try:
        return await admin_lifecycle.archive_workspace(actor, workspace_id, archived=True)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.post("/workspaces/{workspace_id}/restore")
async def post_workspace_restore(workspace_id: str, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    try:
        return await admin_lifecycle.archive_workspace(actor, workspace_id, archived=False)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.post("/workspaces/{workspace_id}/deletion")
async def post_workspace_deletion(workspace_id: str, req: DestructiveConfirmation, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    try:
        return await admin_lifecycle.request_workspace_deletion(actor, workspace_id, req.confirmation)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.delete("/workspaces/{workspace_id}/deletion")
async def delete_workspace_deletion(workspace_id: str, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_workspaces")
    try:
        return await admin_lifecycle.cancel_workspace_deletion(actor, workspace_id)
    except Exception as exc:
        _raise_lifecycle(exc)


@router.post("/ownership-transfer", status_code=status.HTTP_204_NO_CONTENT)
async def post_ownership_transfer(req: OwnershipTransfer, actor: Member = Depends(get_current_member)) -> Response:
    await _admin(actor, "transfer_organization_ownership")
    try:
        await admin_lifecycle.transfer_ownership(actor, req.target_member_id, req.confirmation)
    except Exception as exc:
        _raise_lifecycle(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/leave", status_code=status.HTTP_204_NO_CONTENT)
async def post_leave(req: DestructiveConfirmation, actor: Member = Depends(get_current_member)) -> Response:
    # Leaving is member-controlled; the transactional implementation preserves
    # the organization owner invariant and revokes every key owned by the leaver.
    try:
        await admin_lifecycle.leave_organization(actor, req.confirmation)
    except Exception as exc:
        _raise_lifecycle(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api-keys")
async def get_api_keys(actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_organization_api_keys")
    return await organization_api_keys.list_keys(actor.organization_id)


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def post_api_key(req: ApiKeyCreate, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_organization_api_keys")
    try:
        return await organization_api_keys.create_key(
            actor, name=req.name, scopes=list(req.scopes),
            rate_limit_per_minute=req.rate_limit_per_minute, expires_at=req.expires_at,
        )
    except (ValueError, PermissionError) as exc:
        _raise_lifecycle(exc)


@router.post("/api-keys/{key_id}/rotate", status_code=status.HTTP_201_CREATED)
async def post_api_key_rotation(key_id: str, actor: Member = Depends(get_current_member)):
    await _admin(actor, "manage_organization_api_keys")
    try:
        result = await organization_api_keys.rotate_key(actor, key_id)
    except (ValueError, PermissionError) as exc:
        _raise_lifecycle(exc)
    if result is None:
        raise HTTPException(status_code=404, detail="Active API key not found")
    return result


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(key_id: str, actor: Member = Depends(get_current_member)) -> Response:
    await _admin(actor, "manage_organization_api_keys")
    if not await organization_api_keys.revoke_key(actor, key_id):
        raise HTTPException(status_code=404, detail="Active API key not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
