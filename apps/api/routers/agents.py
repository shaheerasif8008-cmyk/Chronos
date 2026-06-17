from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from core import agents, audit
from core.auth import get_current_member
from core.db import engine, reflect_table
from core.models import Member
from routers.tasks import create_task_record
from runtime import task_runner

router = APIRouter(prefix="/agents", tags=["agents"])


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
    config: dict[str, Any] = Field(default_factory=dict)


class InboundPublicationRequest(BaseModel):
    external_conversation_id: str
    external_message_id: str | None = None
    sender: dict[str, Any] = Field(default_factory=dict)
    text: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class AgentCommandRequest(BaseModel):
    command: str


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


@router.post("/publications/{publication_id}/inbound")
async def receive_publication_message(
    publication_id: str,
    req: InboundPublicationRequest,
    x_chronos_agent_token: str | None = Header(default=None, alias="X-Chronos-Agent-Token"),
) -> dict[str, Any]:
    publication, agent = await agents.get_publication(publication_id)
    if not x_chronos_agent_token or x_chronos_agent_token != publication.get("inbound_token"):
        raise HTTPException(status_code=403, detail="Invalid publication token")

    member = Member(
        id=agent.get("created_by") or "chronos",
        organization_id=agent["organization_id"],
        email="agent-publication@chronos.local",
        role="agent",
    )
    goal = f"[{publication['target']}:{publication.get('external_channel_id') or 'inbound'}] {req.text}"
    project_id = (agent.get("project_ids") or [None])[0]
    task_id = await create_task_record(
        goal=goal,
        member=member,
        triggered_by=f"agent_publication:{publication_id}",
        persona_id=agent["id"],
        model=agent.get("model"),
        mode="agent",
        project_id=project_id,
        original_message=req.text,
        router_decision={
            "mode": "agent",
            "ui_title": req.text,
            "metadata": {
                "agent_id": agent["id"],
                "publication_id": publication_id,
                "publish_target": publication["target"],
            },
        },
        attachments_context=req.attachments,
        conversation_context=[{"role": "external", "content": req.text, "metadata": req.model_dump()}],
    )
    await _attach_agent_state(
        task_id,
        agent["organization_id"],
        agent_profile=agent,
        agent_publication={
            "id": publication_id,
            "target": publication["target"],
            "external_channel_id": publication.get("external_channel_id"),
            "external_conversation_id": req.external_conversation_id,
            "external_message_id": req.external_message_id,
            "sender": req.sender,
        },
    )
    await agents._event(
        agent_id=agent["id"],
        organization_id=agent["organization_id"],
        publication_id=publication_id,
        event_type="external_message_received",
        payload=req.model_dump(),
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
            "external_conversation_id": req.external_conversation_id,
            "external_message_id": req.external_message_id,
        },
    )
    await agents._event(
        agent_id=agent["id"],
        organization_id=agent["organization_id"],
        publication_id=publication_id,
        task_id=task_id,
        event_type="agent_publication_task_created",
        payload={"task_id": task_id, "external_conversation_id": req.external_conversation_id},
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
    return {"task_id": task_id, "status": "queued", "agent_id": agent["id"], "publication_id": publication_id}


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
