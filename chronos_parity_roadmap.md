# Chronos → Claude Parity Roadmap

**Status:** Proposed
**Date:** 2026-05-23
**Owner:** Engineering
**Goal:** Bring Chronos to functional parity with Claude Code and Claude.ai across 10 categories, **without sacrificing the enterprise governance layer** (append-only audit, approval inbox, multi-tenant memory scoping, permission seam) that is Chronos's actual differentiator.

---

## 0. How to Read This Document

Each category below follows the same structure:

- **Current state** — what the code does today, with the real files involved.
- **Target (Claude parity)** — the bar we are matching.
- **Gap** — the precise delta.
- **Design** — component/data/API changes with code sketches.
- **Effort / Risk** — rough sizing and the main failure mode.
- **Verification** — how we prove it works.

The categories are **not** equally important. Read §11 (Sequencing) first if you only read one section. The whole plan hinges on Categories 2 (model) and 1 (agent loop), in that order. Everything else is refinement on a working agent.

### Guiding constraints (assumptions — correct these if wrong)

| Constraint | Assumption |
|---|---|
| Team size | Small (1–3 engineers) |
| Delivery | Incremental; each phase ships independently |
| Budget | BYOK model spend acceptable; cost-control via model tiering |
| Non-negotiable | Every tool call routes through `tool_broker.execute`. No exceptions, even after the loop rewrite. |
| Non-negotiable | `audit_log` stays append-only; approvals stay enforced. |

---

## 1. Agent Loop Architecture

> **The keystone change. Nothing else matters as much.**

### Current state

Chronos uses a **plan-then-execute** model:

- `runtime/planner.py::create_plan(goal, context, org_id)` makes **one** upfront LLM call that returns a fixed list of steps. Each step has an `action` ∈ `{think, tool_call, spawn_sub_agent, approval_gate}`.
- `runtime/executor.py::_run_loop()` iterates that frozen step list. It executes each step, merges results into `task.result`, advances `current_step`, and emits activity events to Redis.
- There is **no path back to the planner**. If step 2's result invalidates the plan, steps 3–N run anyway. `_think()` calls the LLM for *local* reasoning only — it cannot change control flow.

```python
# Today — executor._run_loop (simplified)
steps = _plan_steps(task)               # frozen at plan time
while current_step < len(steps):
    step = steps[current_step]
    result = await self._execute_step(task, step)   # deterministic dispatch
    current_step += 1
    await update_task(task_id, current_step=current_step, result=merged)
```

This is why the system only reliably runs the one workflow the demo plan encodes (`research → qualify → approve`). It is a **scripted workflow runner**, not a general agent.

### Target (Claude parity)

Claude Code and Claude.ai run a **model-in-the-loop** (ReAct / tool-calling) cycle:

```
while not done:
    decision = model(history + tool_schemas)   # model picks next tool OR final answer
    result   = execute(decision.tool, decision.args)
    history.append(result)                      # model sees result, adapts next step
```

The model re-decides at every step. Failed searches get retried with new queries; surprising results redirect the plan. Adaptation is free and continuous.

### Gap

Control flow is frozen at plan time instead of being model-driven per step. No replanning, no result-conditioned branching, no dynamic tool selection.

### Design

Rewrite `runtime/executor.py` into a tool-calling loop. The planner is **demoted** from a hard contract to an optional "suggested plan" the model receives as a hint.

**New executor core:**

```python
# runtime/executor.py — target shape
class TaskExecutor:
    MAX_ITERATIONS = 40            # hard ceiling; loop-detection still applies per-tool

    async def _run_loop(self, task_id: str) -> None:
        task = await get_task(task_id)
        history = await self._seed_history(task)        # goal + suggested plan + memory
        tool_schemas = await self._available_tool_schemas(task)

        for iteration in range(self.MAX_ITERATIONS):
            task = await get_task(task_id)
            if task["status"] in {"cancelled", "failed"}:
                return

            decision = await llm.tool_call(             # NEW: structured tool-choice call
                messages=history,
                tools=tool_schemas,
                model=settings.agent_model,             # frontier model (see §2)
            )

            if decision.type == "final":
                await update_task(task_id, status="complete",
                                  result=decision.result, completed_at=now_utc())
                await emit_activity(task_id, {"type": "task_complete",
                                              "result": decision.result})
                return

            if decision.type == "tool_call":
                await emit_activity(task_id, {"type": "tool_call",
                                              "tool": decision.tool, "args_preview": ...})
                try:
                    result = await tool_broker.execute(   # UNCHANGED SEAM — still mandatory
                        AgentContext.from_task(task), decision.tool, decision.args)
                except ApprovalRequired as e:
                    await self._open_approval(task, decision)   # pause, persist, resume on decide
                    return
                history.append(self._tool_result_msg(decision, result))
                await emit_activity(task_id, {"type": "tool_result",
                                              "summary": result.summary})

        # Iteration ceiling hit
        await self._escalate(task_id, "max_iterations_exceeded")
```

**Key design points:**

1. **The broker stays mandatory.** `decision.tool` is *always* dispatched through `tool_broker.execute`. "Let the model decide" never means "bypass the broker." This is the line that protects governance (§9).
2. **Approvals become a pause/resume checkpoint.** When the broker raises `ApprovalRequired`, the loop persists `history` to `task.result["agent_history"]` and returns. The approval-decide endpoint calls `executor.resume(task_id)`, which rehydrates `history` and continues. `tasks.current_step` is replaced by an opaque history checkpoint.
3. **Planner becomes a hint.** `create_plan` output is injected into the seed history as "Here is a suggested approach; deviate if results warrant." Keep it for cheap guidance; stop treating it as law.
4. **Loop detection still applies.** The broker's existing `_check_loop` (same tool+args ≥10 in 5 min) and a new `MAX_ITERATIONS` ceiling prevent runaway loops.

**New LLM primitive (`core/llm.py`):**

```python
async def tool_call(messages, tools, *, model=None):
    """Frontier tool-calling. Returns {type: 'tool_call'|'final', tool, args, result}."""
    resp = await litellm.acompletion(
        model=model or settings.agent_model,
        messages=messages,
        tools=tools,                       # JSON-schema tool defs
        tool_choice="auto",
        **_provider_kwargs(model),
    )
    return _parse_tool_decision(resp)
```

### Data model changes

- `tasks.plan` JSONB: now stores `{suggested_steps: [...], agent_history: [...]}` instead of an authoritative step list.
- Add `tasks.iteration_count INT DEFAULT 0` for observability.

### Effort / Risk

- **Effort:** High — 2–3 weeks.
- **Risk:** This is where governance can silently regress. Mitigation: a test that asserts **no connector is reachable except through the broker** (grep + a runtime guard). A second risk is cost/runaway loops — mitigated by `MAX_ITERATIONS` + existing loop detection.

### Verification

- Golden task ("research 20 SaaS leads, draft outreach") still produces 20 approvals.
- A **new** adaptive task ("find the careers page for Acme, then extract the hiring manager's email") that the static plan could never handle, now succeeds.
- Audit log shows every tool call passed through the broker.

---

## 2. Driving Model

> **Cheapest change, highest immediate ROI. Do this first.**

### Current state

- `core/config.py` / `.env`: free OpenRouter models (`nvidia/nemotron-3-super-120b`, `minimax/minimax-m2.5:free`).
- These are rate-limited upstream (we hit `RateLimitError` repeatedly) and weak at tool-call JSON, multi-step planning, and long-context reasoning.
- `core/llm.py::complete_json` now falls back fast → main (fixed this session), which masks but does not solve the capability gap.

### Target (Claude parity)

A frontier model (Claude Opus/Sonnet, or GPT-4-class) drives the agent loop and planning; a cheap fast model handles routing, skill selection, and memory extraction.

### Gap

The loop is driven by a model that cannot reliably emit tool calls or plan multiple steps — the single largest capability gap, and it is a config/cost decision, not an architectural one.

### Design — model tiering

```python
# core/config.py additions
agent_model:   str = "claude-sonnet-4-6"       # drives the loop (§1) + planning
fast_model:    str = "openrouter/.../fast"     # intent, skills, extraction
embedding_model: str = "..."                   # unchanged
```

| Call site | Model tier | Why |
|---|---|---|
| Agent loop (`tool_call`) | `agent_model` (frontier) | Tool-call fidelity + reasoning |
| `planner.create_plan` | `agent_model` | Decomposition quality |
| `intent.classify_intent` | `fast_model` | Cheap, high-volume |
| `skills.find_relevant_skills` | `fast_model` | Cheap, high-volume |
| `memory.extraction` | `fast_model` | Background, cost-sensitive |
| `chat stream_completion` | user-selected, default `agent_model` | Quality-facing |

litellm already abstracts providers — this is a config + small routing change in `core/llm.py`. The fast→main fallback chain we built stays as a resilience layer.

### Effort / Risk

- **Effort:** Low — 1–2 days.
- **Risk:** Cost. A multi-step loop with sub-agents on a frontier model is expensive per task. Mitigation: model tiering above; per-org token budgets in the broker; `RATE_LIMITS` already exist.

### Verification

- `classify_intent` confidence rises from ~0.72 (heuristic) to >0.9 (we observed this once on the working model).
- Planner produces task-specific plans, not the SDR demo fallback.

---

## 3. Tool Breadth

> Feeds the loop. A dynamic loop with two fixture tools is still useless.

### Current state

- Two connectors: `connectors/gmail.py` (draft only; `gmail.send` blocked by `_ALWAYS_APPROVAL_TOOLS`) and `connectors/browser.py` (Playwright search/fetch/extract).
- Both honor a tier flag (`__connector_tier` ∈ `live|demo|fixture`); in `DEMO_MODE=true` they return fixtures.
- `connectors/registry.py::get()` looks up a connector row per org/provider; no rows → `ConnectorNotFound`.

### Target (Claude parity)

Claude Code: filesystem ops, sandboxed code execution, real web, and the entire **MCP ecosystem**. Claude.ai: web search, file analysis, code execution, MCP connectors.

### Gap

Tiny tool surface, mostly fixture-backed. No file ops, no code execution, no MCP.

### Design — three additions, in priority order

**(a) MCP client connector — the force multiplier.**
One integration inherits a whole ecosystem of tools, exactly like Claude Code.

```python
# connectors/mcp_client.py
class MCPConnector:
    async def execute(self, tool: str, args: dict, vault_ref: str) -> ToolResult:
        server = self._resolve_server(tool)           # tool name "mcp.<server>.<tool>"
        client = await self._connect(server, vault_ref)
        raw = await client.call_tool(tool.split(".", 2)[2], args)
        return ToolResult(data=raw, summary=f"MCP {tool}")
```

Register MCP servers as `connectors` rows (`provider="mcp"`, `account_handle=<server-id>`). The broker routes `mcp.*` here. Each MCP tool is still permission-checked and audit-logged — governance is preserved automatically because everything goes through the broker.

**(b) Filesystem tool** (sandboxed per-task workspace in MinIO or a scratch dir):
`fs.read`, `fs.write`, `fs.list`. Behind the broker, with a path jail to the task's workspace.

**(c) Sandboxed code execution** (subprocess with resource limits, like the existing Playwright sandbox pattern):
`code.python` — runs in an isolated subprocess, captures stdout/stderr, no network by default.

**(d) Make existing connectors real:** configure `COMPOSIO_API_KEY` + Gmail OAuth (the flow in `connectors/gmail.py::oauth_start_url/oauth_finish` already exists); run `playwright install chromium`. Then flip those connectors to `tier="live"`.

### Data model changes

- No schema change for MCP — reuse `connectors` rows.
- Add a `connector_health` surface (we sketched `core/connector_health.py`) to `GET /settings` so operators see which tools are `live` vs `unconfigured`.

### Effort / Risk

- **Effort:** Med-High — ~2 weeks (MCP client is the bulk).
- **Risk:** Sandbox escape for code execution. Mitigation: subprocess isolation, no network, CPU/mem/time limits, never run as the app user.

### Verification

- Register a public MCP server; the loop calls one of its tools; audit log shows the call.
- A task writes a file, reads it back, and references it in the final result.

---

## 4. Memory

> Closest to parity already. Mostly wiring existing seams.

### Current state

- `core/memory.py::retrieve()` — pgvector cosine search over `memory_entries`, filtered by `organization_id` + `is_deleted`, with a recent-memory fallback when embeddings fail.
- `memory/extraction.py::extract_and_save()` — after each assistant turn, an LLM extracts durable facts (importance ≥ 0.6) and writes them with embeddings; publishes a 60s "undo" event.
- `jobs/profile_synthesis.py` — nightly org-profile synthesis into a memory entry.
- Scopes (`personal|workspace|persona|department|org|restricted`) exist in the schema but **retrieval ignores scope** (Phase 1 stub — returns all org memories).

### Target (Claude parity)

Claude.ai Projects + persistent memory; Claude Code CLAUDE.md + memory files. Editable, scoped, recency-aware.

### Gap

(a) No scope filtering at retrieval. (b) No reranking beyond raw cosine. (c) Memory editor UI not wired to write-back.

### Design

1. **Activate scope filtering** — the seam is already designed for it. In `memory.retrieve`, compute `authorized_scopes` from `RequesterContext` and add a `scope_pairs` filter. The signature does not change (the whole point of the seam).

```python
# memory.retrieve — Phase 3 drop-in (already anticipated in CLAUDE.md)
authorized = compute_authorized_scopes(requester_context)   # personal+own, workspace+member, org
# AND (scope, scope_id) IN authorized
```

2. **Hybrid rerank** — combine cosine similarity with `importance_score` and recency:
`final = 0.6*cosine + 0.25*importance + 0.15*recency_decay`. Sort, take top-10.

3. **Editable memory** — `MemoryEditor.tsx` exists; wire it to the existing `/memory` CRUD endpoints (`memory.py` already has create/delete/undo). Add inline edit.

### Data model changes

None — `memory_entries` already has `scope`, `scope_id`, `importance_score`, `created_at`.

### Effort / Risk

- **Effort:** Low-Med — ~1 week.
- **Risk:** Scope filtering could over-restrict and hide useful org memory. Mitigation: default new writes to `org` (already the case); roll out filtering behind a flag and compare retrieval recall.

### Verification

- A `personal`-scoped memory for user A does not appear in user B's retrieval.
- Reranking surfaces a high-importance recent fact above a marginally-closer stale one.

---

## 5. Sub-Agents

### Current state

- `runtime/sub_agent.py::SubAgentManager.spawn_and_wait()` — creates a child `tasks` row, runs `TaskExecutor().run()` on it, **blocks** the parent via `_wait_for_completion` (subscribes to the child's Redis activity channel), forwards nested events to the parent, returns the child's result.
- Depth-limited to 3 (`DepthLimitExceeded`); `concurrent_sub_agents=5` per org is defined in limits but not yet enforced in spawn.
- Child inherits the parent's full task context.

### Target (Claude parity)

Claude Code's Task tool: **parallel** fan-out, each sub-agent gets an **isolated** context window, results summarized back to the parent.

### Gap

Sequential only; no context isolation; concurrency limit not enforced.

### Design

1. **Parallel spawning.** Add `spawn_many(parent, goals: list[str]) -> list[dict]` that launches N children with `asyncio.gather`, bounded by a semaphore set to `RATE_LIMITS.concurrent_sub_agents`.

```python
async def spawn_many(self, parent, goals):
    sem = asyncio.Semaphore(settings.concurrent_sub_agents)   # default 5
    async def one(goal):
        async with sem:
            return await self.spawn_and_wait(parent, goal)
    return await asyncio.gather(*[one(g) for g in goals])
```

2. **Context isolation.** A child no longer inherits parent history. It gets only: its goal, org/persona context, and memory. This mirrors Claude Code subagents (clean context, return a summary) and keeps token cost bounded.

3. **Summarized return.** Child returns a structured summary (not its full transcript) to the parent's loop history.

4. Once §1 lands, each sub-agent is itself a model-driven loop — no special-casing.

### Data model changes

None — `tasks.parent_task_id`, `depth` already exist.

### Effort / Risk

- **Effort:** Med — ~1 week.
- **Risk:** Parallel sub-agents multiply token cost and can trip per-org rate limits. Mitigation: the semaphore + the broker's existing per-org rate limiter.

### Verification

- A parent task spawns 3 research sub-agents that run concurrently (timestamps overlap in the activity log) and the parent assembles all three summaries.

---

## 6. Skills

### Current state

- `skills/registry.py::load_skill_index()` — loads `metadata.json` from each `skills/<id>/` at startup (cached).
- `skills/loader.py::find_relevant_skills()` — LLM picks ≤2 relevant skills (keyword fallback); `load_skill_content()` concatenates the skill's `*.md` files into context.
- `metadata.json` declares `requires_connectors` and `spawns_sub_agent` but these are **not enforced**.

### Target (Claude parity)

Claude Code skills: multi-file, scripts, templates, progressive disclosure, and skills that unlock tools.

### Gap

(a) Only `*.md` loaded — templates/scripts ignored. (b) `requires_connectors` not enforced. (c) No authoring path.

### Design

1. **Load auxiliary files.** Extend `load_skill_content` to expose templates/scripts as referenceable resources (e.g. list file paths the agent can `fs.read` via §3's filesystem tool), not just inline `.md`.
2. **Enforce `requires_connectors`.** When a skill activates, check its connectors are `live`; if not, surface a clear "this skill needs Gmail connected" message instead of failing mid-task. Ties skills to §3's `connector_health`.
3. **Progressive disclosure.** Inject only the skill's `SKILL.md` summary first; load detail files lazily when the agent references them (saves context budget — relevant to §7).
4. **Authoring.** A `skills/<id>/` scaffold + validation (metadata schema check at startup; already partially there via `load_skill_index`'s try/except).

### Effort / Risk

- **Effort:** Low-Med — ~1 week.
- **Risk:** Skill content bloats the prompt. Mitigation: progressive disclosure + the §7 token budgeter.

### Verification

- Activating `sdr-outreach` with no Gmail connector shows a clear setup prompt, not a crash.
- A skill template file is loaded only when the agent asks for it.

---

## 7. Context Management

### Current state

- `core/context.py::assemble_context()` stacks 6 layers (base prompt → org context → persona → skills → memory → task state) plus the **last 20 messages** (hard limit). No token budgeting, no compaction.

### Target (Claude parity)

Large window (200k+) with automatic compaction when context fills.

### Gap

Fixed 20-message truncation; no awareness of the model's context window; long conversations silently lose history.

### Design

1. **Token budgeter.** Measure the assembled prompt (tiktoken or provider token count). Given `agent_model`'s window, allocate budgets: system layers get a cap, history gets the remainder.
2. **History compaction.** When history exceeds budget, summarize the oldest messages into a running "conversation summary" (reuse the `profile_synthesis` LLM pattern) instead of dropping them. Replace the static `limit(20)` with a dynamic fill.
3. **Layer prioritization.** Under pressure, drop in this order: old history → low-importance memories → secondary skills. Never drop the system base, active persona, or current task state.

```python
# context.py — target
budget = model_window(settings.agent_model) - RESPONSE_RESERVE
system = build_system_layers(...)                    # capped
history = await fit_history(conversation_id, budget - tokens(system))  # summarize overflow
```

### Effort / Risk

- **Effort:** Med — ~1 week. Depends on §2 (need a real window to budget against).
- **Risk:** Summarization loses detail. Mitigation: keep the last K verbatim turns; only summarize older ones; persist summaries so they're stable.

### Verification

- A 200-message conversation stays coherent and within window; older context is summarized, not lost.

---

## 8. Streaming & Artifacts

### Current state

- Dual SSE: chat tokens (`/chat/message`) and activity log (`/tasks/{id}/stream`, Redis-backed).
- `messages.artifact_ids UUID[]` column **exists but is unused**.
- MinIO is wired (used for browser screenshots).

### Target (Claude parity)

Claude.ai artifacts (live docs/code/UI side-panel); Claude Code file diffs.

### Gap

No artifact concept in the product — outputs are plaintext tokens only.

### Design

1. **Artifact storage.** Persist artifacts (markdown, code, structured docs, generated files) in MinIO; record metadata in a new `artifacts` table; link via the existing `messages.artifact_ids`.
2. **Artifact SSE event.** New event type `{"type": "artifact", "artifact_id", "kind", "title"}` streamed alongside tokens.
3. **Frontend panel.** Render artifacts in a side panel in the chat UI (the layout already has a drawer pattern for activity — reuse it).
4. **Versioning.** Artifacts are append-version (edits create new versions), consistent with the audit-everything ethos.

### Data model changes

```sql
CREATE TABLE artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL DEFAULT 'default',
    region TEXT NOT NULL DEFAULT 'us',
    conversation_id UUID,
    task_id UUID,
    kind TEXT NOT NULL,              -- 'markdown'|'code'|'file'|'doc'
    title TEXT,
    minio_path TEXT NOT NULL,
    version INT DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### Effort / Risk

- **Effort:** Med — 1–2 weeks (mostly frontend).
- **Risk:** Scope creep into a full document editor. Mitigation: start read-only render + download; defer live editing.

### Verification

- A task that "writes a competitive brief" produces a markdown artifact in the side panel, stored and versioned in MinIO.

---

## 9. Governance & Permissions

> **Chronos already leads here. The goal is to NOT regress as §1–§3 add power.**

### Current state

- `core/tool_broker.py` — every tool call passes permission check → rate limit → loop detection → safety limits → approval gating → audit (call + result). `_ALWAYS_APPROVAL_TOOLS` (e.g. `gmail.send`, social posts) and `_SAFETY_LIMITS` enforced.
- `audit_log` is append-only at the DB grant level (migration `0005`).
- `permission.check` is a Phase-1 stub returning `True` while logging; the seam is OpenFGA-ready (signature frozen, 200+ call sites).
- Approval inbox works end-to-end (we verified 40 queued drafts).

### Target

N/A — Chronos exceeds Claude here. This category is **defensive**: preserve guarantees while the loop becomes dynamic.

### Gap

Not a feature gap — a discipline. The §1 rewrite makes the model choose tools dynamically, which is exactly when a tool could slip around the broker.

### Design

1. **Broker-only invariant test.** A CI test that statically asserts no module imports a connector directly (only `tool_broker._route` may), plus a runtime guard.
2. **OpenFGA migration.** Replace the stub `permission.check` body with an OpenFGA query — signature unchanged, so zero call-site churn. Model org/workspace/persona/member relations.
3. **Per-tool approval policy stays authoritative.** New tools (MCP, fs, code-exec) declare safety policy; default-deny for anything not explicitly allowed.
4. **Budget guard.** Add per-org token/cost ceilings in the broker (ties to §2 cost risk).

### Effort / Risk

- **Effort:** Low per feature; OpenFGA is a Med project (~1 week) when Phase 3 starts.
- **Risk:** A new tool path bypasses the broker. Mitigation: the invariant test above is the single most important safeguard in this whole roadmap.

### Verification

- The broker-only test passes after the §1 rewrite.
- A `code.python` tool call that tries network access is blocked and audit-logged.

---

## 10. Reliability & Performance

### Current state

- Task execution: `asyncio.create_task(TaskExecutor().run(...))` — in-process, single process.
- `main.py::recover_incomplete_tasks()` resumes `pending|planning|running` tasks on startup (good — survives restart).
- `current_step` is persisted (checkpointing exists).
- APScheduler runs jobs in-process.
- Langfuse/Sentry keys are configured-but-empty → no observability.
- Model calls had no retry/backoff (we hit raw rate-limit errors).

### Target (Claude parity)

Durable, observable, horizontally scalable execution.

### Gap

Single-process execution; no durable queue; no observability; no model-call resilience.

### Design

1. **Durable execution.** Two options — see trade-off below. Minimum: replace the §1 history checkpoint into `tasks.result["agent_history"]` so any worker can resume; enforce idempotent step replay.
2. **Retry/backoff + circuit breaker** on model calls in `core/llm.py` (exponential backoff on 429/5xx; open the circuit after N consecutive failures and surface a clear status). The fast→main fallback we added is the first layer; add backoff on top.
3. **Observability.** Wire the existing Langfuse keys (LLM traces) and Sentry DSN (errors). Every loop iteration emits a Langfuse span.
4. **Health checks.** `/health` that verifies Postgres, Redis, MinIO, and model reachability.

### Trade-off: durable execution — Option A vs B

| Dimension | A: Keep asyncio + checkpoint | B: Redis/Celery worker queue |
|---|---|---|
| Complexity | Low | High |
| Scalability | Single process | Horizontal |
| Failover | Resume-on-restart (exists) | Queue redelivery |
| Team familiarity | High (current pattern) | Lower |
| Time to value | Days | Weeks |

**Recommendation:** Start with **A** (checkpoint hardening) — it's days of work and the resume path already exists. Move to **B** only when concurrent task volume per org justifies horizontal workers.

### Effort / Risk

- **Effort:** Med-High — ~2 weeks for A + observability; B is a later, larger project.
- **Risk:** Duplicate side effects on resume. Mitigation: idempotency — approvals already keyed by `(task_id, step_id)`; extend the same keying to tool calls.

### Verification

- Kill the API mid-task; on restart the task resumes from its last checkpoint without re-sending drafts.
- A Langfuse trace shows every loop iteration; a forced model error appears in Sentry.

---

## 11. Sequencing (Dependency-Ordered)

```
PHASE 0  (week 1)         #2  Model tiering ───────────────► unblocks everything
                          #9  Broker-only invariant test (before the rewrite!)

PHASE 1  (weeks 2–4)      #1  Agent-loop rewrite ──────────► the keystone
                          #3  MCP client + fs/code tools ── feeds the loop

PHASE 2  (weeks 5–7)      #5  Parallel + isolated sub-agents
                          #7  Context budgeting + compaction
                          #4  Memory scope filter + rerank

PHASE 3  (weeks 8–10)     #6  Rich skills (multi-file, connector-aware)
                          #8  Artifacts
                          #10 Durable execution (Option A) + observability
                          #9  OpenFGA migration
```

### Why this order

- **#2 before everything:** a weak model dooms every other category. One config change, instant lift.
- **#9 invariant test before #1:** install the guardrail before the rewrite that could breach it.
- **#1 before #3/#5/#7:** the loop is the substrate; tools, sub-agents, and context budgeting all assume a model-driven loop exists.
- **Polish (#4, #6, #8) last:** valuable but additive; they don't create core capability.

---

## 12. Critical-Path Trade-off (read this twice)

The temptation is to ship visible wins first — artifacts, more connectors, a memory editor. **Resist it.**

- A frontier model in a static plan-executor is *still* a scripted workflow runner.
- A weak model in a dynamic loop *hallucinates tool calls and burns money.*

You need **#2 then #1**, in that order, before any other category moves the needle. Categories 4–10 are refinements on a working agent. If you only have budget for two changes this quarter, do exactly those two.

---

## 13. What Stays Hard (revisit as the system grows)

- **Cost.** Frontier model + multi-step loop + parallel sub-agents is expensive per task. Model tiering (§2) and per-org budget guards (§9) are the levers, but unit economics need monitoring from day one.
- **Governance under dynamism.** The §1 rewrite permanently raises the risk that a tool slips around the broker. The invariant test (§9) must be treated as load-bearing infrastructure, not a nice-to-have.
- **Context cost vs. quality.** Richer skills (§6) and memory (§4) fight the token budget (§7). Progressive disclosure is the reconciler, but it adds latency (extra round-trips to fetch detail).
- **Sandbox security.** Code execution (§3) is the highest-risk new surface. It deserves its own threat model and ADR before shipping.
- **Determinism for the demo.** The current `DEMO_MODE=true` fixtures give reliable demos. A dynamic loop is non-deterministic — keep a fixture/replay mode for sales demos and tests even after the rewrite.

---

## 14. Open Questions

1. Which frontier model is the default `agent_model` — Claude Sonnet (cost) or Opus (capability)? Affects §2 and §13 cost.
2. Is BYOK per-org required day one, or is a shared Cognisia key acceptable for early customers?
3. Code execution (§3c) — in-scope for Phase 1, or deferred until a security review?
4. Durable execution — is single-process (Option A) acceptable through the first N customers, or is horizontal scale a near-term sales requirement?

---

*This roadmap is grounded in the Chronos codebase as of 2026-05-23. File and function references (`runtime/executor.py`, `core/tool_broker.py`, `core/memory.py`, etc.) reflect the current implementation. Revisit after Phase 1 — the agent-loop rewrite will invalidate assumptions in §5, §7, and §10.*
