# Chronos — Technical Architecture

**What this document is:** the actual wiring. How every request flows, what every service does, what the real code patterns look like, and why each technical decision was made.

**Reference docs:** `chronos_architecture_v2.md` (what), `chronos_build_plan.md` (when)

---

## 1. The Stack, Explained

```
Next.js (App Router)       → Frontend. Streaming UI, server components, SSE client.
FastAPI (async Python)     → API. Every route is async. No sync blocking anywhere.
litellm                    → Model router. Local LLM primary, BYOK fallback.
Postgres 15 + pgvector     → Everything. Conversations, memory, tasks, audit. One DB.
Redis                      → Activity log streaming (pub/sub). Embedding cache.
MinIO                      → File storage. Context folders, skill packs, artifacts.
Composio                   → Connector adapters for consumer SaaS (Gmail, HubSpot).
Playwright                 → Browser automation. Runs in sandboxed subprocess.
APScheduler                → Background jobs. Memory extraction, profile synthesis.
Alembic                    → Database migrations.
SQLAlchemy Core            → Database queries. Not ORM — Core for control.
Pydantic v2                → Request/response validation everywhere.
Langfuse                   → AI observability. Every LLM call logged.
Sentry                     → Error tracking.
```

**Why SQLAlchemy Core, not ORM?**
The memory retrieval query does a vector similarity search with a dynamic scope filter computed at runtime. The approval query joins tasks, members, and approvals with conditional permission checks. ORM abstractions fight you on complex queries. Core gives you the query control you need without dropping to raw SQL strings.

**Why one Postgres database for everything?**
Phase 1: simplicity. One connection pool, one migration history, one backup target.
Phase 3: add row-level security policies — the database itself enforces tenant isolation. No application code change.
Phase 4: if a client's load demands it, move them to a dedicated database instance. The `organization_id` column is already on every row.

---

## 2. Repository Structure

```
chronos/
├── apps/
│   ├── web/                          # Next.js frontend
│   │   ├── app/
│   │   │   ├── (auth)/
│   │   │   │   └── login/
│   │   │   └── (app)/
│   │   │       ├── chat/             # Main chat interface
│   │   │       ├── approvals/        # Approval inbox
│   │   │       ├── settings/
│   │   │       │   ├── memory/       # Memory editor
│   │   │       │   ├── connectors/   # Connector status + OAuth
│   │   │       │   └── personas/     # Persona management
│   │   │       └── admin/            # Org admin panel
│   │   └── components/
│   │       ├── chat/
│   │       │   ├── MessageStream.tsx
│   │       │   ├── ActivityLog.tsx   # Manus-style real-time log
│   │       │   ├── SubAgentCard.tsx  # Nested agent view
│   │       │   └── TakeoverFrame.tsx # Browser takeover iframe
│   │       ├── memory/
│   │       │   └── MemoryEditor.tsx
│   │       └── approvals/
│   │           └── ApprovalInbox.tsx
│   │
│   └── api/                          # FastAPI backend
│       ├── main.py
│       ├── routers/
│       │   ├── auth.py
│       │   ├── chat.py               # Message + streaming endpoint
│       │   ├── tasks.py              # Task management
│       │   ├── memory.py             # Memory CRUD
│       │   ├── approvals.py          # Approval actions
│       │   ├── connectors.py         # OAuth flows, connector status
│       │   └── admin.py              # Org admin
│       ├── core/                     # THE THREE SEAMS LIVE HERE
│       │   ├── permissions.py        # permission.check() — stub → OpenFGA
│       │   ├── memory.py             # memory.retrieve() — stub → scoped
│       │   ├── tool_broker.py        # tool_broker.execute() — always real
│       │   ├── context.py            # Context assembly for every LLM call
│       │   └── embeddings.py         # Embed text → vector
│       ├── runtime/
│       │   ├── planner.py            # Goal → execution plan
│       │   ├── executor.py           # Step-by-step execution loop
│       │   └── sub_agent.py          # Sub-agent spawn + coordination
│       ├── memory/
│       │   ├── retrieval.py          # Vector search implementation
│       │   ├── extraction.py         # Post-response memory extraction
│       │   └── synthesis.py          # Daily synthesized profile job
│       ├── connectors/
│       │   ├── registry.py           # tool_name → connector lookup
│       │   ├── vault.py              # Credential encryption/decryption
│       │   ├── gmail.py              # Composio Gmail adapter
│       │   └── browser.py            # Playwright browser connector
│       ├── skills/
│       │   ├── loader.py             # SKILL.md lazy loader
│       │   └── registry.py           # Preloaded skill name+description index
│       ├── models/                   # SQLAlchemy table definitions
│       │   ├── organization.py
│       │   ├── member.py
│       │   ├── conversation.py
│       │   ├── message.py
│       │   ├── memory_entry.py
│       │   ├── task.py
│       │   ├── approval.py
│       │   └── connector.py
│       ├── migrations/               # Alembic migration files
│       └── jobs/                     # APScheduler job definitions
│           ├── memory_extraction.py
│           ├── profile_synthesis.py
│           └── context_update.py
│
├── skills/                           # Skill pack folder (loaded from MinIO in prod)
│   ├── general/
│   │   ├── SKILL.md
│   │   └── metadata.json
│   └── sdr-outreach/
│       ├── SKILL.md
│       ├── icp-qualification.md
│       └── metadata.json
│
├── docker-compose.yml
├── .env.example
└── packages/
    └── shared/
        └── types/                    # Shared TypeScript types
```

---

## 3. The Database Schema

Every table carries `organization_id` and is RLS-ready for Phase 3.

```sql
-- Core tenant table
CREATE TABLE organizations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        TEXT UNIQUE NOT NULL,           -- 'novatech' → novatech.cognisiatech.com
    name        TEXT NOT NULL,
    region      TEXT NOT NULL DEFAULT 'us',
    plan        TEXT NOT NULL DEFAULT 'trial',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Humans
CREATE TABLE members (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id),
    email           TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user', -- owner/admin/supervisor/user
    name            TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(organization_id, email)
);

-- Saved Chronos configurations (personas)
CREATE TABLE personas (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    name            TEXT NOT NULL,               -- 'Alex', 'Jordan', default 'Chronos'
    prompt          TEXT,                        -- personality/role prompt
    skill_pack_ids  TEXT[] DEFAULT '{}',
    connector_ids   UUID[] DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Context scopes
CREATE TABLE workspaces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL,
    name            TEXT NOT NULL,
    persona_id      UUID REFERENCES personas(id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Conversation threads
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

-- Individual messages
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    role            TEXT NOT NULL,               -- user/assistant/system/tool
    content         TEXT NOT NULL,
    artifact_ids    UUID[] DEFAULT '{}',
    token_count     INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- The memory store — one table, six tiers via scope column
CREATE TABLE memory_entries (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id         UUID NOT NULL DEFAULT 'default',
    scope                   TEXT NOT NULL,       -- personal/workspace/persona/dept/org/restricted
    scope_id                TEXT NOT NULL,       -- the relevant entity ID
    content                 TEXT NOT NULL,
    embedding               VECTOR(1536),
    source                  TEXT NOT NULL,       -- autonomous/explicit/synthesized
    source_conversation_id  UUID,
    source_message_id       UUID,
    importance_score        FLOAT DEFAULT 0.5,
    is_deleted              BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    created_by              TEXT                 -- member_id or 'chronos'
);
CREATE INDEX ON memory_entries USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX ON memory_entries (organization_id, scope, scope_id);

-- Tasks (multi-step execution units)
CREATE TABLE tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    parent_task_id  UUID REFERENCES tasks(id),   -- set for sub-agents
    persona_id      UUID,
    workspace_id    UUID,
    triggered_by    TEXT NOT NULL,               -- message_id/scheduled/webhook
    triggered_by_member_id UUID,
    status          TEXT DEFAULT 'pending',      -- pending/planning/running/awaiting_approval/complete/failed/cancelled
    goal            TEXT NOT NULL,
    plan            JSONB,                       -- [{id, action, description, tool, args, depends_on}]
    current_step    INT DEFAULT 0,
    result          JSONB,
    error           TEXT,
    depth           INT DEFAULT 0,               -- sub-agent nesting depth
    token_count     INT DEFAULT 0,
    cost_estimate   FLOAT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ
);

-- Approval gates
CREATE TABLE approvals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    task_id         UUID NOT NULL REFERENCES tasks(id),
    step_id         TEXT NOT NULL,               -- which plan step this gates
    action_type     TEXT NOT NULL,               -- send_email/delete_records/financial/publish
    action_payload  JSONB NOT NULL,              -- the exact content awaiting approval
    requested_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,                 -- 24h default
    status          TEXT DEFAULT 'pending',      -- pending/approved/rejected/expired
    decided_by      UUID REFERENCES members(id),
    decided_at      TIMESTAMPTZ,
    decision_note   TEXT
);

-- Connector registry
CREATE TABLE connectors (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    persona_id      UUID,                        -- NULL = org-level
    provider        TEXT NOT NULL,               -- gmail/browser/hubspot/slack/...
    account_handle  TEXT,                        -- alex@novatech.com
    vault_ref       TEXT NOT NULL,               -- pointer to credential in vault
    status          TEXT DEFAULT 'active',       -- active/expired/disconnected
    scopes          TEXT[] DEFAULT '{}',
    connected_at    TIMESTAMPTZ DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ
);

-- Append-only audit log. No UPDATE, no DELETE. Ever.
CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    event_type      TEXT NOT NULL,
    actor_id        TEXT,                        -- member_id or 'chronos' or task_id
    action          TEXT NOT NULL,
    resource_type   TEXT,
    resource_id     TEXT,
    payload         JSONB,
    decision        TEXT,                        -- granted/denied (for permission events)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
-- Enforce immutability at DB level (apply in migration)
-- REVOKE UPDATE, DELETE ON audit_log FROM app_user;
CREATE INDEX ON audit_log (organization_id, created_at DESC);
CREATE INDEX ON audit_log (organization_id, actor_id, created_at DESC);
```

---

## 4. The Three Critical Request Flows

### Flow 1: Simple Chat Message

```
User types → POST /chat/message → stream tokens back

Step 1: Auth middleware
  → Validate JWT
  → Set app.current_org_id in DB session (Phase 3: RLS uses this)
  → Load member from DB

Step 2: Context assembly (core/context.py)
  → Load org context folder (all .md files from MinIO/org_id/)
  → Load active persona prompt (if invoked by name)
  → Load relevant skills (scan skill descriptions vs message intent)
  → memory.retrieve(message, requester_context)  ← MEMORY SEAM
  → Load last 20 conversation messages
  → Assemble system prompt + history + user message

Step 3: permission.check()  ← PERMISSION SEAM
  → check(actor=member, action="chat", resource=workspace_id)
  → Phase 1: returns True
  → Phase 3: queries OpenFGA

Step 4: LLM call via litellm
  → Streaming response
  → Tokens streamed back to client via SSE

Step 5: Background tasks (after stream completes)
  → Save assistant message to DB
  → memory_extraction.run(conversation_id, user_message, response)
  → Emit audit event
```

```python
# apps/api/routers/chat.py
@router.post("/message")
async def send_message(req: ChatRequest, member: Member = Depends(get_current_member)):
    await permission.check(member, "chat", req.workspace_id)

    context = await assemble_context(
        conversation_id=req.conversation_id,
        message=req.message,
        requester_context=RequesterContext.from_member(member)
    )

    async def stream():
        full_response = ""
        async for chunk in litellm.acompletion(
            model=get_model_for_org(member.org_id),
            messages=context,
            stream=True
        ):
            token = chunk.choices[0].delta.content or ""
            full_response += token
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

        # Background: save + extract memories
        asyncio.create_task(post_message_jobs(
            req.conversation_id, req.message, full_response, member
        ))
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
```

### Flow 2: Multi-Step Task with Sub-Agent

```
User: "Find 20 leads and draft outreach"

Step 1: Chat message received (same as Flow 1)

Step 2: Chronos identifies this as a task (not a simple answer)
  → Calls /tasks/create internally
  → task = {goal: "Find 20 leads...", status: "planning", triggered_by: message_id}

Step 3: Planner generates the execution plan
  → LLM call: "Given this goal, generate a JSON execution plan"
  → Plan saved to tasks.plan

Step 4: Executor starts (asyncio.create_task — non-blocking)
  → Chronos responds to user immediately: "On it. I'll research leads now."
  → Executor runs in background

Step 5: Executor loop
  For each step in plan:
    → permission.check(actor, step.action, workspace)
    → If step needs sub-agent:
        sub_task = await sub_agent_manager.spawn(parent_task, sub_goal)
        await wait_for_sub_agent(sub_task)  # listens on Redis channel
    → Else:
        result = await tool_broker.execute(agent, step.tool, step.args)
    → Update tasks.current_step
    → Publish step result to Redis: PUBLISH activity:{task_id} {event}
    → If approval_gate step:
        create approval record
        pause execution (task status = awaiting_approval)
        notify member via in-app + email
        wait for approval signal on Redis: SUBSCRIBE approval:{approval_id}
        resume on approval

Step 6: Activity log streams to frontend
  → Frontend subscribes to GET /tasks/{task_id}/stream (SSE)
  → FastAPI subscribes to Redis channel activity:{task_id}
  → Every event published by executor arrives at the frontend in real time
```

```python
# apps/api/runtime/executor.py
class TaskExecutor:
    async def run(self, task: Task):
        await db.update_task(task.id, status="running", started_at=now())

        for i, step in enumerate(task.plan["steps"]):
            if i < task.current_step:
                continue  # Resume from where we left off after restart

            await self._emit(task.id, {"type": "step_start", "step": step})

            try:
                result = await self._execute_step(task, step)
                await db.update_task(task.id, current_step=i+1)
                await self._emit(task.id, {"type": "step_done", "step": step, "result": result})

            except ApprovalRequired as e:
                approval = await self._create_approval(task, step, e.payload)
                await self._emit(task.id, {"type": "awaiting_approval", "approval_id": approval.id})
                await self._wait_for_approval(approval.id)  # blocks until approved/rejected

            except Exception as e:
                if await self._should_retry(step, e):
                    continue
                await self._escalate(task, step, e)
                return

        await db.update_task(task.id, status="complete", completed_at=now())
        await self._emit(task.id, {"type": "task_complete"})

    async def _execute_step(self, task: Task, step: dict):
        if step["action"] == "spawn_sub_agent":
            return await sub_agent_manager.spawn_and_wait(task, step["goal"])
        elif step["action"] == "tool_call":
            return await tool_broker.execute(
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

### Flow 3: Approval Flow

```
Executor hits a step where action_type = "send_email" and autonomy = medium

Step 1: ApprovalRequired raised by ToolBroker
  → tool_broker checks: does this action require approval for this agent?
  → Yes → raise ApprovalRequired(action_type="send_email", payload={to, subject, body})

Step 2: Executor creates approval record
  → INSERT INTO approvals (task_id, action_type, action_payload, expires_at=+24h)
  → Task status → "awaiting_approval"
  → Emit to activity log: "Waiting for your approval to send this email"
  → Send email notification to member (SendGrid)

Step 3: Frontend
  → Approval inbox badge lights up
  → Member opens approval, sees the exact email Chronos wants to send
  → Clicks Approve / Modify / Reject

Step 4: POST /approvals/{id}/decide
  → Updates approval record
  → Publishes to Redis: PUBLISH approval:{approval_id} {"decision": "approved"}
  → Executor unblocks

Step 5: Executor resumes
  → If approved: tool_broker.execute("gmail.send", payload)
  → If rejected: log, skip step, continue plan
  → Emit result to activity log
```

---

## 5. Context Assembly — The Heart of the System

Every single LLM call goes through `assemble_context()`. This function is what makes Chronos feel like it knows the org.

```python
# apps/api/core/context.py
async def assemble_context(
    conversation_id: str,
    message: str,
    requester_context: RequesterContext
) -> list[dict]:

    parts = []

    # 1. Base system prompt
    base = load_base_system_prompt()

    # 2. Org context folder — all .md files
    org_files = await minio.list_files(f"context/{requester_context.org_id}/")
    org_context = ""
    for f in org_files:
        content = await minio.read(f.path)
        org_context += f"\n\n## {f.name}\n{content}"
    if org_context:
        base += f"\n\n# Organization Context{org_context}"

    # 3. Active persona
    if requester_context.persona_id:
        persona = await db.get_persona(requester_context.persona_id)
        if persona.prompt:
            base += f"\n\n# Your Identity\n{persona.prompt}"

    # 4. Active skills (lazy-loaded on relevance)
    relevant_skills = await skill_registry.find_relevant(
        query=message,
        org_id=requester_context.org_id
    )
    for skill in relevant_skills:
        skill_content = await skill_loader.load_full(skill.id)
        base += f"\n\n# Skill: {skill.name}\n{skill_content}"

    # 5. Memory retrieval — the SEAM
    memories = await memory.retrieve(message, requester_context)
    if memories:
        mem_text = "\n".join([f"- {m.content}" for m in memories])
        base += f"\n\n# What I Remember\n{mem_text}"

    # 6. Current task context (if mid-task)
    if requester_context.task_id:
        task = await db.get_task(requester_context.task_id)
        base += f"\n\n# Current Task\nGoal: {task.goal}\nStep {task.current_step} of {len(task.plan['steps'])}"

    # 7. Conversation history (last 20 messages)
    history = await db.get_messages(conversation_id, limit=20)

    return [
        {"role": "system", "content": base},
        *[{"role": m.role, "content": m.content} for m in history],
        {"role": "user", "content": message}
    ]
```

**The context loading order matters.** Base system prompt establishes Chronos's identity. Org context grounds it in the company. Persona adds the specific role. Skills add task-specific knowledge. Memory adds accumulated learning. History adds conversational continuity. User message is always last. Each layer narrows what Chronos knows about — the pyramid of context.

---

## 6. The Memory System — Technical Implementation

### Memory Retrieval (the seam)

```python
# apps/api/core/memory.py — THE SEAM

async def retrieve(query: str, requester_context: RequesterContext) -> list[MemoryEntry]:
    """
    Phase 1: Unfiltered vector search (requester_context ignored)
    Phase 3: Scope-filtered by authorized_scopes computed from requester_context

    THE CALL SIGNATURE NEVER CHANGES. Only the implementation does.
    """
    query_embedding = await embed(query)

    # Phase 1 implementation (stub):
    return await db.vector_search(
        table="memory_entries",
        vector=query_embedding,
        filters={"is_deleted": False, "organization_id": requester_context.org_id},
        limit=10
    )

    # Phase 3 implementation (drop-in replacement):
    # authorized = compute_authorized_scopes(requester_context)
    # return await db.vector_search(
    #     table="memory_entries",
    #     vector=query_embedding,
    #     filters={
    #         "is_deleted": False,
    #         "organization_id": requester_context.org_id,
    #         "scope_pairs": authorized  # (scope, scope_id) IN (...)
    #     },
    #     limit=10
    # )
```

### Memory Extraction (post-response background job)

```python
# apps/api/memory/extraction.py
async def extract_and_save(
    conversation_id: str,
    user_message: str,
    assistant_response: str,
    requester_context: RequesterContext
) -> None:

    # Ask Chronos to identify salient facts worth remembering
    prompt = f"""
    Identify facts worth remembering from this exchange.
    Return JSON array only. Each item: {{"content": "...", "scope": "personal|workspace|persona|org", "importance": 0.0-1.0}}
    Only include durable, useful facts. Not conversational filler.
    If nothing is worth remembering, return [].

    User: {user_message}
    Assistant: {assistant_response}
    """

    result = await litellm.acompletion(
        model=get_fast_model(),  # cheap model for extraction
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"}
    )

    candidates = json.loads(result.choices[0].message.content).get("memories", [])

    for c in candidates:
        if c["importance"] < 0.6:
            continue  # threshold — not everything gets saved

        embedding = await embed(c["content"])
        scope_id = resolve_scope_id(c["scope"], requester_context)

        entry_id = await db.insert("memory_entries", {
            "organization_id": requester_context.org_id,
            "scope": c["scope"],
            "scope_id": scope_id,
            "content": c["content"],
            "embedding": embedding,
            "source": "autonomous",
            "source_conversation_id": conversation_id,
            "importance_score": c["importance"],
            "created_by": "chronos"
        })

        # Tell frontend to show the inline "Memory saved" notification
        await redis.publish(f"memories:{conversation_id}", json.dumps({
            "type": "memory_saved",
            "entry_id": str(entry_id),
            "content": c["content"],
            "scope": c["scope"],
            "undo_expires": (datetime.now() + timedelta(seconds=60)).isoformat()
        }))
```

### Synthesized Profile (daily background job)

```python
# apps/api/jobs/profile_synthesis.py
@scheduler.scheduled_job("cron", hour=3)  # 3am daily
async def synthesize_all_org_profiles():
    orgs = await db.list_active_orgs()
    for org in orgs:
        await synthesize_org_profile(org.id)

async def synthesize_org_profile(org_id: str):
    # Get last 7 days of conversations
    recent = await db.get_recent_conversations(org_id, days=7)
    if not recent:
        return

    # Summarize into a synthesized profile document
    summary_prompt = f"""
    Based on these recent conversations, write a concise profile of:
    1. Communication patterns and preferences
    2. Recurring topics and projects
    3. Key people and relationships mentioned
    4. Domain vocabulary specific to this organization
    Keep it under 500 words. This will be used to help the AI understand the org better.

    Conversations: {format_conversations_for_summary(recent)}
    """

    profile = await litellm.acompletion(
        model=get_fast_model(),
        messages=[{"role": "user", "content": summary_prompt}]
    )

    # Delete old synthesized profile, insert new one
    await db.delete_where("memory_entries", {
        "organization_id": org_id, "scope": "org", "source": "synthesized"
    })

    embedding = await embed(profile.choices[0].message.content)
    await db.insert("memory_entries", {
        "organization_id": org_id,
        "scope": "org",
        "scope_id": org_id,
        "content": profile.choices[0].message.content,
        "embedding": embedding,
        "source": "synthesized",
        "importance_score": 0.9
    })
```

---

## 7. The Sub-Agent System — Technical Implementation

Sub-agents are `asyncio.Task` instances running their own executor loop. They communicate with the parent via Redis pub/sub. Each sub-agent has its own Redis activity channel.

```python
# apps/api/runtime/sub_agent.py
class SubAgentManager:

    async def spawn_and_wait(self, parent_task: Task, goal: str) -> dict:
        # Enforce depth limit
        if parent_task.depth >= 3:
            # Check for explicit permission
            if not await self._has_spawn_permission(parent_task):
                raise DepthLimitExceeded(f"Max sub-agent depth (3) reached")

        # Create sub-task record
        sub_task = await db.insert("tasks", {
            "organization_id": parent_task.organization_id,
            "parent_task_id": parent_task.id,
            "persona_id": parent_task.persona_id,
            "workspace_id": parent_task.workspace_id,
            "triggered_by": f"task:{parent_task.id}",
            "goal": goal,
            "depth": parent_task.depth + 1,
            "status": "pending"
        })

        # Inherit parent's memory context for this task
        # (passed as initial context, not merged into parent's memory yet)
        inherited_context = await self._snapshot_context(parent_task)

        # Spawn the executor as an asyncio task
        executor = TaskExecutor(
            memory_context=inherited_context,
            activity_channel=f"activity:{sub_task.id}"
        )
        asyncio.create_task(executor.run(sub_task))

        # Notify parent's activity log that a sub-agent was spawned
        await redis.publish(f"activity:{parent_task.id}", json.dumps({
            "type": "sub_agent_spawned",
            "sub_task_id": str(sub_task.id),
            "goal": goal,
            "depth": sub_task.depth
        }))

        # Wait for sub-agent to complete (subscribe to its channel)
        return await self._wait_for_completion(sub_task.id, parent_task.id)

    async def _wait_for_completion(self, sub_task_id: str, parent_task_id: str) -> dict:
        async with redis.subscribe(f"activity:{sub_task_id}") as channel:
            async for message in channel:
                event = json.loads(message)

                # Forward sub-agent events to parent's activity log (nested)
                await redis.publish(f"activity:{parent_task_id}", json.dumps({
                    "type": "sub_agent_event",
                    "sub_task_id": sub_task_id,
                    "event": event
                }))

                if event["type"] == "task_complete":
                    # On completion: save sub-agent profile for future use
                    await self._save_sub_agent_profile(sub_task_id)
                    return event.get("result", {})

                if event["type"] == "task_failed":
                    raise SubAgentFailed(event.get("error"))

    async def _save_sub_agent_profile(self, sub_task_id: str):
        # Save sub-agent's accumulated memory to a sub-task scope
        # User can later promote this to a full persona
        sub_task = await db.get_task(sub_task_id)
        sub_memories = await db.get_memories_by_scope("subtask", sub_task_id)

        profile = {
            "task_id": sub_task_id,
            "goal": sub_task.goal,
            "memory_count": len(sub_memories),
            "created_at": sub_task.completed_at.isoformat(),
            "promotable": True  # User can promote this to a persona
        }

        await db.update_task(sub_task_id, result={**sub_task.result, "profile": profile})
        # Frontend shows: "Sub-agent completed. Save as persona?"
```

### The Activity Log Stream (Frontend ↔ Redis ↔ Executor)

```
Executor publishes events → Redis channel → FastAPI SSE endpoint → Browser EventSource
```

```python
# apps/api/routers/tasks.py
@router.get("/{task_id}/stream")
async def stream_activity(task_id: str, member: Member = Depends(get_current_member)):
    await permission.check(member, "view_task", task_id)

    async def event_stream():
        # Send current task state first (catch-up for late subscribers)
        task = await db.get_task(task_id)
        yield f"data: {json.dumps({'type': 'task_state', 'task': task.to_dict()})}\n\n"

        # Subscribe to live events
        async with redis.subscribe(f"activity:{task_id}") as channel:
            async for message in channel:
                yield f"data: {message}\n\n"
                event = json.loads(message)
                if event["type"] in ("task_complete", "task_failed", "task_cancelled"):
                    break

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

```tsx
// apps/web/components/chat/ActivityLog.tsx
export function ActivityLog({ taskId }: { taskId: string }) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);

  useEffect(() => {
    const es = new EventSource(`/api/tasks/${taskId}/stream`);
    es.onmessage = (e) => {
      const event = JSON.parse(e.data);
      setEvents(prev => [...prev, event]);
    };
    return () => es.close();
  }, [taskId]);

  return (
    <div className="activity-log">
      {events.map(event => (
        <ActivityEvent key={event.id} event={event} />
      ))}
    </div>
  );
}
```

---

## 8. The ToolBroker — Where Everything Gets Enforced

The ToolBroker is the single enforcement point for safety limits, approval gating, rate limiting, and audit logging. Every tool call goes through it. No exceptions.

```python
# apps/api/core/tool_broker.py — THE SEAM (always real from day 1)
class ToolBroker:

    # Per-org state (in Redis — survives restarts)
    async def _get_action_count(self, org_id: str, window_seconds: int = 60) -> int:
        key = f"action_rate:{org_id}:{int(time.time() // window_seconds)}"
        return int(await redis.get(key) or 0)

    async def _increment_action_count(self, org_id: str, window_seconds: int = 60):
        key = f"action_rate:{org_id}:{int(time.time() // window_seconds)}"
        await redis.incr(key)
        await redis.expire(key, window_seconds * 2)

    async def execute(self, agent: AgentContext, tool: str, args: dict) -> ToolResult:
        org_id = agent.org_id

        # 1. Rate limit check
        action_count = await self._get_action_count(org_id)
        if action_count >= 10:
            await audit.log("rate_limit_triggered", agent.id, tool)
            raise RateLimitExceeded("More than 10 external actions/minute")

        # 2. Loop detection
        call_hash = hashlib.md5(f"{tool}:{json.dumps(args, sort_keys=True)}".encode()).hexdigest()
        loop_key = f"loop:{agent.task_id}:{call_hash}"
        call_count = int(await redis.incr(loop_key))
        await redis.expire(loop_key, 300)  # 5 minute window
        if call_count >= 10:
            raise LoopDetected(f"Same action repeated {call_count} times")

        # 3. Hard safety limits (these CANNOT be overridden by permissions)
        await self._enforce_safety_limits(tool, args)

        # 4. Permission check (the seam)
        await permission.check(agent.as_member(), f"use_tool:{tool}", agent.workspace_id)

        # 5. Approval gate check
        approval_required = await self._check_approval_required(agent, tool, args)
        if approval_required:
            raise ApprovalRequired(action_type=tool, payload=args)

        # 6. Log the call
        log_id = await audit.log("tool_call_start", agent.id, tool, payload={"args_hash": call_hash})

        # 7. Execute via connector
        try:
            connector = await connector_registry.get(agent.persona_id or agent.org_id, tool)
            credentials = await credential_vault.get(connector.vault_ref)
            result = await connector.execute(tool, args, credentials)

            await self._increment_action_count(org_id)
            await audit.log("tool_call_complete", agent.id, tool, payload={"result_summary": result.summary})
            return result

        except Exception as e:
            await audit.log("tool_call_error", agent.id, tool, payload={"error": str(e)})
            raise

    async def _enforce_safety_limits(self, tool: str, args: dict):
        if tool == "gmail.send" and len(args.get("to", [])) > 10:
            raise SafetyLimitViolation("Cannot send to more than 10 recipients without batch approval")
        if tool in ("db.delete", "crm.delete_records") and args.get("count", 0) > 5:
            raise SafetyLimitViolation("Cannot delete more than 5 records without confirmation")
        if tool in ("finance.transfer", "payment.send") and args.get("amount", 0) > 100:
            raise SafetyLimitViolation("Cannot move more than $100 without dual approval")
        if tool in ("twitter.post", "linkedin.post", "website.publish"):
            raise SafetyLimitViolation("External publishing always requires explicit approval")
        if tool == "audit_log.write":
            # Allow. audit_log.delete or audit_log.update don't exist — enforced at DB level
            pass
```

---

## 9. The Skill System — Technical Implementation

```python
# apps/api/skills/registry.py
class SkillRegistry:
    def __init__(self):
        self._index: list[SkillMeta] = []  # Loaded at startup

    async def load_all(self):
        # Load all metadata.json files from MinIO skills/ folder
        skill_dirs = await minio.list_dirs("skills/")
        for dir in skill_dirs:
            meta = await minio.read_json(f"skills/{dir}/metadata.json")
            self._index.append(SkillMeta(
                id=dir,
                name=meta["name"],
                description=meta["description"],  # Short description for relevance check
                requires_connectors=meta.get("requires_connectors", []),
                spawns_sub_agent=meta.get("spawns_sub_agent", False)
            ))

    async def find_relevant(self, query: str, org_id: str) -> list[SkillMeta]:
        # Ask the LLM which skills are relevant (cheap, fast model)
        # Only send skill names + descriptions — NOT the full SKILL.md
        skill_list = "\n".join([f"- {s.id}: {s.description}" for s in self._index])
        prompt = f"""
        Given this user message, which skills (if any) are relevant?
        Return a JSON array of skill IDs. Return [] if none apply.
        Skills: {skill_list}
        Message: {query}
        """
        result = await litellm.acompletion(
            model=get_fast_model(),
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        ids = json.loads(result.choices[0].message.content).get("skills", [])
        return [s for s in self._index if s.id in ids]
```

```python
# apps/api/skills/loader.py
class SkillLoader:
    _cache: dict[str, str] = {}  # In-memory cache of loaded skills

    async def load_full(self, skill_id: str) -> str:
        if skill_id in self._cache:
            return self._cache[skill_id]

        # Load SKILL.md + all other files in the skill folder
        files = await minio.list_files(f"skills/{skill_id}/")
        content = ""
        for f in files:
            if f.name == "metadata.json":
                continue
            file_content = await minio.read(f.path)
            content += f"\n\n### {f.name}\n{file_content}"

        self._cache[skill_id] = content
        return content
```

---

## 10. The Connector System — Technical Implementation

```python
# apps/api/connectors/vault.py
class CredentialVault:
    """Encrypts credentials at rest. Only ToolBroker should call this."""

    KEY = os.environ["VAULT_ENCRYPTION_KEY"]  # 32-byte AES key

    async def store(self, connector_id: str, credentials: dict) -> str:
        # Encrypt with AES-256-GCM
        encrypted = self._encrypt(json.dumps(credentials))
        vault_ref = f"vault:{connector_id}"
        await redis.set(vault_ref, encrypted)  # Redis for fast retrieval
        await db.insert("vault_entries", {  # Postgres as backup
            "connector_id": connector_id,
            "encrypted_data": encrypted
        })
        return vault_ref

    async def get(self, vault_ref: str) -> dict:
        encrypted = await redis.get(vault_ref)
        if not encrypted:
            # Fallback to Postgres
            row = await db.get_vault_entry(vault_ref)
            encrypted = row.encrypted_data
        decrypted = self._decrypt(encrypted)
        # NEVER log decrypted credentials — audit only logs vault_ref
        return json.loads(decrypted)
```

```python
# apps/api/connectors/gmail.py
class GmailConnector:
    """Employee-owned Gmail identity via Composio."""

    async def execute(self, tool: str, args: dict, credentials: dict) -> ToolResult:
        client = Composio(api_key=credentials["composio_api_key"])

        if tool == "gmail.read_inbox":
            result = client.execute_action(
                action=Action.GMAIL_FETCH_EMAILS,
                params={"max_results": args.get("limit", 10)},
                entity_id=credentials["entity_id"]  # Chronos's Gmail identity
            )
            return ToolResult(data=result, summary=f"Read {len(result)} emails")

        elif tool == "gmail.draft":
            # ALWAYS create a draft — never send directly
            # Sending requires an approved approval record
            result = client.execute_action(
                action=Action.GMAIL_CREATE_DRAFT,
                params={"to": args["to"], "subject": args["subject"], "body": args["body"]},
                entity_id=credentials["entity_id"]
            )
            return ToolResult(data=result, summary=f"Draft created: {args['subject']}")

        elif tool == "gmail.send":
            # Only reaches here if ToolBroker approved it (approval record exists)
            result = client.execute_action(
                action=Action.GMAIL_SEND_EMAIL,
                params=args,
                entity_id=credentials["entity_id"]
            )
            return ToolResult(data=result, summary=f"Sent to {args['to']}")
```

---

## 11. The Streaming Architecture

Two separate SSE streams run simultaneously when Chronos is working:

**Stream 1: Chat message tokens** (`/chat/message`)
- LLM token stream → FastAPI SSE → Frontend message bubble
- Ephemeral — only live while the message is generating

**Stream 2: Task activity log** (`/tasks/{id}/stream`)
- Redis pub/sub → FastAPI SSE → Frontend activity panel
- Persistent — client can reconnect and get current state

```
Executor ──publish──► Redis channel ──subscribe──► FastAPI SSE ──► Browser EventSource
                      activity:{task_id}            (FastAPI fan-out)
                                                    (one SSE per browser tab)
```

Why Redis pub/sub instead of WebSockets or polling?
- Multiple browser tabs get the same events (fan-out is Redis's job, not ours)
- Multiple API instances can publish to the same channel (horizontal scaling works)
- Reconnection is trivial: subscriber just reconnects and gets caught up from DB state
- Polling would create visible jitter in the activity log (events appear in batches)

---

## 12. The Permission Seam — Phase 1 vs Phase 3

```python
# apps/api/core/permissions.py

# Phase 1 — stub (returns True, still logs)
async def check(actor: Member, action: str, resource: str) -> bool:
    await audit.log("permission_check", actor.id, action,
                    resource_type="generic", resource_id=resource,
                    decision="granted_stub")
    return True


# Phase 3 — drop-in replacement (same signature)
async def check(actor: Member, action: str, resource: str) -> bool:
    result = await openfga_client.check(
        tuple_key={
            "user": f"member:{actor.id}",
            "relation": action,
            "object": f"resource:{resource}"
        }
    )
    decision = "granted" if result.allowed else "denied"
    await audit.log("permission_check", actor.id, action,
                    resource_type="generic", resource_id=resource,
                    decision=decision)
    if not result.allowed:
        raise PermissionDenied(f"{actor.id} cannot {action} on {resource}")
    return True
```

The function signature is identical. Phase 3 is a one-file change that activates across 200+ call sites.

---

## 13. Key Technical Decisions and Why

**Async everywhere.** FastAPI + asyncio means a single thread handles thousands of concurrent SSE connections. The activity log streaming (potentially dozens of open SSE connections per active org) would block a sync server immediately.

**One database for everything.** Simpler ops, one backup, one connection pool, one migration history. The pgvector extension keeps embeddings in the same store as structured data — no separate vector database to maintain, monitor, or sync.

**Redis pub/sub for streaming, not WebSockets.** WebSockets require sticky sessions (load balancer must route a client to the same server instance every time). SSE + Redis pub/sub works with any load balancer — any API instance can serve any client's stream.

**APScheduler, not Celery.** Celery is a full distributed task queue requiring a dedicated worker process, broker configuration, and result backend. APScheduler runs in-process. For Phase 1 with a handful of daily jobs and post-message background tasks, Celery is massive overkill. When job volume demands it, Celery can be added without changing job logic.

**SQLAlchemy Core, not ORM.** The vector similarity search + scope filter is one complex query. The approval query joins five tables with conditional logic. ORM generates suboptimal SQL for both. Core gives exact query control while keeping Python objects instead of raw SQL strings.

**Local LLMs primary.** Inference stays in the org's infrastructure. No data leaves for a cloud model provider unless the client's BYOK key is used. This is also a cost lever — local inference is cheap, frontier model calls are reserved for tasks that actually need them.

**Playwright in a sandboxed subprocess.** Browser automation is the highest-risk capability in the system. Running it in a subprocess with resource limits (memory cap, network allowlist per task) contains bugs, runaway loops, and potential injection attacks to the subprocess level.

**MinIO for file storage.** S3-compatible, runs locally in Docker Compose, no cloud dependency in Phase 1. Phase 3: swap the MinIO endpoint for actual S3. No code change — just an environment variable.

**Pydantic v2 everywhere.** Request validation, response serialization, and all internal data models use Pydantic v2. The performance improvement over v1 matters at the level of messages-per-second Chronos processes. It also catches shape mismatches at the boundary, not deep in business logic.

---

## 14. Local Development Setup

```bash
# One command gets you a full dev environment
git clone https://github.com/cognisia/chronos
cd chronos
cp .env.example .env  # Fill in your local LLM endpoint + backup API key
docker-compose up -d  # Postgres + Redis + MinIO
cd apps/api && pip install -r requirements.txt
alembic upgrade head  # Run all migrations
python seed.py        # Create default org + admin user
uvicorn main:app --reload

# In another terminal
cd apps/web && npm install && npm run dev
```

The seed script creates:
- One org with `id = 'default'`
- One admin member with a test email
- The context folder with a starter `org.md`
- The General and SDR skill packs loaded into MinIO

After setup: open `localhost:3000`, enter the test email, get OTP from the API console log (no SendGrid needed in dev), log in.

---

## 15. What Changes Phase 1 → Phase 3

The seams are why Phase 3 is a refactor, not a rewrite.

| Component | Phase 1 | Phase 3 change |
|-----------|---------|----------------|
| `permission.check()` | Returns True, logs | Replace with OpenFGA client call |
| `memory.retrieve()` | Unfiltered search | Add scope filter from session context |
| Auth | Email OTP, JWT | Add WorkOS/Clerk, SAML+OIDC |
| DB isolation | None (all one org) | Add RLS policies to every table |
| `organization_id` | Config constant "default" | Real UUID from JWT session |
| `region` | Config constant "us" | Real value from org record |
| Approval notifications | Email only | + Slack, Teams |
| File storage | Local MinIO | Same API, S3 endpoint |
| Vault | Redis + Postgres | Same API, add HSM for Phase 4 |

Every Phase 3 change is a localized swap, not a distributed refactor. That's the value of the seams.

---

*Reference: chronos_architecture_v2.md · chronos_build_plan.md*
*Technical architecture version: 1.0*
