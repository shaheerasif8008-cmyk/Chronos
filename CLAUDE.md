# CLAUDE.md — Chronos by Cognisia

This file is the primary context source for all AI coding assistants (Codex, Claude Code, Cursor) working on this project. Read it fully before writing any code.

---

## What This Project Is

**Chronos** is an autonomous AI agent platform for enterprises. It is a Manus-class task executor combined with Claude-style persistent organizational memory and enterprise governance (approvals, audit, permissions). One Chronos instance per organization learns the org through use, accumulates institutional memory, and becomes harder to replace over time.

**Company:** Cognisia  
**Product:** Chronos (working name)  
**Domain:** cognisiatech.com  
**Per-tenant subdomains:** `novatech.cognisiatech.com`

**Reference documents (always attach to sessions):**
- `chronos_architecture_v2.md` — product decisions and system design
- `chronos_build_plan.md` — sprint-by-sprint build order with exit criteria
- `chronos_technical_architecture.md` — code patterns, request flows, schemas

---

## Current Build Phase

**Phase 1, Sprint 1 — Weeks 1-2: Skeleton + Seams**

Build in this exact order:
1. Monorepo structure (Next.js + FastAPI)
2. Docker Compose (Postgres+pgvector, Redis, MinIO)
3. Database migrations (organizations, members, conversations, messages, audit_log)
4. Email OTP auth + JWT sessions
5. litellm model layer with streaming SSE
6. Conversation persistence
7. **The three seam functions** (permission.check, memory.retrieve, tool_broker.execute)
8. Context folder loader (reads `/context/{org_id}/*.md`)
9. Chat UI (Next.js, streaming, conversation history)
10. Seed script

Do not add any scope beyond this list. Do not start Sprint 2 until all Sprint 1 exit criteria pass.

---

## The Three Critical Seams

**These three functions must exist before any other feature is built. They are the most important code in the entire project. Every tool call, every memory read, every action check routes through them — no exceptions, ever.**

### 1. Permission Seam

```python
# apps/api/core/permissions.py
async def check(actor: Member, action: str, resource: str) -> bool:
    """
    Phase 1: returns True, logs to audit_log.
    Phase 3: queries OpenFGA with same signature.
    200+ call sites. The signature NEVER changes.
    """
    await audit.log("permission_check", actor.id, action,
                    resource_type="generic", resource_id=resource,
                    decision="granted_stub")
    return True
```

### 2. Memory Seam

```python
# apps/api/core/memory.py
async def retrieve(query: str, requester_context: RequesterContext) -> list[MemoryEntry]:
    """
    Phase 1: unfiltered vector search. requester_context is ignored.
    Phase 3: filters by authorized_scopes. Same signature.
    50+ call sites. The signature NEVER changes.
    """
    query_embedding = await embed(query)
    return await db.vector_search(
        table="memory_entries",
        vector=query_embedding,
        filters={"is_deleted": False, "organization_id": requester_context.org_id},
        limit=10
    )
    # Phase 3 replacement (drop-in, no call site changes):
    # authorized = compute_authorized_scopes(requester_context)
    # filters["scope_pairs"] = authorized
```

### 3. Broker Seam

```python
# apps/api/core/tool_broker.py
async def execute(agent: AgentContext, tool: str, args: dict) -> ToolResult:
    """
    Phase 1: passes through with audit logging.
    Phase 3+: adds rate limiting, approval gating, safety limits.
    Every tool call routes here. No direct connector calls. Ever.
    """
    await audit.log("tool_call", agent.id, tool, payload={"args": args})
    connector = await connector_registry.get(agent, tool)
    credentials = await credential_vault.get(connector.vault_ref)
    result = await connector.execute(tool, args, credentials)
    await audit.log("tool_result", agent.id, tool, payload={"summary": result.summary})
    return result
```

---

## Critical Rules — Never Violate These

```
RULE 1: Every tool call goes through tool_broker.execute(). Never call a connector directly.
RULE 2: Every memory read goes through memory.retrieve(). Never query memory_entries directly.
RULE 3: Every action check goes through permission.check(). Never inline permission logic.
RULE 4: Every table has organization_id UUID NOT NULL DEFAULT 'default'. No exceptions.
RULE 5: Every table has region TEXT NOT NULL DEFAULT 'us'. No exceptions.
RULE 6: audit_log is INSERT-only. REVOKE UPDATE, DELETE ON audit_log FROM app_user.
RULE 7: Credentials NEVER appear in logs. Only vault_ref is logged.
RULE 8: gmail.send always creates a draft first. Sending requires an approval record.
RULE 9: organization_id is always set from config constant ORG_ID = "default" in Phase 1.
RULE 10: Do not build sub-agent UI before the task engine runs real tasks end-to-end.
```

---

## Tech Stack

```
Backend:       Python 3.11+, FastAPI (async), Pydantic v2, SQLAlchemy Core, Alembic
Model layer:   litellm (local LLM primary, BYOK API key fallback)
Database:      Postgres 15 + pgvector extension
Cache/pubsub:  Redis
File storage:  MinIO (S3-compatible, local in dev)
Frontend:      Next.js 14 (App Router), TypeScript, Tailwind
Auth:          Email OTP + JWT (Phase 1). WorkOS/Clerk in Phase 3.
Connectors:    MCP (primary interface). Composio for Gmail/HubSpot adapters.
Browser:       Playwright (sandboxed subprocess)
Observability: Langfuse (LLM calls), Sentry (errors)
Scheduling:    APScheduler (in-process, not Celery)
Permissions:   Stub returning True (Phase 1). OpenFGA in Phase 3.
```

---

## Repository Structure

```
chronos/
├── apps/
│   ├── web/                          # Next.js frontend
│   │   ├── app/
│   │   │   ├── (auth)/login/
│   │   │   └── (app)/
│   │   │       ├── chat/
│   │   │       ├── approvals/
│   │   │       └── settings/
│   │   │           ├── memory/
│   │   │           ├── connectors/
│   │   │           └── personas/
│   │   └── components/
│   │       ├── chat/
│   │       │   ├── MessageStream.tsx
│   │       │   ├── ActivityLog.tsx
│   │       │   ├── SubAgentCard.tsx
│   │       │   └── TakeoverFrame.tsx
│   │       ├── memory/MemoryEditor.tsx
│   │       └── approvals/ApprovalInbox.tsx
│   │
│   └── api/                          # FastAPI backend
│       ├── main.py
│       ├── routers/
│       │   ├── auth.py
│       │   ├── chat.py
│       │   ├── tasks.py
│       │   ├── memory.py
│       │   ├── approvals.py
│       │   ├── connectors.py
│       │   └── admin.py
│       ├── core/                     # THE THREE SEAMS LIVE HERE
│       │   ├── permissions.py        # permission.check()
│       │   ├── memory.py             # memory.retrieve()
│       │   ├── tool_broker.py        # tool_broker.execute()
│       │   ├── context.py            # assemble_context()
│       │   └── embeddings.py
│       ├── runtime/
│       │   ├── planner.py
│       │   ├── executor.py
│       │   └── sub_agent.py
│       ├── memory/
│       │   ├── retrieval.py
│       │   ├── extraction.py
│       │   └── synthesis.py
│       ├── connectors/
│       │   ├── registry.py
│       │   ├── vault.py
│       │   ├── gmail.py
│       │   └── browser.py
│       ├── skills/
│       │   ├── loader.py
│       │   └── registry.py
│       ├── models/
│       │   ├── organization.py
│       │   ├── member.py
│       │   ├── conversation.py
│       │   ├── message.py
│       │   ├── memory_entry.py
│       │   ├── task.py
│       │   ├── approval.py
│       │   └── connector.py
│       ├── migrations/
│       └── jobs/
│           ├── memory_extraction.py
│           ├── profile_synthesis.py
│           └── context_update.py
│
├── skills/
│   ├── general/
│   │   ├── SKILL.md
│   │   └── metadata.json
│   └── sdr-outreach/
│       ├── SKILL.md
│       ├── icp-qualification.md
│       ├── templates/
│       └── metadata.json
│
├── context/
│   └── default/
│       └── org.md
│
├── docker-compose.yml
└── .env.example
```

---

## Database Schema

All tables include `organization_id` and `region`. This is non-negotiable.

```sql
-- organizations
CREATE TABLE organizations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    region          TEXT NOT NULL DEFAULT 'us',
    plan            TEXT NOT NULL DEFAULT 'trial',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- members
CREATE TABLE members (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    email           TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user',
    name            TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(organization_id, email)
);

-- personas (saved Chronos configurations)
CREATE TABLE personas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    name            TEXT NOT NULL,
    prompt          TEXT,
    skill_pack_ids  TEXT[] DEFAULT '{}',
    connector_ids   UUID[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- workspaces (context scopes, not AI instances)
CREATE TABLE workspaces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    name            TEXT NOT NULL,
    persona_id      UUID REFERENCES personas(id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- conversations
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    workspace_id    UUID REFERENCES workspaces(id),
    persona_id      UUID REFERENCES personas(id),
    member_id       UUID NOT NULL,
    title           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- messages
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    artifact_ids    UUID[] DEFAULT '{}',
    token_count     INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- memory_entries (one table, six tiers via scope column)
CREATE TABLE memory_entries (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id         UUID NOT NULL DEFAULT 'default',
    scope                   TEXT NOT NULL,
    scope_id                TEXT NOT NULL,
    content                 TEXT NOT NULL,
    embedding               VECTOR(1536),
    source                  TEXT NOT NULL,
    source_conversation_id  UUID,
    importance_score        FLOAT DEFAULT 0.5,
    is_deleted              BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    created_by              TEXT
);
CREATE INDEX ON memory_entries USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX ON memory_entries (organization_id, scope, scope_id);

-- tasks
CREATE TABLE tasks (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id         UUID NOT NULL DEFAULT 'default',
    parent_task_id          UUID REFERENCES tasks(id),
    persona_id              UUID,
    workspace_id            UUID,
    triggered_by            TEXT NOT NULL,
    triggered_by_member_id  UUID,
    status                  TEXT DEFAULT 'pending',
    goal                    TEXT NOT NULL,
    plan                    JSONB,
    current_step            INT DEFAULT 0,
    result                  JSONB,
    error                   TEXT,
    depth                   INT DEFAULT 0,
    token_count             INT DEFAULT 0,
    cost_estimate           FLOAT DEFAULT 0,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ
);

-- approvals
CREATE TABLE approvals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    task_id         UUID NOT NULL REFERENCES tasks(id),
    step_id         TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    action_payload  JSONB NOT NULL,
    requested_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    status          TEXT DEFAULT 'pending',
    decided_by      UUID REFERENCES members(id),
    decided_at      TIMESTAMPTZ,
    decision_note   TEXT
);

-- connectors
CREATE TABLE connectors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    persona_id      UUID,
    provider        TEXT NOT NULL,
    account_handle  TEXT,
    vault_ref       TEXT NOT NULL,
    status          TEXT DEFAULT 'active',
    scopes          TEXT[] DEFAULT '{}',
    connected_at    TIMESTAMPTZ DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ
);

-- audit_log (append-only, forever)
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    event_type      TEXT NOT NULL,
    actor_id        TEXT,
    action          TEXT NOT NULL,
    resource_type   TEXT,
    resource_id     TEXT,
    payload         JSONB,
    decision        TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX ON audit_log (organization_id, created_at DESC);
-- Apply in migration:
-- REVOKE UPDATE, DELETE ON audit_log FROM app_user;
```

---

## Core Data Model

```python
# apps/api/core/models.py
class RequesterContext(BaseModel):
    org_id: str = "default"          # Phase 1: always "default"
    member_id: str
    workspace_id: str | None = None
    persona_id: str | None = None
    task_id: str | None = None
    role: str = "user"

    @classmethod
    def from_member(cls, member: Member) -> "RequesterContext":
        return cls(
            org_id=member.organization_id,
            member_id=str(member.id),
            role=member.role
        )
```

---

## Context Assembly (assemble_context)

Every LLM call goes through this function. Never bypass it.

```python
# apps/api/core/context.py
async def assemble_context(
    conversation_id: str,
    message: str,
    requester_context: RequesterContext
) -> list[dict]:

    base = load_base_system_prompt()

    # Layer 1: Org context folder (all .md files)
    org_files = await minio.list_files(f"context/{requester_context.org_id}/")
    for f in org_files:
        content = await minio.read(f.path)
        base += f"\n\n## {f.name}\n{content}"

    # Layer 2: Active persona
    if requester_context.persona_id:
        persona = await db.get_persona(requester_context.persona_id)
        if persona.prompt:
            base += f"\n\n# Your Identity\n{persona.prompt}"

    # Layer 3: Relevant skills (lazy-loaded)
    skills = await skill_registry.find_relevant(message, requester_context.org_id)
    for skill in skills:
        skill_content = await skill_loader.load_full(skill.id)
        base += f"\n\n# Skill: {skill.name}\n{skill_content}"

    # Layer 4: Memory — THE SEAM
    memories = await memory.retrieve(message, requester_context)
    if memories:
        base += "\n\n# What I Remember\n"
        base += "\n".join([f"- {m.content}" for m in memories])

    # Layer 5: Current task (if mid-task)
    if requester_context.task_id:
        task = await db.get_task(requester_context.task_id)
        base += f"\n\n# Current Task\nGoal: {task.goal}\nStep {task.current_step}/{len(task.plan['steps'])}"

    # Layer 6: Conversation history
    history = await db.get_messages(conversation_id, limit=20)

    return [
        {"role": "system", "content": base},
        *[{"role": m.role, "content": m.content} for m in history],
        {"role": "user", "content": message}
    ]
```

---

## Streaming Pattern

Two simultaneous SSE streams when Chronos works:

```python
# Stream 1: Chat tokens (/chat/message)
async def stream():
    async for chunk in litellm.acompletion(model=..., messages=context, stream=True):
        token = chunk.choices[0].delta.content or ""
        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"

return StreamingResponse(stream(), media_type="text/event-stream")

# Stream 2: Activity log (/tasks/{id}/stream)
# Executor publishes to Redis → FastAPI subscribes → SSE to browser
async def event_stream():
    task = await db.get_task(task_id)
    yield f"data: {json.dumps({'type': 'task_state', 'task': task.to_dict()})}\n\n"
    async with redis.subscribe(f"activity:{task_id}") as channel:
        async for message in channel:
            yield f"data: {message}\n\n"

return StreamingResponse(event_stream(), media_type="text/event-stream")
```

---

## Task Executor Pattern

```python
# apps/api/runtime/executor.py
class TaskExecutor:
    async def run(self, task: Task):
        await db.update_task(task.id, status="running")

        for i, step in enumerate(task.plan["steps"]):
            if i < task.current_step:
                continue  # resume after restart

            await self._emit(task.id, {"type": "step_start", "step": step})

            try:
                result = await self._execute_step(task, step)
                await db.update_task(task.id, current_step=i+1)
                await self._emit(task.id, {"type": "step_done", "result": result})

            except ApprovalRequired as e:
                approval = await self._create_approval(task, step, e.payload)
                await self._emit(task.id, {"type": "awaiting_approval", "approval_id": approval.id})
                await self._wait_for_approval(approval.id)

            except Exception as e:
                if await self._should_retry(step, e):
                    continue
                await self._escalate(task, step, e)
                return

        await db.update_task(task.id, status="complete")
        await self._emit(task.id, {"type": "task_complete"})

    async def _execute_step(self, task, step):
        if step["action"] == "spawn_sub_agent":
            return await sub_agent_manager.spawn_and_wait(task, step["goal"])
        elif step["action"] == "tool_call":
            return await tool_broker.execute(  # ALWAYS through broker
                agent=AgentContext.from_task(task),
                tool=step["tool"],
                args=step["args"]
            )
        elif step["action"] == "think":
            return await self._llm_step(task, step)

    async def _emit(self, task_id: str, event: dict):
        await redis.publish(f"activity:{task_id}", json.dumps(event))
        await audit.log("task_step", task_id, event)
```

---

## Memory Extraction Pattern

Runs as background task after every assistant response.

```python
# apps/api/memory/extraction.py
async def extract_and_save(
    conversation_id: str,
    user_message: str,
    assistant_response: str,
    requester_context: RequesterContext
) -> None:
    prompt = f"""
    Identify facts worth remembering from this exchange.
    Return JSON: {{"memories": [{{"content": "...", "scope": "personal|workspace|persona|org", "importance": 0.0-1.0}}]}}
    Only include durable facts. Not conversational filler. If nothing worth saving, return {{"memories": []}}.

    User: {user_message}
    Assistant: {assistant_response}
    """

    result = await litellm.acompletion(
        model=get_fast_model(),
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )

    candidates = json.loads(result.choices[0].message.content).get("memories", [])

    for c in candidates:
        if c["importance"] < 0.6:
            continue

        embedding = await embed(c["content"])
        entry_id = await db.insert("memory_entries", {
            "organization_id": requester_context.org_id,
            "scope": c["scope"],
            "scope_id": resolve_scope_id(c["scope"], requester_context),
            "content": c["content"],
            "embedding": embedding,
            "source": "autonomous",
            "source_conversation_id": conversation_id,
            "importance_score": c["importance"],
            "created_by": "chronos"
        })

        # Notify frontend: show inline "Memory saved — Undo?" for 60 seconds
        await redis.publish(f"memories:{conversation_id}", json.dumps({
            "type": "memory_saved",
            "entry_id": str(entry_id),
            "content": c["content"],
            "scope": c["scope"],
            "undo_expires": (datetime.now() + timedelta(seconds=60)).isoformat()
        }))
```

---

## Sub-Agent Pattern

```python
# apps/api/runtime/sub_agent.py
class SubAgentManager:
    async def spawn_and_wait(self, parent_task: Task, goal: str) -> dict:
        if parent_task.depth >= 3:
            if not await self._has_spawn_permission(parent_task):
                raise DepthLimitExceeded("Max sub-agent depth (3) reached")

        sub_task = await db.insert("tasks", {
            "organization_id": parent_task.organization_id,
            "parent_task_id": parent_task.id,
            "goal": goal,
            "depth": parent_task.depth + 1,
            "status": "pending",
            "triggered_by": f"task:{parent_task.id}"
        })

        executor = TaskExecutor()
        asyncio.create_task(executor.run(sub_task))

        await redis.publish(f"activity:{parent_task.id}", json.dumps({
            "type": "sub_agent_spawned",
            "sub_task_id": str(sub_task.id),
            "goal": goal
        }))

        return await self._wait_for_completion(sub_task.id, parent_task.id)

    async def _wait_for_completion(self, sub_task_id, parent_task_id):
        async with redis.subscribe(f"activity:{sub_task_id}") as channel:
            async for message in channel:
                event = json.loads(message)
                # Forward to parent activity log (nested)
                await redis.publish(f"activity:{parent_task_id}", json.dumps({
                    "type": "sub_agent_event",
                    "sub_task_id": sub_task_id,
                    "event": event
                }))
                if event["type"] == "task_complete":
                    await self._save_sub_agent_profile(sub_task_id)
                    return event.get("result", {})
                if event["type"] == "task_failed":
                    raise SubAgentFailed(event.get("error"))
```

---

## Skill System Pattern

```python
# Skill folder structure
# skills/{skill_id}/
#   SKILL.md          — instructions, rules, output format
#   metadata.json     — name, description, requires_connectors, spawns_sub_agent
#   (optional files)  — templates, scripts, reference docs

# metadata.json format:
{
    "id": "sdr-outreach",
    "name": "SDR Outreach",
    "description": "Researches leads, qualifies ICP fit, drafts personalized outreach emails.",
    "requires_connectors": ["gmail", "browser"],
    "spawns_sub_agent": true
}

# Registry: loads only names + descriptions at startup
# Loader: lazy-loads full SKILL.md + all files when relevant
# Relevance: cheap LLM call with skill names/descriptions vs user message
# Multiple skills can activate for one task
```

---

## Connector Pattern

```python
# Never call connectors directly.
# Always: await tool_broker.execute(agent, "gmail.send", args)

# gmail.draft always, never gmail.send directly in Phase 1
# gmail.send only reachable after approval record exists in DB

# Credential vault: AES-256-GCM, stored in Redis (fast) + Postgres (backup)
# vault_ref is the only thing that appears in logs. Never the credential itself.
```

---

## Environment Variables (.env)

```bash
# Phase 1 constants
ORG_ID=default
REGION=us

# Database
DATABASE_URL=postgresql+asyncpg://chronos:chronos@localhost:5432/chronos

# Redis
REDIS_URL=redis://localhost:6379

# MinIO
MINIO_ENDPOINT=localhost:9000
MINIO_ACCESS_KEY=chronos
MINIO_SECRET_KEY=chronos123

# Model layer
LOCAL_LLM_BASE_URL=http://localhost:11434  # Ollama or similar
LOCAL_LLM_MODEL=llama3
BACKUP_API_KEY=sk-...                      # Client's Anthropic/OpenAI key
FAST_MODEL=llama3                          # Cheap model for extraction/routing

# Vault encryption
VAULT_ENCRYPTION_KEY=<32-byte-hex-key>

# Observability
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
SENTRY_DSN=https://...

# Email (dev: OTP logged to console, no SendGrid needed)
SENDGRID_API_KEY=optional-in-dev
```

---

## Local Dev Setup

```bash
# Clone and configure
git clone https://github.com/cognisia/chronos
cd chronos
cp .env.example .env

# Start infrastructure
docker-compose up -d  # Postgres+pgvector, Redis, MinIO

# Backend
cd apps/api
pip install -r requirements.txt
alembic upgrade head
python seed.py        # creates default org + admin member
uvicorn main:app --reload --port 8000

# Frontend (new terminal)
cd apps/web
npm install
npm run dev           # runs on localhost:3000

# Login: open localhost:3000, enter the test email from seed.py
# OTP appears in the API console log in dev (no SendGrid needed)
```

---

## Seed Script Output

The seed script creates:
- One org: `id = 'default'`, `slug = 'default'`
- One member: the admin email from `.env`
- Context folder: `context/default/org.md` with a starter template
- Skill packs loaded into MinIO: `general/` and `sdr-outreach/`

---

## Docker Compose Services

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg15
    environment:
      POSTGRES_DB: chronos
      POSTGRES_USER: chronos
      POSTGRES_PASSWORD: chronos
    ports: ["5432:5432"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: chronos
      MINIO_ROOT_PASSWORD: chronos123
    ports: ["9000:9000", "9001:9001"]
```

---

## Safety Limits (enforce in ToolBroker)

```python
SAFETY_LIMITS = {
    "gmail.send": {"max_recipients": 10},
    "crm.delete_records": {"max_count": 5},
    "db.delete": {"max_count": 5},
    "finance.transfer": {"max_amount": 100},
    "payment.send": {"max_amount": 100},
    # These always raise ApprovalRequired regardless of autonomy:
    "twitter.post": "always_approval",
    "linkedin.post": "always_approval",
    "website.publish": "always_approval",
}

RATE_LIMITS = {
    "actions_per_minute": 10,      # per org
    "loop_detection_threshold": 10, # same tool+args = escalate
    "sub_agent_depth": 3,          # max nesting
    "concurrent_sub_agents": 5,    # per org
}
```

---

## Memory Scopes

```python
MEMORY_SCOPES = {
    "personal":    "private to one human member",
    "workspace":   "shared within one workspace",
    "persona":     "shared by all users of one persona",
    "department":  "shared within a department",
    "org":         "shared across the entire organization",
    "restricted":  "explicit ACL — specific members only",
}

# Phase 1: all writes default to scope="org" unless user specifies otherwise
# Phase 3: scope filter applied at retrieval based on authorized_scopes
```

---

## What Chronos Is NOT

- Not a chatbot wrapper. It runs multi-hour autonomous tasks.
- Not a one-shot agent. It accumulates memory and identity over time.
- Not a collection of separate AI employees. One engine, personas are saved configs.
- Not a factory pipeline. The Forge mistake was building infrastructure without a working product.

---

## What Phase 1 Proves

By the end of Sprint 4 (Week 8), one user should be able to:

1. Tell Chronos an ICP description for lead generation
2. Watch the activity log stream as Chronos plans and executes
3. Watch a sub-agent browse the web and extract leads
4. See a lead report with qualification scores
5. See 20 personalized draft emails in the approval inbox
6. Approve them in batch
7. See the drafts appear in the connected Gmail account

If this loop doesn't work end-to-end, Sprint 4 is not done.

---

## The Anti-Patterns (from the previous build, Forge-apr8)

```
Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

Tradeoff: These guidelines bias toward caution over speed. For trivial tasks, use judgment.

1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

State your assumptions explicitly. If uncertain, ask.
If multiple interpretations exist, present them - don't pick silently.
If a simpler approach exists, say so. Push back when warranted.
If something is unclear, stop. Name what's confusing. Ask.
2. Simplicity First
Minimum code that solves the problem. Nothing speculative.

No features beyond what was asked.
No abstractions for single-use code.
No "flexibility" or "configurability" that wasn't requested.
No error handling for impossible scenarios.
If you write 200 lines and it could be 50, rewrite it.
Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:

Don't "improve" adjacent code, comments, or formatting.
Don't refactor things that aren't broken.
Match existing style, even if you'd do it differently.
If you notice unrelated dead code, mention it - don't delete it.
When your changes create orphans:

Remove imports/variables/functions that YOUR changes made unused.
Don't remove pre-existing dead code unless asked.
The test: Every changed line should trace directly to the user's request.

4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

"Add validation" → "Write tests for invalid inputs, then make them pass"
"Fix the bug" → "Write a test that reproduces it, then make it pass"
"Refactor X" → "Ensure tests pass before and after"
For multi-step tasks, state a brief plan:

1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

These guidelines are working if: fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

*This file is the source of truth for all AI coding sessions.*
*Reference: chronos_architecture_v2.md · chronos_build_plan.md · chronos_technical_architecture.md*
*Version: 1.0*
