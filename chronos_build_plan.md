# Chronos — Operational Build Plan

**Reference doc:** `chronos_architecture_v2.md`
**Builder:** Solo + Codex + Claude Code + Cursor
**Constraint:** Cannot operate at a loss. Revenue reinvested into development.

---

## The Full Timeline at a Glance

```
PHASE 1 ── Weeks 1-8  ── One Chronos, one client, SDR demo
GATE    ── 2 weeks of daily client use
PHASE 3 ── Weeks 9-14 ── Multi-tenant before client 2
PHASE 2 ── Weeks 9-14 ── Product polish (parallel track, lower priority)
GATE    ── Multi-tenant live, client 2 onboarded
PHASE 4 ── Week 15+   ── Enterprise hardening, on-demand
```

Phase 2 and Phase 3 run in parallel. Phase 3 is the primary track.
Phase 4 items are never built speculatively — only when a specific deal requires them.

---

## How to Use This Document

Each sprint has three sections:

- **What gets built** — the specific components, in order
- **What Codex gets asked** — the prompt structure to hand to Codex or Claude Code
- **Exit criteria** — the test that tells you the sprint is done

Don't start the next sprint until exit criteria are met. Don't add scope mid-sprint.

---

## Phase 1, Sprint 1 — Weeks 1-2: Skeleton + Seams

### What Gets Built

**Day 1-2: Repo and infrastructure**
- Monorepo: `/apps/web` (Next.js), `/apps/api` (FastAPI), `/packages/shared`
- Docker Compose: Postgres 15 + pgvector, Redis, MinIO
- Environment config: `.env` with `ORG_ID=default`, `REGION=us`, model keys
- Database migrations tooling (Alembic)
- First migration: `organizations`, `members`, `conversations`, `messages`, `audit_log` tables
  - Every table has `organization_id UUID NOT NULL DEFAULT 'default'`
  - Every table has `region TEXT NOT NULL DEFAULT 'us'`
  - `audit_log` has no UPDATE or DELETE permissions, enforced by migration

**Day 3-4: Auth**
- Email OTP: user enters work email, gets 6-digit code, logs in
- JWT session management (short-lived access token, refresh token in httpOnly cookie)
- Single hardcoded org member in seed data — the client's admin
- `POST /auth/request-otp`, `POST /auth/verify-otp`, `POST /auth/refresh`

**Day 5-6: Model layer**
- litellm wrapper with local LLM as primary, fallback to env-configured backup key
- Streaming response support (SSE)
- `POST /chat/message` → streams tokens back to client
- Conversation persistence: every message written to `messages` table on completion
- Basic context assembly: last N messages prepended to each request

**Day 7-8: Chat UI**
- Next.js chat interface: sidebar (conversation list) + main chat area
- Streaming message display (tokens appear as they arrive)
- Conversation history: clicking a past conversation loads it
- New conversation button
- No persona selector yet — single default Chronos identity

**Day 9-10: The Three Seams + Audit**

This is the most important work of Phase 1. These three functions must exist before any other feature is built.

```python
# /apps/api/core/permissions.py
async def check(actor: Member, action: str, resource: str) -> bool:
    # STUB: Phase 1 always returns True
    # Phase 3: queries OpenFGA
    await audit.log_permission_check(actor, action, resource, granted=True)
    return True

# /apps/api/core/memory.py
async def retrieve(query: str, requester_context: RequesterContext) -> list[Memory]:
    # STUB: Phase 1 returns unfiltered vector search
    # Phase 3: filters by requester_context.authorized_scopes
    return await vector_search(query, limit=10)

# /apps/api/core/tool_broker.py
async def execute(agent: Agent, tool: str, args: dict) -> ToolResult:
    # Phase 1: passes through with logging
    # Phase 3+: adds rate limiting, approval gating, spend cap checking
    await audit.log_tool_call(agent, tool, args)
    result = await tool_registry.call(tool, args)
    await audit.log_tool_result(agent, tool, result)
    return result
```

Context folder loader (also Week 1):
```python
# /apps/api/core/context.py
async def load_org_context(org_id: str) -> str:
    # Reads all .md files from /context/{org_id}/ folder
    # Returns concatenated content, prepended to every system prompt
    # Phase 1: reads from local filesystem
    # Phase 3: reads from MinIO per-tenant bucket
```

Audit log: every `audit.log_*` call does an `INSERT INTO audit_log`. No reads in Phase 1.

**Day 10: Seed the demo org**
- Seed script: one org (`default`), one member (the client's admin email), empty context folder
- `python seed.py` gives a working dev environment in under 30 seconds

### What Codex Gets Asked (Sprint 1 Prompt Structure)

```
You are building Chronos, an AI employee platform.
Reference: chronos_architecture_v2.md (attached)

Sprint 1 goal: working skeleton with the three critical seams.

Build in this exact order:
1. Monorepo structure (Next.js + FastAPI)
2. Docker Compose (Postgres+pgvector, Redis, MinIO)
3. Database migrations (organizations, members, conversations, messages, audit_log)
   - Every table must have organization_id and region columns
   - audit_log must be INSERT-only (no UPDATE, no DELETE, enforce via migration)
4. Email OTP auth (request-otp, verify-otp endpoints + JWT sessions)
5. litellm model layer with streaming SSE endpoint
6. Conversation persistence
7. The three seam functions (permission.check, memory.retrieve, tool_broker.execute)
   - ALL THREE must be stubs in Phase 1
   - ALL THREE must write to audit_log even as stubs
   - DO NOT implement real logic — just stubs with logging
8. Context folder loader (reads /context/{org_id}/*.md, returns concatenated string)
9. Chat UI (Next.js, streaming, conversation history)
10. Seed script

Hard constraints:
- organization_id = "default" everywhere (config constant, not hardcoded strings)
- region = "us" everywhere (config constant)
- The three seams are NEVER bypassed — every tool call goes through tool_broker.execute,
  every memory read goes through memory.retrieve, every action check goes through permission.check
- audit_log gets an INSERT on every action, no exceptions
```

### Sprint 1 Exit Criteria

- [ ] `docker-compose up` starts all services cleanly
- [ ] Admin logs in with work email via OTP
- [ ] Admin sends a message to Chronos, gets a streaming response
- [ ] Conversation persists — refreshing the page shows the same conversation
- [ ] Past conversations load from the sidebar
- [ ] `permission.check`, `memory.retrieve`, `tool_broker.execute` all exist in `/apps/api/core/`
- [ ] Every Chronos response loads the context folder and prepends it to the system prompt
- [ ] `audit_log` has rows after a conversation

---

## Phase 1, Sprint 2 — Weeks 3-4: Memory System

### What Gets Built

**Structured memory table**
```sql
CREATE TABLE memory_entries (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL DEFAULT 'default',
    scope TEXT NOT NULL,           -- personal/workspace/persona/department/org/restricted
    scope_id TEXT NOT NULL,        -- the ID of the scoped entity
    content TEXT NOT NULL,
    embedding VECTOR(1536),
    source TEXT,                   -- 'autonomous' | 'explicit' | 'synthesized'
    source_conversation_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    created_by TEXT,               -- member_id or 'chronos'
    importance_score FLOAT DEFAULT 0.5,
    is_deleted BOOLEAN DEFAULT FALSE
);
CREATE INDEX ON memory_entries USING ivfflat (embedding vector_cosine_ops);
```

**Memory writes — two paths**

Autonomous: after each assistant response, Chronos runs a lightweight "memory extraction" pass. Identifies salient facts (names, preferences, decisions, facts about the org). For each candidate memory, Chronos assigns a scope and writes it. Every autonomous write surfaces inline in the chat: "Memory saved: [content] ([scope]) — Undo?"

Explicit: user says "remember that..." or similar. Chronos parses the instruction, writes the memory, confirms. User sees: "Got it, I'll remember that."

**Undo mechanism**: autonomous writes have a 60-second undo window in the chat UI. After 60 seconds they're permanent (but still editable in settings).

**Retrieve through the seam**
Update `memory.retrieve` stub to actually run a vector search (still unfiltered by scope — that comes in Phase 3). The call signature doesn't change:

```python
async def retrieve(query: str, requester_context: RequesterContext) -> list[Memory]:
    # Phase 1: unfiltered. Phase 3: filtered by authorized_scopes.
    embeddings = await embed(query)
    return await db.vector_search(
        table="memory_entries",
        vector=embeddings,
        limit=10,
        filters={"is_deleted": False}
        # Phase 3 adds: AND (scope, scope_id) IN authorized_scopes
    )
```

**Memory editor UI**
Settings panel → Memory tab. Lists all memory entries. Each entry shows: content, scope, date, source (Chronos/you). Edit inline. Delete. Add manually.

**Synthesized profile — Phase 1 version**
Background job runs every 24 hours. For each org: takes the last 7 days of conversations, asks Chronos to summarize communication patterns, project context, and recurring themes. Writes the output as a `scope=org, source=synthesized` memory entry. Replaces the previous synthesized entry.

This is the basic version — a daily summary document, not a sophisticated per-user profile. Good enough for Phase 1.

**Context folder daily update**
Background job: runs every 24 hours. Checks if any new meaningful information appeared in recent conversations that isn't in `org.md` yet. If yes, proposes an edit to `org.md` and surfaces it in the admin dashboard as "Chronos suggests updating your context — Review."

### What Codex Gets Asked (Sprint 2 Prompt Structure)

```
Sprint 2: Memory system.

Reference: chronos_architecture_v2.md §8, Sprint 2 in the build plan.

Build in this order:
1. memory_entries table migration (schema above, with pgvector index)
2. Embedding service (litellm embed endpoint, cached in Redis)
3. Autonomous memory extraction (post-response job, surfaces inline with undo)
4. Explicit memory write (parse "remember that..." instructions)
5. Update memory.retrieve to run actual vector search (still no scope filtering)
6. Memory editor UI in settings panel
7. Synthesized profile background job (daily, last 7 days of conversations)
8. Context folder daily update job (proposes edits to org.md, admin reviews)

Constraints:
- memory.retrieve signature MUST NOT change — only the implementation
- Every memory write goes through audit_log
- Autonomous writes must surface inline with 60-second undo
- Scope column must exist on every memory entry (default 'org' for now)
- Background jobs run via APScheduler or Celery Beat
```

### Sprint 2 Exit Criteria

- [ ] Chronos autonomously saves memories during conversation — they appear inline
- [ ] User can say "remember that..." and Chronos saves it, confirms
- [ ] Undo works within 60 seconds of an autonomous write
- [ ] Memory editor in settings shows all saved memories, editable, deletable
- [ ] Chronos retrieves relevant memories at the start of each response
- [ ] Daily synthesized profile job runs (can trigger manually for testing)
- [ ] Context folder update job proposes edits when relevant (can trigger manually)

---

## Phase 1, Sprint 3 — Weeks 5-6: Connectors

### What Gets Built

**ToolBroker upgrade**
The stub from Sprint 1 gets real routing logic. Still no approval gating (Sprint 4). But it now:
- Routes tool calls to the correct connector based on tool name
- Checks that the tool exists in the ConnectorRegistry for this agent
- Enforces per-org action rate limits (10+ actions/minute → block, log, notify)
- Enforces loop detection (same tool + same args 10 times → escalate)

**ConnectorRegistry**
```python
class ConnectorRegistry:
    # Per-agent registry of connected systems
    # Phase 1: stored in Postgres (connectors table)
    # Lookup: given agent_id + tool_name → connector instance + credentials
```

**CredentialVault**
```python
class CredentialVault:
    # Stores encrypted credentials (AES-256) per connector per agent
    # get_credentials(agent_id, connector_name) → decrypted credentials
    # All access logged to audit_log
```

**Gmail connector (via Composio)**
Port `ComposioProviderAdapter` from Forge-apr8.

Capabilities needed for Demo 1:
- `gmail.read_inbox` — read recent emails
- `gmail.send` — send email from Chronos's identity
- `gmail.draft` — create draft (used in approval flow — create draft, send only after approval)

OAuth flow: admin triggers from Settings → Connectors → "Connect Gmail for Chronos." Standard OAuth2 flow, token stored in CredentialVault under Chronos's identity.

**Web browser / research connector**
Playwright-based autonomous browsing. Capabilities:
- `browser.search(query)` — web search, returns structured results
- `browser.fetch(url)` — fetch and parse a web page
- `browser.extract_contacts(url)` — extract name/email/title from a company page
- `browser.linkedin_search(query)` — LinkedIn people/company search (public data only)

The browser runs in a sandboxed subprocess. Each browsing session is isolated. Screenshots captured and stored for the activity log.

**Connector status dashboard**
Settings → Connectors. Shows: connected accounts, Chronos identity for each (e.g., `alex@novatech.com`), last used, auth status (green/yellow/red). "Reconnect" button if auth has expired.

**Phase 1 connector limits**
Only two connectors ship in Phase 1: Gmail + browser. No Calendar, no HubSpot, no Slack yet. Those are Phase 2.

### What Codex Gets Asked (Sprint 3 Prompt Structure)

```
Sprint 3: Connectors.

Reference: chronos_architecture_v2.md §10, §11 (ToolBroker), Sprint 3 in build plan.

Build in this order:
1. connectors table migration (connector_name, agent_id, credential_vault_ref, status, last_used)
2. CredentialVault (AES-256 encryption, Postgres-backed, all access audit-logged)
3. ConnectorRegistry (routes tool_name → connector + credentials)
4. ToolBroker upgrade (real routing, rate limit enforcement, loop detection)
5. Composio Gmail adapter (port from Forge-apr8 ComposioProviderAdapter)
   - gmail.read_inbox, gmail.send, gmail.draft
   - Employee-level OAuth flow (Chronos's own identity, not the user's)
6. Browser connector (Playwright, sandboxed subprocess)
   - browser.search, browser.fetch, browser.extract_contacts
7. Connector status dashboard UI (Settings → Connectors)

Constraints:
- ALL connector calls route through tool_broker.execute — no direct connector calls
- Credentials NEVER appear in logs or error messages — vault reference only
- gmail.send MUST create a draft, not send directly — sending requires approval (Sprint 4)
- Browser runs in isolated subprocess, screenshots captured per step
- Rate limit: >10 external actions/minute triggers block + admin notification
- Loop detection: same (tool + args hash) 10 times → escalate to human
```

### Sprint 3 Exit Criteria

- [ ] Admin can connect Gmail via OAuth — Chronos gets its own Gmail identity
- [ ] Chronos can read its inbox
- [ ] Chronos can create email drafts (NOT send yet — that's Sprint 4 with approval)
- [ ] Chronos can browse the web: search, fetch pages, extract contact info
- [ ] All connector calls appear in audit_log with tool name, args hash, result status
- [ ] Connector status dashboard shows connected accounts
- [ ] Rate limiting triggers correctly at 10+ actions/minute
- [ ] Loop detection triggers at 10 identical calls

---

## Phase 1, Sprint 4 — Weeks 7-8: Runtime + Skills + Sub-Agents + Demo

### What Gets Built

**Skill system**
```
/skills/
  general/
    SKILL.md
    metadata.json
  sdr-outreach/
    SKILL.md
    icp-qualification.md
    outreach-templates/
      cold-email-1.md
      follow-up-1.md
    metadata.json       — declares: requires gmail connector, requires browser connector
```

Skill loader:
- On startup: read all `metadata.json` files, load skill names + descriptions into context
- On task: identify relevant skills from descriptions, load full `SKILL.md` + resources
- Multiple skills can be active simultaneously

**SDR/Sales skill pack content**
`SKILL.md` contains:
- How to interpret an ICP description
- Step-by-step lead research process
- Qualification criteria and scoring logic (0-10 scale with dimension breakdown)
- Email personalization rules ("reference the company's recent funding/product/hiring")
- Output format: lead report + draft emails + qualification scores
- Approval requirement: "all outbound emails must go through approval before send"

**Task engine + planner**

```sql
CREATE TABLE tasks (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL DEFAULT 'default',
    agent_id TEXT NOT NULL,
    parent_task_id UUID,           -- for sub-tasks
    triggered_by TEXT,             -- message_id | scheduled | webhook
    status TEXT DEFAULT 'pending', -- pending/planning/running/awaiting_approval/complete/failed
    goal TEXT NOT NULL,
    plan JSONB,                    -- the steps Chronos planned
    current_step INT DEFAULT 0,
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    token_count INT DEFAULT 0,
    cost_estimate FLOAT DEFAULT 0
);
```

Planner-executor loop:
1. Receive goal → generate plan (list of steps with tool calls)
2. Persist plan to `tasks` table
3. Execute step by step, updating `current_step` after each
4. On failure: retry (3x), try alternative, escalate
5. On approval-required step: pause → write to `approvals` table → notify → resume on approval
6. On completion: write result, update status, emit final response

Multi-hour tasks: state is fully in Postgres. Process can restart mid-task and resume from `current_step`.

**Sub-agent spawning**

Three spawn paths, all producing the same thing — a new task with `parent_task_id` set:

```python
# Path 1: Chronos decides
if task_requires_parallelization(current_step):
    sub_task = await spawn_sub_agent(parent_task=current_task, goal=sub_goal)

# Path 2: User requests
if user_message.intent == "spawn_sub_agent":
    sub_task = await spawn_sub_agent(parent_task=current_task, goal=user_specified_goal)

# Path 3: Skill declares
if "spawns_sub_agent" in skill.metadata:
    sub_task = await spawn_sub_agent(parent_task=current_task, goal=skill.sub_agent_goal)
```

Sub-agent memory: inherits parent's memory context for task duration. Writes to `scope=subtask, scope_id=task_id`. On completion: merged into parent or promoted to persona.

Recursion enforcement: before spawning, check depth. If `depth >= 3` and no explicit permission → block spawn, log, continue without sub-agent.

**Activity log streaming (Manus-style UI)**

Right panel in the chat UI. Real-time SSE stream of:
- Current agent action ("Searching LinkedIn for AI companies in NYC...")
- Tool call details (collapsible — tool name, args, result summary)
- Sub-agent cards (each sub-agent is a nested expandable card)
- Screenshot thumbnails from browser sessions (click to expand)
- Approval pending states (inline approve/reject buttons)
- Token count and estimated cost (running total)

**User takeover:** each agent card has a "Take over" button. Clicking opens the agent's browser/computer session in an interactive iframe. User can type, click, scroll. Handing back to Chronos resumes automation.

**Sub-conversation panel**
Bottom sheet or side panel: "Talk to sub-agent." User can directly address any active sub-agent. Sub-agent responds in context of its current task.

**Approval inbox**

```sql
CREATE TABLE approvals (
    id UUID PRIMARY KEY,
    organization_id UUID NOT NULL DEFAULT 'default',
    task_id UUID NOT NULL,
    agent_id TEXT NOT NULL,
    action_type TEXT NOT NULL,     -- 'send_email' | 'delete_records' | etc.
    action_payload JSONB NOT NULL, -- the exact content awaiting approval
    requested_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,        -- 24h default
    status TEXT DEFAULT 'pending', -- pending/approved/rejected/expired
    decided_by UUID,               -- member_id
    decided_at TIMESTAMPTZ,
    decision_note TEXT
);
```

Inbox UI: `/approvals` page. Lists pending approvals. Each shows: what Chronos wants to do, the exact content (email text, record to delete, etc.), approve/modify/reject buttons. Batch approval available.

Notification: in-app badge on the approvals nav item. Email notification (send via SendGrid — one-time setup).

**Hard safety limits enforcement (in ToolBroker)**
- `gmail.send` with >10 recipients → block, escalate
- Any delete action with >5 records → block, escalate  
- Financial actions >$100 → block, escalate
- Audit log write → always allowed, never blocked

**Demo 1 end-to-end test**

Before calling Sprint 4 done, run the full demo manually:

1. Tell Chronos: "Find 20 B2B SaaS companies, Series A/B, 50-200 employees, US-based, actively hiring salespeople. Draft personalized cold outreach for each."
2. Watch Chronos plan the task in the activity log.
3. Watch research sub-agent browse and extract leads.
4. Watch Chronos qualify and score.
5. Watch draft emails appear.
6. Approval inbox shows 20 pending emails.
7. Approve 3 individually, approve the rest in batch.
8. Emails are drafted in Gmail (not sent yet — verify drafts exist in Gmail).

### What Codex Gets Asked (Sprint 4 Prompt Structure)

```
Sprint 4: Runtime, skills, sub-agents, approvals. This is the core product.

Reference: chronos_architecture_v2.md §9 (skills), §11 (runtime loop), §13 (approvals), Sprint 4 in build plan.

Build in this order:
1. Skill folder loader (reads SKILL.md + metadata.json, lazy-loads on activation)
2. SDR/Sales skill pack (SKILL.md with ICP qualification, research process, email templates)
3. tasks table migration + planner-executor loop
   - Plan generation (Chronos generates a JSON plan of steps)
   - Step-by-step execution with state persistence
   - Failure handling: retry 3x, try alternative, escalate
   - Multi-hour support: full state in Postgres, survives process restart
4. approvals table migration + approval inbox UI
   - Pending approval pauses task execution
   - In-app inbox with approve/modify/reject
   - Batch approval
   - 24-hour expiry
   - Email notification on new approval (SendGrid)
5. Sub-agent spawning (all three paths: Chronos-initiated, user-initiated, skill-declared)
   - parent_task_id linking
   - Memory inheritance for task duration
   - Recursion limit: 3 levels, enforced before spawn
6. Activity log streaming (Manus-style, right panel, real-time SSE)
   - Current action display
   - Tool call details (collapsible)
   - Sub-agent cards (nested, expandable)
   - Browser screenshots
   - Running token count
7. User takeover (interactive browser session via iframe)
8. Sub-conversation panel (talk directly to any sub-agent)
9. Hard safety limits in ToolBroker:
   - >10 email recipients → block
   - >5 record deletes → block
   - >$100 financial action → block
   - Audit log always permitted

Demo 1 must run end-to-end before this sprint is closed.
```

### Sprint 4 Exit Criteria

- [ ] Skills load correctly — SKILL.md content affects Chronos behavior
- [ ] Task is persisted with plan + step-by-step progress to Postgres
- [ ] Task survives process restart (kill API, restart, task resumes)
- [ ] Sub-agents spawn from all three paths
- [ ] Sub-agents appear as nested cards in the activity log
- [ ] User can take over a sub-agent's browser session
- [ ] User can talk directly to a sub-agent
- [ ] Approvals pause task execution, appear in inbox, resume on decision
- [ ] Safety limits block correctly (test each one)
- [ ] **Demo 1 runs end-to-end without any manual intervention** ← the real gate

---

## Phase 1 Gate

**Condition:** the first client's team uses Chronos daily for two consecutive weeks.

When this gate fires:
- Stop adding features to Phase 1
- Immediately begin Phase 3 (multi-tenant)
- Do NOT onboard client 2 until Phase 3 is live
- Phase 2 polish work continues in parallel, lower priority

---

## Phase 3 — Weeks 9-14: Multi-Tenant Infrastructure

This is the hard refactor. The seams placed in Phase 1 pay off here.

### Sprint 5 (Weeks 9-10): Auth + Tenant Isolation

**WorkOS or Clerk integration.** Replace email OTP with a full auth provider. SAML + OIDC support. All existing email OTP sessions remain valid through a migration period.

**Postgres RLS.** Add RLS policies to every tenant table. The `organization_id` column goes from a hardcoded constant to a real per-row filter.

```sql
-- Applied to every tenant table
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON messages
    USING (organization_id = current_setting('app.current_org_id')::uuid);
```

The API sets `app.current_org_id` from the JWT on every request. Application code never needs a WHERE clause for tenant isolation — the database enforces it.

**Multiple members per org.** The members table goes from one hardcoded user to a real multi-member system with roles (owner, admin, supervisor, user). Invitation flow. Role-based UI (supervisors see approval queues, users see their own conversations).

**Second admin requirement.** Onboarding flow enforces designation of a second owner before the org is fully activated.

Exit: create two test orgs. Confirm data is invisible across tenants. Confirm RLS blocks cross-tenant reads at the DB level (test with raw SQL).

### Sprint 6 (Weeks 11-12): Workspaces + Permissions

**Workspace layer.** `workspaces` table, `workspace_members` table. Workspace scoped context: which connectors are active, which memory scope applies, which personas are available.

**OpenFGA.** Wire the `permission.check()` seam to real ReBAC. Migrate hardcoded "return True" to actual tuple evaluation. The 200+ call sites don't change — only the implementation.

```python
async def check(actor: Member, action: str, resource: str) -> bool:
    # Phase 3: real OpenFGA query
    result = await openfga.check(
        tuple_key={"user": actor.id, "relation": action, "object": resource}
    )
    await audit.log_permission_check(actor, action, resource, granted=result.allowed)
    return result.allowed
```

**Persona creation and management UI.** Admin can create named personas (Alex, Jordan), configure their skill packs, connectors, and personality. Invoke by name in chat.

Exit: permission check blocks an unauthorized action (test by revoking a tool permission and confirming the block).

### Sprint 7 (Weeks 13-14): Memory Scoping + Client 2

**Scope-aware memory retrieval.** Wire the `memory.retrieve()` seam to use real scope filtering. The requester's authorized scopes are computed from their session context and passed to the vector search.

```python
async def retrieve(query: str, requester_context: RequesterContext) -> list[Memory]:
    # Phase 3: real scope filtering
    authorized = await compute_authorized_scopes(requester_context)
    return await db.vector_search(
        table="memory_entries",
        vector=await embed(query),
        filters={"scope_scope_id_pairs": authorized, "is_deleted": False},
        limit=10
    )
```

**Account recovery flows.** Domain re-verification, backup contact designation at onboarding, manual support escalation path.

**Client 2 onboards.** Their data goes into a separate org. Test that client 1's data is invisible to client 2. Confirm audit logs are separate.

Exit: client 2 onboarded. Cross-tenant data test passes. Scope-filtering test: a restricted memory is invisible to a member not on the ACL.

---

## Phase 2 — Weeks 9-14 (Parallel Track, Lower Priority)

These run alongside Phase 3 but never block it.

| Sprint | Work |
|--------|------|
| 9-10 | Additional vertical skill packs: Legal, Finance |
| 11-12 | Phase 2 connectors: Calendar, HubSpot/Salesforce, Slack |
| 13-14 | BYOK model connector menu + model routing rules |
| 13-14 | Usage dashboard (token counts, action counts, cost estimates) |
| 13-14 | HR + Operations skill packs |
| Ongoing | Demo 4 capability (day-in-the-life multi-workflow) |

---

## Phase 4 — Week 15+: Enterprise Hardening (On-Demand Only)

Build these only when a specific deal requires them. Never speculatively.

| Feature | Build Trigger |
|---------|--------------|
| Temporal for approval workflows | When delegation/timeouts become real requirements |
| Audit log → Kafka + S3 | When Postgres query performance degrades |
| Multi-region (EU, APAC) | When specific client deal requires it |
| SCIM provisioning | When specific enterprise deal requires it |
| Database-per-tenant | When a client's load demands it |
| SOC 2 Type 2 | At ~40-50 clients |
| HIPAA | After SOC 2 |
| ISO 42001 / NIST AI RMF | After SOC 2 |
| Formal SLA commitments | After SOC 2 |
| Bug bounty program | Phase 4 |

---

## How to Work With Codex Effectively

### One Sprint, One Codex Session

Start a new Codex session at the beginning of each sprint. Attach:
- `chronos_architecture_v2.md`
- `chronos_build_plan.md` (this document)
- The sprint prompt from this document

Codex will generate a lot of code. Your job in each sprint: review the seam implementations specifically — every other part can be wrong and fixed later, but a seam implemented incorrectly now means a rewrite in Phase 3.

### The Seam Review Checklist

After every Codex session, before merging:

```
□ Does any tool call bypass tool_broker.execute? → Fix it.
□ Does any memory read bypass memory.retrieve? → Fix it.
□ Does any action bypass permission.check? → Fix it.
□ Does any connector call expose credentials in logs? → Fix it.
□ Does any table lack organization_id? → Fix it.
□ Does audit_log have any UPDATE or DELETE anywhere? → Fix it.
```

### Claude Code for Debugging

Use Claude Code (not Codex) for debugging specific issues — it's better at reading existing code and understanding what's wrong. Codex for generation, Claude Code for diagnosis.

### Cursor for UI

The activity log UI, sub-agent cards, and approval inbox are complex React components. Cursor with the full codebase context handles these better than Codex's one-shot generation.

---

## The Budget Reality

The build must not operate at a loss. What this means practically:

- No managed services with non-trivial cost before revenue (Temporal, large Redis clusters, multi-region DBs)
- Local LLMs primary for inference — this is also an architecture advantage
- Managed Postgres (Supabase or Railway) from Phase 3 — cheap enough (~$25/month) and eliminates backup/DR ops work
- Langfuse cloud tier for Phase 1 (free up to a point)
- Sentry free tier for Phase 1
- SendGrid free tier (100 emails/day) covers Phase 1 approval notifications
- Vercel for Next.js hosting (generous free tier)

First revenue (client 1, even at a discounted early-adopter price) funds Phase 2 and Phase 3 infrastructure costs. Structure the first client deal so payment starts by Week 6 of Phase 1.

---

## Summary

```
WEEK 1-2:   Skeleton + seams. Chat works. Audit logs. Context folder loads.
WEEK 3-4:   Memory system. Chronos remembers across conversations.
WEEK 5-6:   Connectors. Chronos has Gmail and browses the web.
WEEK 7-8:   Runtime. Demo 1 runs end-to-end.
GATE:       2 weeks of daily client use.
WEEK 9-10:  Auth upgrade + tenant isolation. RLS live.
WEEK 11-12: Workspaces + OpenFGA. Real permissions.
WEEK 13-14: Memory scoping + client 2 onboards.
WEEK 15+:   Enterprise hardening, on-demand.
```

Every line of Phase 1 code goes through the three seams.
Every table has `organization_id`.
The audit log is append-only forever.
Client 2 does not onboard until Phase 3 is live.

---

*Reference: chronos_architecture_v2.md*
*Build plan version: 1.0*
