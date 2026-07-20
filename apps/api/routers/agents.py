from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from base64 import b64decode
from datetime import datetime, timezone
from email.parser import Parser
from email.utils import parseaddr
from typing import Any

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from core import agent_publications, agents, audit, permissions
from core.auth import get_current_member
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member
from routers.tasks import create_task_record
from runtime import task_runner

router = APIRouter(prefix="/agents", tags=["agents"])
_MAX_PUBLICATION_BODY = 1_000_000


class AgentProfileRequest(BaseModel):
    profile_kind: str = "agent"
    name: str
    role: str
    template_id: str | None = None
    instructions: str
    personality: str | None = None
    model: str | None = None
    tool_grants: list[str] = Field(default_factory=list)
    connector_grants: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    connected_accounts: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)
    memory_scopes: list[dict[str, Any]] = Field(default_factory=list)
    autonomy_level: str = "supervised"
    approval_policy: dict[str, Any] = Field(default_factory=dict)
    schedule_permissions: dict[str, Any] = Field(default_factory=dict)


class PatchAgentProfileRequest(BaseModel):
    profile_kind: str | None = None
    name: str | None = None
    role: str | None = None
    template_id: str | None = None
    instructions: str | None = None
    personality: str | None = None
    model: str | None = None
    tool_grants: list[str] | None = None
    connector_grants: list[str] | None = None
    workflows: list[str] | None = None
    connected_accounts: list[str] | None = None
    project_ids: list[str] | None = None
    memory_scopes: list[dict[str, Any]] | None = None
    autonomy_level: str | None = None
    approval_policy: dict[str, Any] | None = None
    schedule_permissions: dict[str, Any] | None = None


class RunAgentRequest(BaseModel):
    goal: str
    project_id: str | None = None
    conversation_id: str | None = None
    model: str | None = None
    mode: str | None = "agent"
    reasoning_effort: str | None = None


class PublishAgentRequest(BaseModel):
    target: str
    display_name: str | None = None
    external_channel_id: str | None = None
    binding_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class PublicationBindingRequest(BaseModel):
    provider: str
    connector_id: str
    external_tenant_id: str | None = None
    external_channel_id: str
    display_name: str | None = None


class PublicationLifecycleRequest(BaseModel):
    action: str


class InboundPublicationRequest(BaseModel):
    external_conversation_id: str = Field(min_length=1, max_length=500)
    external_message_id: str | None = Field(default=None, max_length=500)
    sender: dict[str, Any] = Field(default_factory=dict)
    text: str = Field(min_length=1, max_length=16_000)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=10)


class AgentCommandRequest(BaseModel):
    command: str


def _bounded_payload(raw: bytes) -> None:
    if len(raw) > _MAX_PUBLICATION_BODY:
        raise HTTPException(status_code=413, detail="Publication payload exceeds 1 MB")


def _require_api_key_scope(member: Member, *, write: bool) -> None:
    if member.auth_type != "api_key":
        raise HTTPException(status_code=401, detail="Use an organization API key for API publications")
    accepted = {"admin", "write"} if write else {"admin", "write", "read"}
    if not accepted.intersection(member.api_key_scopes):
        raise HTTPException(status_code=403, detail=f"API key requires {'write' if write else 'read'} scope")


def _safe_external_attachments(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=422, detail="Attachment metadata must be an object")
        # Public ingress accepts references only. File bytes must go through the
        # authenticated upload scanner instead of entering task context here.
        entry = {
            key: str(item[key])[:500]
            for key in ("id", "name", "filename", "content_type", "url")
            if item.get(key) is not None
        }
        entry["untrusted_content"] = True
        clean.append(entry)
    return clean


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def _parse_command(command: str) -> dict[str, Any]:
    text = command.strip()
    lowered = text.lower()
    kind = "assistant" if "assistant" in lowered and "agent" not in lowered else "agent"
    action = "list"
    if any(word in lowered for word in ("create", "make", "build", "new ")):
        action = "create"
    elif any(word in lowered for word in ("edit", "update", "change", "modify")):
        action = "update"

    fields: dict[str, Any] = {"profile_kind": kind}
    for raw_part in text.replace("\n", " ").split("|"):
        if ":" not in raw_part:
            continue
        key, raw_value = raw_part.split(":", 1)
        key = key.strip().lower().replace(" ", "_")
        value = raw_value.strip()
        if not value:
            continue
        if key in {"kind", "type"}:
            fields["profile_kind"] = "assistant" if value.lower().startswith("assistant") else "agent"
        elif key in {"name", "role", "personality", "instructions", "autonomy_level", "template_id"}:
            fields[key] = value
        elif key in {"purpose", "function"}:
            fields["instructions"] = value
        elif key in {"tools", "tool_grants", "allowed_tools"}:
            fields["tool_grants"] = _split_values(value)
        elif key in {"connectors", "connector_grants", "connected_accounts", "accounts"}:
            fields["connector_grants"] = _split_values(value)
            fields["connected_accounts"] = _split_values(value)
        elif key in {"workflows", "target_workflow", "workflow"}:
            fields["workflows"] = _split_values(value)
        elif key in {"memory", "memory_scopes", "memory_scope"}:
            fields["memory_scopes"] = [{"scope": item} for item in _split_values(value)]
        elif key in {"approval", "approval_rules", "approval_policy", "approval_behavior"}:
            fields["approval_policy"] = {"rule": value}
        elif key in {"schedule", "trigger", "triggers"}:
            fields["schedule_permissions"] = {"rule": value, "allowed": value.lower() not in {"none", "off", "disabled"}}
    return {"action": action, "fields": fields}


def _missing_required(fields: dict[str, Any]) -> list[str]:
    required = ["instructions"]
    if fields.get("profile_kind") == "agent":
        required.extend(["tool_grants", "approval_policy", "workflows"])
    return [field for field in required if not fields.get(field)]


def _clarifying_questions(missing: list[str], kind: str) -> list[str]:
    labels = {
        "instructions": f"What should this {kind} do, and where should its responsibility stop?",
        "tool_grants": "Which tools should it be allowed to use?",
        "approval_policy": "What actions require approval before the agent proceeds?",
        "workflows": "What target workflow should this agent run or support?",
    }
    return [labels[field] for field in missing if field in labels]


@router.get("/templates")
async def list_agent_templates(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    return agents.templates()


@router.post("")
@router.post("/")
async def create_agent_profile(req: AgentProfileRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    return await agents.create_profile(member, req.model_dump())


@router.get("")
@router.get("/")
async def list_agent_profiles(
    profile_kind: str | None = Query(default=None),
    member: Member = Depends(get_current_member),
) -> list[dict[str, Any]]:
    return await agents.list_profiles(member, profile_kind=profile_kind)


@router.post("/command")
async def agent_command(req: AgentCommandRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    parsed = _parse_command(req.command)
    action = parsed["action"]
    fields = parsed["fields"]
    kind = fields.get("profile_kind") or "agent"

    if action == "list":
        profiles = await agents.list_profiles(member, profile_kind=kind)
        return {"status": "ok", "action": "list", "profile_kind": kind, "profiles": profiles}

    if action == "update":
        existing = await agents.list_profiles(member, profile_kind=kind)
        name = str(fields.get("name") or "").lower()
        matches = [profile for profile in existing if name and profile.get("name", "").lower() == name]
        if not matches:
            return {
                "status": "needs_clarification",
                "action": "update",
                "profile_kind": kind,
                "questions": [f"Which {kind} should I update? Include name: Existing options are {', '.join(p['name'] for p in existing) or 'none'}."],
            }
        patch = {key: value for key, value in fields.items() if key not in {"name"}}
        updated = await agents.patch_profile(member, matches[0]["id"], patch)
        return {"status": "updated", "action": "update", "profile": updated}

    missing = _missing_required(fields)
    if missing:
        return {
            "status": "needs_clarification",
            "action": "create",
            "profile_kind": kind,
            "missing": missing,
            "questions": _clarifying_questions(missing, kind),
        }
    fields.setdefault("name", "New Assistant" if kind == "assistant" else "New Agent")
    fields.setdefault("role", fields.get("instructions", kind)[:80])
    fields.setdefault("autonomy_level", "manual" if kind == "assistant" else "supervised")
    profile = await agents.create_profile(member, fields)
    return {"status": "created", "action": "create", "profile": profile}


@router.post("/publication-bindings")
async def create_publication_binding(req: PublicationBindingRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    return await agent_publications.create_binding(member, req.model_dump())


@router.get("/publication-bindings")
async def list_publication_bindings(member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    return await agent_publications.list_bindings(member)


@router.delete("/publication-bindings/{binding_id}")
async def revoke_publication_binding(binding_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    return await agent_publications.revoke_binding(member, binding_id)


@router.post("/publications/{publication_id}/lifecycle")
async def publication_lifecycle(publication_id: str, req: PublicationLifecycleRequest, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    return await agent_publications.lifecycle(member, publication_id, req.action)


@router.get("/{agent_id}")
async def get_agent_profile(agent_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    return await agents.get_profile(member, agent_id)


@router.patch("/{agent_id}")
async def patch_agent_profile(
    agent_id: str,
    req: PatchAgentProfileRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    return await agents.patch_profile(member, agent_id, req.model_dump(exclude_unset=True))


@router.delete("/{agent_id}")
async def delete_agent_profile(agent_id: str, member: Member = Depends(get_current_member)) -> dict[str, Any]:
    return await agents.delete_profile(member, agent_id)


@router.post("/{agent_id}/run")
async def run_agent_profile(
    agent_id: str,
    req: RunAgentRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    await permissions.check(member, "run_agent", agent_id)
    agent = await agents.get_profile(member, agent_id)
    project_id = req.project_id or (agent.get("project_ids") or [None])[0]
    task_id = await create_task_record(
        goal=req.goal,
        member=member,
        triggered_by=f"agent:{agent_id}",
        persona_id=agent_id,
        model=req.model or agent.get("model"),
        mode=req.mode or "agent",
        reasoning_effort=req.reasoning_effort,
        project_id=project_id,
        original_message=req.goal,
        router_decision={"mode": "agent", "ui_title": req.goal, "metadata": {"agent_id": agent_id}},
    )
    await _attach_agent_state(task_id, member.organization_id, agent_profile=agent)
    await agents._event(
        agent_id=agent_id,
        organization_id=member.organization_id,
        event_type="agent_task_created",
        task_id=task_id,
        payload={"goal": req.goal, "project_id": project_id},
    )
    await task_runner.enqueue_task(task_id)
    return {"task_id": task_id, "status": "queued", "agent_id": agent_id}


@router.post("/{agent_id}/publications")
async def publish_agent_profile(
    agent_id: str,
    req: PublishAgentRequest,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    return await agents.publish_agent(member, agent_id, req.model_dump())


@router.get("/{agent_id}/publications")
async def list_agent_publications(agent_id: str, member: Member = Depends(get_current_member)) -> list[dict[str, Any]]:
    await agents.get_profile(member, agent_id)
    return await agent_publications.list_publications(member, agent_id)


async def _queue_publication_message(
    publication_id: str,
    req: InboundPublicationRequest,
    *,
    publication: dict[str, Any],
    agent: dict[str, Any],
    external_event_id: str,
    payload_bytes: bytes,
    reply_metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    await agent_publications.enforce_rate_limit(publication)
    clean_text = agent_publications.validate_message(req.text)
    safe_attachments = _safe_external_attachments(req.attachments)
    events = await reflect_table("agent_publication_inbound_events")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    stable_task_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"chronos:{publication_id}:{external_event_id[:240]}")
    )
    try:
        async with engine.begin() as conn:
            event = (
                await conn.execute(
                    insert(events).values(
                        organization_id=publication["organization_id"],
                        publication_id=publication_id,
                        external_event_id=external_event_id[:240],
                        external_conversation_id=req.external_conversation_id[:500],
                        external_message_id=(req.external_message_id or "")[:500] or None,
                        payload_sha256=digest,
                        task_id=stable_task_id,
                    ).returning(events)
                )
            ).mappings().one()
    except IntegrityError:
        async with engine.begin() as conn:
            existing = (
                await conn.execute(select(events).where(events.c.publication_id == publication_id, events.c.external_event_id == external_event_id[:240]))
            ).mappings().one()
        if not hmac.compare_digest(str(existing["payload_sha256"]), digest):
            raise HTTPException(status_code=409, detail="External event ID was reused with a different payload")
        tasks = await reflect_table("tasks")
        async with engine.begin() as conn:
            task_exists = (
                await conn.execute(
                    select(tasks.c.id).where(
                        tasks.c.id == existing["task_id"],
                        tasks.c.organization_id == publication["organization_id"],
                    )
                )
            ).scalar_one_or_none()
        if task_exists:
            return {"accepted": True, "duplicate": True, "task_id": str(task_exists), "publication_id": publication_id}
        event = existing

    member = Member(
        id=agent.get("created_by") or "chronos",
        organization_id=agent["organization_id"],
        email="agent-publication@chronos.local",
        role="agent",
    )
    # One durable Chronos conversation per provider conversation. The link is
    # tenant-scoped and every inbound task points at both sides of the mapping.
    links = await reflect_table("agent_publication_links")
    async with engine.begin() as conn:
        link = (
            await conn.execute(select(links).where(links.c.publication_id == publication_id, links.c.external_conversation_id == req.external_conversation_id))
        ).mappings().first()
    if link:
        conversation_id = str(link["conversation_id"])
    else:
        from routers.chat import _create_conversation

        conversation_id = await _create_conversation(member, f"{agent['name']} via {publication['target']}", (agent.get("project_ids") or [None])[0])
        try:
            async with engine.begin() as conn:
                await conn.execute(insert(links).values(organization_id=agent["organization_id"], publication_id=publication_id, external_conversation_id=req.external_conversation_id, conversation_id=conversation_id))
        except IntegrityError:
            async with engine.begin() as conn:
                existing_link = (
                    await conn.execute(select(links).where(links.c.publication_id == publication_id, links.c.external_conversation_id == req.external_conversation_id))
                ).mappings().one()
            conversation_id = str(existing_link["conversation_id"])

    goal = f"[{publication['target']}:{publication.get('external_channel_id') or 'inbound'}] {clean_text}"
    project_id = (agent.get("project_ids") or [None])[0]
    workspace_id = None
    try:
        workspaces = await reflect_table("workspaces")
        async with engine.begin() as conn:
            workspace_id = (
                await conn.execute(
                    select(workspaces.c.id).where(
                        workspaces.c.organization_id == agent["organization_id"],
                        workspaces.c.legacy_key == "default",
                        workspaces.c.status == "active",
                    ).limit(1)
                )
            ).scalar_one_or_none()
        workspace_id = str(workspace_id) if workspace_id else None
    except Exception:
        # The migration always provisions a default workspace. Keeping this
        # explicit fallback lets older development databases fail through the
        # canonical workspace authorization seam instead of broadening access.
        workspace_id = None
    publication_state = {
            "id": publication_id,
            "target": publication["target"],
            "external_channel_id": publication.get("external_channel_id"),
            "external_conversation_id": req.external_conversation_id,
            "external_message_id": req.external_message_id,
            "sender": {key: str(req.sender[key])[:200] for key in ("id", "name") if key in req.sender},
            "conversation_id": conversation_id,
            **(reply_metadata or {}),
    }
    profile_state = {
        "id": agent["id"],
        "name": agent["name"],
        "role": agent["role"],
        "instructions": agent["instructions"],
        "tool_grants": agent.get("tool_grants") or [],
        "connector_grants": agent.get("connector_grants") or [],
        "project_ids": agent.get("project_ids") or [],
        "memory_scopes": agent.get("memory_scopes") or [],
        "autonomy_level": agent.get("autonomy_level"),
        "approval_policy": agent.get("approval_policy") or {},
        "schedule_permissions": agent.get("schedule_permissions") or {},
    }
    try:
        task_id = await create_task_record(
            goal=goal,
            member=member,
            # Native runtime convention: a top-level task's triggered_by is the
            # Chronos conversation id. This makes the final answer persist into
            # the durable conversation.
            triggered_by=conversation_id,
            persona_id=agent["id"],
            model=agent.get("model"),
            mode="agent",
            workspace_id=workspace_id,
            project_id=project_id,
            original_message=clean_text,
            router_decision={
                "mode": "agent",
                "ui_title": clean_text,
                "metadata": {
                    "agent_id": agent["id"],
                    "publication_id": publication_id,
                    "publish_target": publication["target"],
                },
            },
            attachments_context=safe_attachments,
            conversation_context=[{"role": "external", "content": clean_text, "metadata": {"publication_id": publication_id, "external_event_id": external_event_id, "untrusted_content": True}}],
            task_id=stable_task_id,
            agent_state_patch={"agent_profile": profile_state, "agent_publication": publication_state},
        )
    except IntegrityError:
        return {"accepted": True, "duplicate": True, "task_id": stable_task_id, "publication_id": publication_id}
    from routers.chat import _save_message

    await _save_message(conversation_id, "user", clean_text, _org_id=agent["organization_id"])
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(update(links).where(links.c.publication_id == publication_id, links.c.external_conversation_id == req.external_conversation_id).values(last_task_id=task_id, updated_at=now))
        await conn.execute(update(events).where(events.c.id == event["id"]).values(task_id=task_id, status="queued", processed_at=now))
        publications = await reflect_table("agent_publications")
        await conn.execute(update(publications).where(publications.c.id == publication_id).values(last_inbound_at=now, updated_at=now))
    await agents._event(
        agent_id=agent["id"],
        organization_id=agent["organization_id"],
        publication_id=publication_id,
        event_type="external_message_received",
        payload={"external_event_id_hash": hashlib.sha256(external_event_id.encode()).hexdigest(), "payload_sha256": digest},
    )
    await audit.log(
        "external_message_received",
        agent.get("created_by"),
        "agents.publications.inbound",
        organization_id=agent["organization_id"],
        resource_type="agent_publication",
        resource_id=publication_id,
        payload={
            "agent_id": agent["id"],
            "target": publication["target"],
            "external_conversation_id_hash": hashlib.sha256(req.external_conversation_id.encode()).hexdigest(),
            "external_message_id_hash": hashlib.sha256(str(req.external_message_id or "").encode()).hexdigest(),
        },
    )
    await agents._event(
        agent_id=agent["id"],
        organization_id=agent["organization_id"],
        publication_id=publication_id,
        task_id=task_id,
        event_type="agent_publication_task_created",
        payload={"task_id": task_id, "external_conversation_id_hash": hashlib.sha256(req.external_conversation_id.encode()).hexdigest()},
    )
    await audit.log(
        "agent_publication_task_created",
        agent.get("created_by"),
        "agents.publications.task",
        organization_id=agent["organization_id"],
        resource_type="task",
        resource_id=task_id,
        payload={"agent_id": agent["id"], "publication_id": publication_id, "target": publication["target"]},
    )
    await task_runner.enqueue_task(task_id)
    return {"accepted": True, "duplicate": False, "task_id": task_id, "status": "queued", "agent_id": agent["id"], "publication_id": publication_id, "conversation_id": conversation_id}


@router.post("/publications/{publication_id}/inbound")
async def receive_publication_message(
    publication_id: str,
    req: InboundPublicationRequest,
    request: Request,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    publication, agent = await agent_publications.require_active(publication_id, target="api")
    if publication["organization_id"] != member.organization_id:
        raise HTTPException(status_code=404, detail="Publication not found")
    _require_api_key_scope(member, write=True)
    await permissions.check(member, "invoke_agent_publication", publication_id)
    raw = await request.body()
    _bounded_payload(raw)
    event_id = req.external_message_id or hashlib.sha256(raw).hexdigest()
    return await _queue_publication_message(publication_id, req, publication=publication, agent=agent, external_event_id=event_id, payload_bytes=raw)


@router.post("/publications/{publication_id}/embed/messages")
async def receive_embed_message(
    publication_id: str,
    req: InboundPublicationRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None),
) -> dict[str, Any]:
    publication, agent = await agent_publications.require_active(publication_id, target="web")
    token = (authorization or "").removeprefix("Bearer ").strip()
    await agent_publications.verify_embed(publication, token, origin)
    raw = await request.body()
    _bounded_payload(raw)
    event_id = req.external_message_id or hashlib.sha256(raw).hexdigest()
    return await _queue_publication_message(publication_id, req, publication=publication, agent=agent, external_event_id=event_id, payload_bytes=raw)


async def _publication_task_result(
    publication: dict[str, Any], task_id: str
) -> dict[str, Any]:
    tasks = await reflect_table("tasks")
    receipts = await reflect_table("notification_delivery_receipts")
    messages = await reflect_table("messages")
    async with engine.begin() as conn:
        task = (
            await conn.execute(
                select(tasks).where(
                    tasks.c.id == task_id,
                    tasks.c.organization_id == publication["organization_id"],
                )
            )
        ).mappings().first()
        if task is None:
            raise HTTPException(status_code=404, detail="Publication task not found")
        state = dict(task.get("agent_state") or {}).get("agent_publication") or {}
        if str(state.get("id") or "") != str(publication["id"]):
            raise HTTPException(status_code=404, detail="Publication task not found")
        receipt = (
            await conn.execute(
                select(receipts)
                .where(
                    receipts.c.organization_id == publication["organization_id"],
                    receipts.c.publication_id == publication["id"],
                    receipts.c.task_id == task_id,
                    receipts.c.delivery_kind == "agent_response",
                )
                .order_by(receipts.c.created_at.desc())
                .limit(1)
            )
        ).mappings().first()
        answer = None
        # The receipt is the release gate. A completed task without a receipt
        # is still between persistence and policy evaluation and fails closed.
        if task["status"] == "complete" and receipt and receipt["status"] == "delivered":
            conversation_id = str(state.get("conversation_id") or "")
            if conversation_id:
                answer = (
                    await conn.execute(
                        select(messages.c.content)
                        .where(
                            messages.c.organization_id == publication["organization_id"],
                            messages.c.conversation_id == conversation_id,
                            messages.c.role == "assistant",
                        )
                        .order_by(messages.c.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
    response_status = str(receipt["status"]) if receipt else "preparing"
    return {
        "task_id": task_id,
        "status": str(task["status"]),
        "response_status": response_status,
        "answer": str(answer) if answer is not None else None,
        "error": "Agent task failed" if task["status"] in {"failed", "dead_letter"} else None,
    }


@router.get("/publications/{publication_id}/tasks/{task_id}")
async def get_api_publication_task(
    publication_id: str,
    task_id: str,
    member: Member = Depends(get_current_member),
) -> dict[str, Any]:
    publication, _agent = await agent_publications.require_active(publication_id, target="api")
    if publication["organization_id"] != member.organization_id:
        raise HTTPException(status_code=404, detail="Publication not found")
    _require_api_key_scope(member, write=False)
    await permissions.check(member, "invoke_agent_publication", publication_id)
    return await _publication_task_result(publication, task_id)


@router.get("/publications/{publication_id}/embed/tasks/{task_id}")
async def get_embed_publication_task(
    publication_id: str,
    task_id: str,
    authorization: str | None = Header(default=None),
    origin: str | None = Header(default=None),
) -> dict[str, Any]:
    publication, _agent = await agent_publications.require_active(publication_id, target="web")
    token = (authorization or "").removeprefix("Bearer ").strip()
    await agent_publications.verify_embed(publication, token, origin)
    return await _publication_task_result(publication, task_id)


def _verify_slack_signature(raw: bytes, timestamp: str | None, signature: str | None) -> None:
    if not settings.slack_signing_secret:
        raise HTTPException(status_code=503, detail="Slack signing secret is not configured")
    try:
        numeric = int(timestamp or "")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Slack signature") from exc
    if abs(int(time.time()) - numeric) > 300:
        raise HTTPException(status_code=401, detail="Expired Slack request")
    expected = "v0=" + hmac.new(settings.slack_signing_secret.encode(), b"v0:" + str(numeric).encode() + b":" + raw, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


@router.post("/publications/{publication_id}/slack/events")
async def receive_slack_event(publication_id: str, request: Request, x_slack_request_timestamp: str | None = Header(default=None), x_slack_signature: str | None = Header(default=None)) -> dict[str, Any]:
    raw = await request.body()
    _bounded_payload(raw)
    _verify_slack_signature(raw, x_slack_request_timestamp, x_slack_signature)
    publication, agent = await agent_publications.require_active(publication_id, target="slack")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Slack event payload") from exc
    event = payload.get("event") or {}
    bindings = await reflect_table("agent_publication_bindings")
    async with engine.begin() as conn:
        binding = (await conn.execute(select(bindings).where(bindings.c.id == publication.get("binding_id"), bindings.c.organization_id == publication["organization_id"], bindings.c.status == "active"))).mappings().first()
    if not binding or str(payload.get("team_id") or "") != str(binding["external_tenant_id"]):
        raise HTTPException(status_code=403, detail="Slack workspace or channel is not bound")
    if payload.get("type") == "url_verification":
        challenge = str(payload.get("challenge") or "")
        if not challenge:
            raise HTTPException(status_code=400, detail="Slack challenge is missing")
        return {"challenge": challenge}
    if event.get("bot_id") or event.get("subtype") or event.get("type") not in {"message", "app_mention"}:
        return {"accepted": True, "ignored": True}
    if str(event.get("channel") or "") != str(binding["external_channel_id"]):
        raise HTTPException(status_code=403, detail="Slack workspace or channel is not bound")
    req = InboundPublicationRequest(external_conversation_id=str(event.get("thread_ts") or event.get("channel")), external_message_id=str(event.get("client_msg_id") or event.get("ts") or payload.get("event_id")), sender={"id": event.get("user")}, text=str(event.get("text") or ""))
    return await _queue_publication_message(publication_id, req, publication=publication, agent=agent, external_event_id=str(payload.get("event_id") or req.external_message_id), payload_bytes=raw)


def _verify_teams_token(token: str) -> dict[str, Any]:
    if not settings.teams_bot_app_id:
        raise HTTPException(status_code=503, detail="Teams bot identity is not configured")
    try:
        key = jwt.PyJWKClient(settings.teams_bot_jwks_url).get_signing_key_from_jwt(token)
        return jwt.decode(token, key.key, algorithms=["RS256"], audience=settings.teams_bot_app_id, issuer=settings.teams_bot_issuer)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid Teams authorization") from exc


@router.post("/publications/{publication_id}/teams/events")
async def receive_teams_event(publication_id: str, request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    token = (authorization or "").removeprefix("Bearer ").strip()
    _verify_teams_token(token)
    raw = await request.body()
    _bounded_payload(raw)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Teams event payload") from exc
    publication, agent = await agent_publications.require_active(publication_id, target="teams")
    if str(payload.get("type") or "message") != "message":
        return {"accepted": True, "ignored": True}
    channel_data = payload.get("channelData") or {}
    team_id = str((channel_data.get("team") or {}).get("id") or "")
    channel_id = str((channel_data.get("channel") or {}).get("id") or "")
    bindings = await reflect_table("agent_publication_bindings")
    async with engine.begin() as conn:
        binding = (await conn.execute(select(bindings).where(bindings.c.id == publication.get("binding_id"), bindings.c.organization_id == publication["organization_id"], bindings.c.status == "active"))).mappings().first()
    if not binding or channel_id != str(binding["external_channel_id"]) or team_id != str(binding["external_tenant_id"]):
        raise HTTPException(status_code=403, detail="Teams tenant or channel is not bound")
    conversation_id = str((payload.get("conversation") or {}).get("id") or channel_id)
    req = InboundPublicationRequest(external_conversation_id=conversation_id, external_message_id=str(payload.get("id") or ""), sender={"id": (payload.get("from") or {}).get("id"), "name": (payload.get("from") or {}).get("name")}, text=str(payload.get("text") or ""))
    return await _queue_publication_message(publication_id, req, publication=publication, agent=agent, external_event_id=str(payload.get("id") or hashlib.sha256(raw).hexdigest()), payload_bytes=raw)


def _verify_sendgrid_inbound_signature(
    raw: bytes, timestamp: str | None, signature: str | None
) -> None:
    public_key_pem = settings.sendgrid_inbound_public_key.strip().replace("\\n", "\n")
    if not public_key_pem:
        raise HTTPException(status_code=503, detail="SendGrid inbound signing key is not configured")
    try:
        numeric = int(timestamp or "")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid SendGrid signature") from exc
    if abs(int(time.time()) - numeric) > 300:
        raise HTTPException(status_code=401, detail="Expired SendGrid request")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Inbound signature verifier is unavailable") from exc
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode())
        key.verify(
            b64decode(signature or "", validate=True),
            str(numeric).encode() + raw,
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid SendGrid signature") from exc


@router.post("/publications/{publication_id}/email/events")
async def receive_email_event(
    publication_id: str,
    request: Request,
    signature: str | None = Header(
        default=None, alias="X-Twilio-Email-Event-Webhook-Signature"
    ),
    timestamp: str | None = Header(
        default=None, alias="X-Twilio-Email-Event-Webhook-Timestamp"
    ),
) -> dict[str, Any]:
    raw = await request.body()
    _bounded_payload(raw)
    _verify_sendgrid_inbound_signature(raw, timestamp, signature)
    publication, agent = await agent_publications.require_active(publication_id, target="email")
    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid SendGrid inbound payload") from exc
    try:
        attachment_count = int(str(form.get("attachments") or "0"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid SendGrid attachment count") from exc
    if attachment_count > 0:
        raise HTTPException(status_code=422, detail="Email attachments must be uploaded through the authenticated scanner")
    envelope: dict[str, Any] = {}
    try:
        envelope = json.loads(str(form.get("envelope") or "{}"))
    except (TypeError, ValueError):
        pass
    envelope_to = envelope.get("to") if isinstance(envelope, dict) else None
    recipient_raw = str((envelope_to or [""])[0] if isinstance(envelope_to, list) else form.get("to") or "")
    recipient = parseaddr(recipient_raw)[1].lower()
    if recipient != str(publication.get("external_channel_id") or "").lower():
        raise HTTPException(status_code=403, detail="Email recipient is not bound to this publication")
    sender_name, sender_email = parseaddr(str(form.get("from") or ""))
    if not sender_email:
        raise HTTPException(status_code=422, detail="Inbound email sender is missing")
    subject = str(form.get("subject") or "").strip()[:500]
    text_body = str(form.get("text") or "").strip()
    if not text_body:
        raise HTTPException(status_code=422, detail="Inbound email has no plain-text body")
    headers = Parser().parsestr(str(form.get("headers") or ""))
    message_id = str(headers.get("Message-ID") or hashlib.sha256(raw).hexdigest())[:500]
    normalized_subject = re.sub(r"^(?:\s*re\s*:\s*)+", "", subject, flags=re.IGNORECASE)
    thread_key = hashlib.sha256(
        f"{sender_email.lower()}\n{normalized_subject.lower()}".encode()
    ).hexdigest()
    req = InboundPublicationRequest(
        external_conversation_id=f"email:{thread_key}",
        external_message_id=message_id,
        sender={"id": sender_email.lower(), "name": sender_name[:200]},
        text=text_body,
    )
    return await _queue_publication_message(
        publication_id,
        req,
        publication=publication,
        agent=agent,
        external_event_id=message_id,
        payload_bytes=raw,
        reply_metadata={
            "reply_to_email": sender_email.lower(),
            "email_subject": subject,
        },
    )


async def _attach_agent_state(
    task_id: str,
    organization_id: str,
    *,
    agent_profile: dict[str, Any],
    agent_publication: dict[str, Any] | None = None,
) -> None:
    tasks = await reflect_table("tasks")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(tasks.c.agent_state).where(tasks.c.id == task_id, tasks.c.organization_id == organization_id)
            )
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="Task not found")
        state = dict(row[0] or {})
        state["agent_profile"] = {
            "id": agent_profile["id"],
            "name": agent_profile["name"],
            "role": agent_profile["role"],
            "instructions": agent_profile["instructions"],
            "tool_grants": agent_profile.get("tool_grants") or [],
            "connector_grants": agent_profile.get("connector_grants") or [],
            "project_ids": agent_profile.get("project_ids") or [],
            "memory_scopes": agent_profile.get("memory_scopes") or [],
            "autonomy_level": agent_profile.get("autonomy_level"),
            "approval_policy": agent_profile.get("approval_policy") or {},
            "schedule_permissions": agent_profile.get("schedule_permissions") or {},
        }
        if agent_publication is not None:
            state["agent_publication"] = agent_publication
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == task_id, tasks.c.organization_id == organization_id)
            .values(agent_state=state)
        )
