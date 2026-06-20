from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException
from sqlalchemy import insert, select, update
from sqlalchemy.sql import func

from core import audit, permissions
from core.config import settings
from core.db import engine, reflect_table
from core.models import Member


def _tpl(
    id: str,
    name: str,
    role: str,
    category: str,
    description: str,
    instructions: str,
    tool_grants: list[str],
    connector_grants: list[str],
    memory_scopes: list[str],
    approval_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "name": name,
        "role": role,
        "category": category,
        "description": description,
        "instructions": instructions,
        "tool_grants": tool_grants,
        "connector_grants": connector_grants,
        "memory_scopes": memory_scopes,
        "approval_policy": approval_policy or {"risky_writes": "require_approval", "external_replies": "require_approval"},
    }


# Ready-to-use agent catalog. Each entry instantiates a runnable agent profile
# with one click; freeform creation/editing happens conversationally via /agent.
AGENT_TEMPLATES: tuple[dict[str, Any], ...] = (
    # --- Research & knowledge ---
    _tpl("research", "Research Analyst", "research analyst", "Research",
         "Source-grounded research, synthesis, and cited brief creation.",
         "Research the requested topic across credible sources, synthesize findings, and produce a concise cited brief. Ask for approval before any external reply.",
         ["web.search", "research.run", "artifact.write"], ["google_drive", "notion", "slack"], ["project", "workspace"]),
    _tpl("deep_research", "Deep Research Agent", "deep research specialist", "Research",
         "Multi-hour deep research with source comparison and a structured report.",
         "Run an exhaustive multi-source investigation. Compare claims across sources, flag disagreements, and deliver a structured report with citations and a confidence note.",
         ["web.search", "research.run", "browser.open", "artifact.write"], ["google_drive", "notion"], ["project", "workspace"]),
    _tpl("market_research", "Market Researcher", "market analyst", "Research",
         "Market sizing, competitor scans, and trend analysis.",
         "Analyze the market or competitor landscape requested. Estimate sizing, map competitors, and summarize trends with sources.",
         ["web.search", "research.run", "artifact.write"], ["google_drive", "notion"], ["project", "workspace"]),
    _tpl("literature_review", "Literature Reviewer", "research librarian", "Research",
         "Academic and technical literature reviews with citations.",
         "Locate and summarize the most relevant literature on the topic. Produce an annotated bibliography and a synthesis of key findings.",
         ["web.search", "research.run", "artifact.write"], ["google_drive", "notion"], ["project"]),

    # --- Sales & marketing ---
    _tpl("sales_sdr", "Sales SDR", "sales development representative", "Sales",
         "Account research, qualification, and approved outreach drafting.",
         "Research target accounts, qualify against the ICP, and draft personalized outreach. Always draft — never send — and request approval.",
         ["web.search", "connector.search", "gmail.draft"], ["hubspot", "salesforce", "gmail", "linkedin"], ["workspace", "project"],
         {"external_replies": "require_approval", "crm_writes": "require_approval"}),
    _tpl("account_executive", "Account Executive", "account executive", "Sales",
         "Deal research, follow-ups, and CRM hygiene with approvals.",
         "Track open deals, draft follow-ups, and keep CRM records tidy. Draft all external messages for approval.",
         ["web.search", "connector.search", "gmail.draft", "task.create"], ["hubspot", "salesforce", "gmail"], ["workspace", "project"],
         {"external_replies": "require_approval", "crm_writes": "require_approval"}),
    _tpl("marketing_content", "Marketing Content Writer", "content marketer", "Marketing",
         "Blog posts, landing copy, and campaign assets.",
         "Draft on-brand marketing content for the requested channel. Match tone guidelines and produce ready-to-edit artifacts.",
         ["web.search", "artifact.write"], ["google_drive", "notion"], ["workspace", "project"]),
    _tpl("social_media", "Social Media Manager", "social media strategist", "Marketing",
         "Social posts, scheduling drafts, and engagement summaries.",
         "Draft social posts tailored per platform and summarize engagement. Posting to social always requires approval.",
         ["web.search", "artifact.write", "task.create"], ["slack", "notion"], ["workspace"],
         {"social_post": "require_approval", "external_replies": "require_approval"}),
    _tpl("seo_specialist", "SEO Specialist", "seo analyst", "Marketing",
         "Keyword research, on-page audits, and content briefs.",
         "Perform keyword research and on-page SEO audits, then produce prioritized content briefs with target terms.",
         ["web.search", "research.run", "artifact.write"], ["google_drive"], ["workspace", "project"]),
    _tpl("email_campaign", "Email Campaign Agent", "email marketer", "Marketing",
         "Email sequences and newsletter drafts with approvals.",
         "Draft email sequences and newsletters. Always draft for approval before any send.",
         ["artifact.write", "gmail.draft"], ["gmail", "google_drive"], ["workspace"],
         {"external_replies": "require_approval"}),

    # --- Engineering & data ---
    _tpl("engineering", "Engineering Agent", "software engineer", "Engineering",
         "Repository inspection, coding tasks, tests, and review assistance.",
         "Inspect the repository, implement the requested change with tests, and summarize the diff. Repo writes and PRs require approval.",
         ["repo.open", "repo.read", "repo.write", "repo.test"], ["github", "linear", "jira"], ["project", "task"],
         {"repo_writes": "require_approval", "pull_requests": "require_approval"}),
    _tpl("code_reviewer", "Code Reviewer", "code reviewer", "Engineering",
         "Pull request review for correctness, style, and risk.",
         "Review the diff for correctness bugs and clear cleanups. Report findings concisely; do not modify code without approval.",
         ["repo.open", "repo.read", "repo.test"], ["github", "linear"], ["project"],
         {"repo_writes": "require_approval"}),
    _tpl("devops", "DevOps Agent", "devops engineer", "Engineering",
         "CI/CD inspection, infra checks, and deployment readiness.",
         "Inspect CI/CD and infrastructure state, surface failures, and prepare deployment checklists. Deploys require approval.",
         ["repo.open", "repo.read", "task.create"], ["github"], ["project", "workspace"],
         {"deploy": "require_approval"}),
    _tpl("qa_engineer", "QA Engineer", "qa engineer", "Engineering",
         "Test planning, reproduction, and regression checks.",
         "Write reproduction steps and test cases for the issue, then verify fixes against them.",
         ["repo.open", "repo.read", "repo.test", "artifact.write"], ["github", "jira"], ["project"]),
    _tpl("data_analysis", "Data Analyst", "data analyst", "Data",
         "Dataset analysis, charts, reports, and metric diagnostics.",
         "Analyze the dataset, produce charts and a written summary, and diagnose anomalies in the metrics.",
         ["data.run", "artifact.write", "code.python"], ["google_drive", "airtable", "stripe"], ["project", "workspace"]),
    _tpl("data_engineer", "Data Engineer", "data engineer", "Data",
         "Pipeline checks, schema review, and data quality audits.",
         "Inspect pipelines and schemas, audit data quality, and report issues with suggested fixes.",
         ["data.run", "code.python", "task.create"], ["google_drive", "airtable"], ["project"]),
    _tpl("bi_analyst", "BI Dashboard Analyst", "business intelligence analyst", "Data",
         "KPI tracking, dashboards, and executive metric summaries.",
         "Track KPIs, assemble dashboard-ready summaries, and explain notable metric changes for executives.",
         ["data.run", "artifact.write"], ["google_drive", "airtable", "stripe"], ["workspace", "org"]),

    # --- Operations & productivity ---
    _tpl("executive_assistant", "Executive Assistant", "executive assistant", "Productivity",
         "Calendar, inbox, briefing, and follow-up workflows with approvals.",
         "Manage calendar and inbox, prepare daily briefings, and draft follow-ups. External replies and calendar writes require approval.",
         ["gmail.search", "calendar.read", "task.create"], ["gmail", "google_calendar", "slack"], ["personal", "workspace"],
         {"external_replies": "require_approval", "calendar_writes": "require_approval"}),
    _tpl("operations", "Operations Agent", "operations coordinator", "Operations",
         "Recurring operational checks, workflows, and handoffs.",
         "Run recurring operational checks and workflows, then hand off results. Workflow writes require approval.",
         ["task.create", "workflow.run", "connector.search"], ["slack", "teams", "linear", "jira"], ["workspace", "org"],
         {"workflow_writes": "require_approval", "external_replies": "require_approval"}),
    _tpl("project_manager", "Project Manager", "project manager", "Operations",
         "Status tracking, standup digests, and risk surfacing.",
         "Track project status across tools, produce standup digests, and surface risks and blockers.",
         ["task.create", "connector.search", "artifact.write"], ["linear", "jira", "slack"], ["project", "workspace"]),
    _tpl("meeting_notetaker", "Meeting Note-Taker", "meeting assistant", "Productivity",
         "Meeting summaries, action items, and follow-up drafts.",
         "Summarize the meeting, extract decisions and action items with owners, and draft follow-ups for approval.",
         ["artifact.write", "task.create", "gmail.draft"], ["google_calendar", "slack", "notion"], ["personal", "workspace"],
         {"external_replies": "require_approval"}),
    _tpl("inbox_zero", "Inbox Triage Agent", "inbox assistant", "Productivity",
         "Email triage, prioritization, and reply drafts.",
         "Triage the inbox, prioritize by urgency, and draft replies. Never send without approval.",
         ["gmail.search", "gmail.draft", "task.create"], ["gmail"], ["personal"],
         {"external_replies": "require_approval"}),

    # --- Support & customer ---
    _tpl("support", "Support Triage", "support specialist", "Support",
         "Customer issue triage, policy answers, and escalation routing.",
         "Triage incoming support issues, answer from policy, and route escalations. External replies require approval.",
         ["connector.search", "artifact.write", "task.create"], ["slack", "teams", "jira", "zendesk"], ["workspace", "project"],
         {"external_replies": "require_approval"}),
    _tpl("customer_success", "Customer Success Agent", "customer success manager", "Support",
         "Account health checks, QBR prep, and renewal nudges.",
         "Monitor account health, prepare QBR materials, and draft renewal outreach for approval.",
         ["connector.search", "artifact.write", "gmail.draft"], ["hubspot", "zendesk", "gmail"], ["workspace", "project"],
         {"external_replies": "require_approval"}),
    _tpl("kb_writer", "Knowledge Base Writer", "documentation specialist", "Support",
         "Help-center articles and internal docs from resolved issues.",
         "Turn resolved issues and product knowledge into clear help-center articles and internal docs.",
         ["connector.search", "artifact.write"], ["notion", "zendesk", "google_drive"], ["workspace", "org"]),

    # --- Content & creative ---
    _tpl("technical_writer", "Technical Writer", "technical writer", "Content",
         "API docs, guides, and changelogs from source material.",
         "Produce accurate technical documentation, guides, and changelogs grounded in the provided source material.",
         ["repo.read", "artifact.write", "web.search"], ["github", "notion", "google_drive"], ["project", "workspace"]),
    _tpl("copywriter", "Copywriter", "copywriter", "Content",
         "Short-form copy: ads, headlines, and product descriptions.",
         "Write concise, persuasive copy for the requested format. Offer a few variations to choose from.",
         ["web.search", "artifact.write"], ["google_drive", "notion"], ["workspace"]),
    _tpl("presentation_builder", "Presentation Builder", "presentation designer", "Content",
         "Slide outlines and decks from briefs or data.",
         "Turn the brief or data into a clear slide outline and deck-ready content with speaker notes.",
         ["artifact.write", "data.run"], ["google_drive", "notion"], ["project", "workspace"]),

    # --- Finance, legal, HR ---
    _tpl("finance_analyst", "Finance Analyst", "finance analyst", "Finance",
         "Financial modeling, variance analysis, and reporting.",
         "Build financial models and variance analyses, then summarize results. Any transfer or payment requires approval.",
         ["data.run", "code.python", "artifact.write"], ["stripe", "google_drive", "airtable"], ["workspace", "org"],
         {"finance_writes": "require_approval", "external_replies": "require_approval"}),
    _tpl("bookkeeper", "Bookkeeping Agent", "bookkeeper", "Finance",
         "Transaction categorization and reconciliation drafts.",
         "Categorize transactions and prepare reconciliation drafts. Flag anomalies; do not post entries without approval.",
         ["data.run", "artifact.write"], ["stripe", "google_drive"], ["workspace"],
         {"finance_writes": "require_approval"}),
    _tpl("legal_review", "Legal Review Agent", "legal analyst", "Legal",
         "Contract review, clause flags, and plain-language summaries.",
         "Review the document, flag risky or non-standard clauses, and provide a plain-language summary. This is not legal advice; recommend human counsel.",
         ["connector.search", "artifact.write", "web.search"], ["google_drive", "notion"], ["project", "workspace"],
         {"external_replies": "require_approval"}),
    _tpl("recruiter", "Recruiting Agent", "technical recruiter", "HR",
         "Candidate sourcing, screening, and outreach drafts.",
         "Source and screen candidates against the role, then draft outreach for approval before sending.",
         ["web.search", "connector.search", "gmail.draft"], ["linkedin", "gmail"], ["workspace", "project"],
         {"external_replies": "require_approval"}),
    _tpl("hr_onboarding", "HR Onboarding Agent", "people operations specialist", "HR",
         "Onboarding checklists, doc prep, and reminders.",
         "Build onboarding checklists, prepare documents, and schedule reminders for new hires.",
         ["task.create", "artifact.write", "calendar.read"], ["google_calendar", "slack", "notion"], ["workspace", "org"]),
)

PUBLISH_TARGETS = {"slack", "teams", "email", "web", "api"}
PROFILE_KINDS = {"assistant", "agent"}
AUTONOMY_LEVELS = {"manual", "supervised", "approval_required", "autonomous"}


def templates() -> list[dict[str, Any]]:
    return [dict(template) for template in AGENT_TEMPLATES]


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise HTTPException(status_code=422, detail="Expected a list")
    return value


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail="Expected an object")
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key, value in list(data.items()):
        if hasattr(value, "isoformat"):
            data[key] = value.isoformat()
        else:
            data[key] = str(value) if key.endswith("_id") and value is not None else value
    if data.get("id") is not None:
        data["id"] = str(data["id"])
    if data.get("agent_profile_id") is not None:
        data["agent_profile_id"] = str(data["agent_profile_id"])
    return data


async def _require_project_access(member: Member, project_ids: list[str]) -> None:
    if not project_ids:
        return
    projects = await reflect_table("projects")
    project_members = await reflect_table("project_members")
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(project_members.c.project_id)
                .join(projects, projects.c.id == project_members.c.project_id)
                .where(
                    project_members.c.member_id == member.id,
                    project_members.c.organization_id == member.organization_id,
                    projects.c.organization_id == member.organization_id,
                    project_members.c.project_id.in_(project_ids),
                )
            )
        ).all()
    allowed = {str(row[0]) for row in rows}
    missing = set(project_ids) - allowed
    if missing:
        raise HTTPException(status_code=404, detail="Project not found")
    for project_id in project_ids:
        await permissions.check(member, "view_project", project_id)


async def _event(
    *,
    agent_id: str,
    organization_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    publication_id: str | None = None,
    task_id: str | None = None,
) -> None:
    events = await reflect_table("agent_profile_events")
    async with engine.begin() as conn:
        values: dict[str, Any] = {
            "organization_id": organization_id,
            "region": settings.region,
            "agent_profile_id": agent_id,
            "event_type": event_type,
            "payload": payload or {},
        }
        if publication_id is not None:
            values["publication_id"] = publication_id
        if task_id is not None:
            values["task_id"] = task_id
        await conn.execute(insert(events).values(**values))


async def create_profile(member: Member, data: dict[str, Any]) -> dict[str, Any]:
    await permissions.check(member, "create_agent", settings.org_id)
    project_ids = [str(pid) for pid in _json_list(data.get("project_ids"))]
    await _require_project_access(member, project_ids)

    autonomy_level = str(data.get("autonomy_level") or "supervised")
    if autonomy_level not in AUTONOMY_LEVELS:
        raise HTTPException(status_code=422, detail="Invalid autonomy level")
    profile_kind = str(data.get("profile_kind") or "agent").lower()
    if profile_kind not in PROFILE_KINDS:
        raise HTTPException(status_code=422, detail="Invalid profile kind")

    profiles = await reflect_table("agent_profiles")
    values = {
        "organization_id": member.organization_id,
        "region": settings.region,
        "profile_kind": profile_kind,
        "name": str(data["name"]).strip(),
        "role": str(data["role"]).strip(),
        "template_id": data.get("template_id"),
        "instructions": str(data.get("instructions") or "").strip(),
        "personality": str(data.get("personality") or "").strip(),
        "model": data.get("model"),
        "tool_grants": _json_list(data.get("tool_grants")),
        "connector_grants": _json_list(data.get("connector_grants")),
        "workflows": _json_list(data.get("workflows")),
        "connected_accounts": _json_list(data.get("connected_accounts")),
        "project_ids": project_ids,
        "memory_scopes": _json_list(data.get("memory_scopes")),
        "autonomy_level": autonomy_level,
        "approval_policy": _json_dict(data.get("approval_policy")),
        "schedule_permissions": _json_dict(data.get("schedule_permissions")),
        "status": data.get("status") or "active",
        "created_by": member.id,
    }
    if not values["name"] or not values["role"]:
        raise HTTPException(status_code=422, detail="Profile name and role are required")

    async with engine.begin() as conn:
        row = (
            await conn.execute(insert(profiles).values(**values).returning(profiles))
        ).mappings().first()
    agent = _row_dict(row)
    await _event(agent_id=agent["id"], organization_id=member.organization_id, event_type=f"{profile_kind}_created", payload={"name": agent["name"]})
    await audit.log(
        f"{profile_kind}_created",
        member.id,
        "agents.create",
        organization_id=member.organization_id,
        resource_type="agent_profile",
        resource_id=agent["id"],
        payload={"profile_kind": profile_kind, "template_id": agent.get("template_id"), "tool_grants": agent.get("tool_grants", [])},
    )
    return agent


async def list_profiles(member: Member, profile_kind: str | None = None) -> list[dict[str, Any]]:
    await permissions.check(member, "list_agents", settings.org_id)
    profiles = await reflect_table("agent_profiles")
    filters = [profiles.c.organization_id == member.organization_id, profiles.c.status != "deleted"]
    if profile_kind:
        kind = profile_kind.lower()
        if kind not in PROFILE_KINDS:
            raise HTTPException(status_code=422, detail="Invalid profile kind")
        filters.append(profiles.c.profile_kind == kind)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(profiles)
                .where(*filters)
                .order_by(profiles.c.created_at.desc())
            )
        ).mappings().all()
    return [_row_dict(row) for row in rows]


async def get_profile(member: Member, agent_id: str) -> dict[str, Any]:
    await permissions.check(member, "view_agent", agent_id)
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                select(profiles).where(
                    profiles.c.id == agent_id,
                    profiles.c.organization_id == member.organization_id,
                    profiles.c.status != "deleted",
                )
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _row_dict(row)


async def patch_profile(member: Member, agent_id: str, data: dict[str, Any]) -> dict[str, Any]:
    await get_profile(member, agent_id)
    patch = {key: value for key, value in data.items() if value is not None}
    if "profile_kind" in patch:
        patch["profile_kind"] = str(patch["profile_kind"]).lower()
        if patch["profile_kind"] not in PROFILE_KINDS:
            raise HTTPException(status_code=422, detail="Invalid profile kind")
    if "project_ids" in patch:
        patch["project_ids"] = [str(pid) for pid in _json_list(patch["project_ids"])]
        await _require_project_access(member, patch["project_ids"])
    for list_key in ("tool_grants", "connector_grants", "memory_scopes", "workflows", "connected_accounts"):
        if list_key in patch:
            patch[list_key] = _json_list(patch[list_key])
    if "autonomy_level" in patch and patch["autonomy_level"] not in AUTONOMY_LEVELS:
        raise HTTPException(status_code=422, detail="Invalid autonomy level")
    if not patch:
        return await get_profile(member, agent_id)
    patch["updated_at"] = func.now()
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                update(profiles)
                .where(profiles.c.id == agent_id, profiles.c.organization_id == member.organization_id)
                .values(**patch)
                .returning(profiles)
            )
        ).mappings().first()
    agent = _row_dict(row)
    await _event(agent_id=agent_id, organization_id=member.organization_id, event_type="agent_updated", payload={"fields": list(data.keys())})
    await audit.log("agent_updated", member.id, "agents.patch", organization_id=member.organization_id, resource_type="agent_profile", resource_id=agent_id, payload={"fields": list(data.keys())})
    return agent


async def delete_profile(member: Member, agent_id: str) -> dict[str, Any]:
    await get_profile(member, agent_id)
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        await conn.execute(
            update(profiles)
            .where(profiles.c.id == agent_id, profiles.c.organization_id == member.organization_id)
            .values(status="deleted")
        )
    await _event(agent_id=agent_id, organization_id=member.organization_id, event_type="agent_deleted")
    await audit.log("agent_deleted", member.id, "agents.delete", organization_id=member.organization_id, resource_type="agent_profile", resource_id=agent_id)
    return {"deleted": True, "agent_id": agent_id}


async def publish_agent(member: Member, agent_id: str, data: dict[str, Any]) -> dict[str, Any]:
    agent = await get_profile(member, agent_id)
    await permissions.check(member, "publish_agent", agent_id)
    target = str(data.get("target") or "").lower()
    if target not in PUBLISH_TARGETS:
        raise HTTPException(status_code=422, detail="Unsupported publish target")
    publications = await reflect_table("agent_publications")
    token = secrets.token_urlsafe(32)
    values = {
        "organization_id": member.organization_id,
        "region": settings.region,
        "agent_profile_id": agent_id,
        "target": target,
        "display_name": data.get("display_name") or agent["name"],
        "external_channel_id": data.get("external_channel_id"),
        "config": _json_dict(data.get("config")),
        "approval_policy": agent.get("approval_policy") or {},
        "inbound_token": token,
        "status": "active",
        "created_by": member.id,
    }
    async with engine.begin() as conn:
        row = (
            await conn.execute(insert(publications).values(**values).returning(publications))
        ).mappings().first()
    publication = _row_dict(row)
    await _event(
        agent_id=agent_id,
        organization_id=member.organization_id,
        publication_id=publication["id"],
        event_type="agent_published",
        payload={"target": target, "external_channel_id": values["external_channel_id"]},
    )
    await audit.log("agent_published", member.id, "agents.publish", organization_id=member.organization_id, resource_type="agent_publication", resource_id=publication["id"], payload={"agent_id": agent_id, "target": target})
    return publication


async def get_publication(publication_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    publications = await reflect_table("agent_publications")
    profiles = await reflect_table("agent_profiles")
    async with engine.begin() as conn:
        publication = (
            await conn.execute(
                select(publications).where(publications.c.id == publication_id, publications.c.status == "active")
            )
        ).mappings().first()
        if publication is None:
            raise HTTPException(status_code=404, detail="Publication not found")
        agent = (
            await conn.execute(
                select(profiles).where(
                    profiles.c.id == publication["agent_profile_id"],
                    profiles.c.organization_id == publication["organization_id"],
                    profiles.c.status == "active",
                )
            )
        ).mappings().first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _row_dict(publication), _row_dict(agent)
