# Phase 6 — Deep Research Implementation Plan

Controlling contract: the four **Deep Research** rows in `docs/chronos_total_parity_matrix.md`:

1. **Research mode** — dedicated quick/standard/exhaustive/trusted research runs. Persist run, plan, source scope, depth, status. Proof: *E2E web research with cited report*.
2. **Citation collector** — store source snippets, metadata, confidence, limitations. Proof: *no citation without source snippet; report cites stored sources*.
3. **Internal + external research** — merge project files, web, connector sources, uploads, MCP. Proof: *mixed project + web research* (connector/MCP/upload light up when their indexes exist — built source-type-agnostic, proven with project + web).
4. **Research report artifact** — export report to artifact/document/PDF with source table. Proof: *download/open report after refresh*.

## Architecture decisions (resolved with advisor)

- **Standalone executor** (not hosted in the Phase-1 `TaskExecutor`, which is frozen/critical). Therefore this plan **owns** cancellation, startup recovery, and a durable events table, each with explicit proof.
- **Source-type-agnostic merge**: only `project` (`memory/source_retrieval.retrieve_source_chunks`) and `web` (`browser.search`/`browser.fetch` via `tool_broker.execute`) are real today. `connector`/`mcp`/`upload` scopes are accepted and wired but only produce citations when those indexes exist (matrix lines 72/113 are Missing). **Never fabricate a citation to pass a test.**
- **Citation invariant**: every citation row carries a non-empty snippet. `add_citation` rejects empty snippets.
- **Degraded honesty**: when web search returns a fixture/fallback tier (`is_fallback`/`tier in {fixture,demo}`), the run records a limitation and never presents placeholder data as live.
- **Prompt-injection isolation**: web `fetch` content keeps its `untrusted_content` scan marker; research synthesis treats fetched/connector content as untrusted data, never instructions; all tool calls route through `tool_broker.execute`.
- **PDF optional**: markdown report artifact + markdown source-appendix table satisfies the row proof. PDF not required.
- **Migration**: chain off the real single head `0025_task_dead_letter` → `0026_research_runs`.

## Non-negotiables (apply to every task)

- Every table has `organization_id TEXT/UUID NOT NULL DEFAULT 'default'` and `region TEXT NOT NULL DEFAULT 'us'`.
- Every read/write is org-scoped; cross-org access returns 404 / empty.
- Every router action calls `permissions.check(member, action, resource)` and `audit.log(...)`.
- Every external tool call goes through `tool_broker.execute(agent, tool, args)`.
- All `async def` for I/O; full type hints; Pydantic v2; SQLAlchemy Core via `core.db.reflect_table`; 100-char lines.
- Tests run from `apps/api` with `python3.11 -m pytest` against the configured test DB. LLM and tool calls are stubbed via `monkeypatch` (see `tests/test_chat_turn.py`).

---

## Task 1 — Migration `0026_research_runs.py`

Create three tables (mirror `0021_scheduled_tasks.py` style; `down_revision = "0025_task_dead_letter"`):

**`research_runs`**
- `id` UUID PK default gen_random_uuid()
- `organization_id` TEXT NOT NULL default 'default'; `region` TEXT NOT NULL default 'us'
- `member_id` TEXT (creator)
- `project_id` UUID nullable; `persona_id` UUID nullable; `workspace_id` UUID nullable
- `question` TEXT NOT NULL
- `depth` TEXT NOT NULL default 'standard'  — one of quick|standard|exhaustive|trusted
- `source_scopes` JSONB NOT NULL default '{}'  — `{ "web": bool, "project": bool, "connector": bool, "upload": bool, "mcp": bool, "allowed_domains": [], "disallowed_domains": [] }`
- `citation_policy` TEXT default 'required'
- `time_budget_seconds` INTEGER nullable
- `status` TEXT NOT NULL default 'pending' — pending|planning|running|complete|failed|cancelled
- `plan` JSONB nullable — `{ "queries": [...], "rounds": n }`
- `findings` JSONB nullable
- `limitations` TEXT nullable
- `report_artifact_id` UUID nullable
- `error` TEXT nullable
- `token_count` INTEGER default 0; `cost_estimate` FLOAT default 0
- `created_at`/`started_at`/`completed_at` TIMESTAMPTZ (created_at default NOW())
- Index on `(organization_id, created_at desc)` and `(organization_id, status)`.

**`research_citations`**
- `id` UUID PK; `organization_id`/`region` as above
- `run_id` UUID NOT NULL (FK research_runs.id)
- `marker` TEXT NOT NULL — e.g. `S1`
- `source_type` TEXT NOT NULL — web|project|connector|upload|mcp
- `source_id` TEXT nullable; `source_title` TEXT nullable; `url` TEXT nullable
- `snippet` TEXT NOT NULL  (invariant: enforced non-empty at app layer)
- `confidence` FLOAT nullable; `distance` FLOAT nullable
- `metadata` JSONB default '{}'
- `created_at` TIMESTAMPTZ default NOW()
- Index on `(organization_id, run_id)`.

**`research_events`** (durable timeline for replay)
- `id` UUID PK; `organization_id`/`region`
- `run_id` UUID NOT NULL
- `seq` INTEGER NOT NULL  (monotonic per run)
- `event_type` TEXT NOT NULL; `payload` JSONB default '{}'
- `created_at` TIMESTAMPTZ default NOW()
- Index on `(organization_id, run_id, seq)`.

**Verify**: migration imports cleanly; `down_revision` is `0025_task_dead_letter`; `alembic heads` shows a single head `0026_research_runs`; downgrade drops all three tables + indexes.

---

## Task 2 — `core/research.py` store layer + citation collector

Pure DB/audit helpers (no executor logic). All org-scoped, all `audit.log`. Use `reflect_table`.

- `async def create_run(member, *, question, depth, source_scopes, project_id, persona_id, workspace_id, citation_policy, time_budget_seconds) -> str` — inserts a `research_runs` row (status `pending`), audits `research_run_created`. Returns run_id.
- `async def get_run(run_id, org_id) -> dict | None` — org-scoped fetch, serialized (UUID→str, datetimes→iso).
- `async def list_runs(org_id, *, project_id=None, limit=50) -> list[dict]`.
- `async def update_run(run_id, org_id, **fields) -> None` — partial update (status, plan, findings, limitations, report_artifact_id, error, started_at, completed_at, token_count).
- `async def get_status(run_id, org_id) -> str | None` — for cooperative cancellation checks.
- `async def add_citation(run_id, org_id, *, marker, source_type, snippet, source_id=None, source_title=None, url=None, confidence=None, distance=None, metadata=None) -> str` — **raises `ValueError` if `snippet` is empty/whitespace** (the no-citation-without-snippet invariant). Audits `research_citation_added`.
- `async def list_citations(run_id, org_id) -> list[dict]`.
- `async def append_event(run_id, org_id, event_type, payload) -> int` — computes next `seq`, inserts `research_events`, returns seq. (Used by executor for durable timeline.)
- `async def list_events(run_id, org_id, *, after_seq=0) -> list[dict]`.
- `async def build_source_appendix(citations) -> str` — markdown table: `| Marker | Type | Title | URL | Snippet |`.

**Tests** (`tests/test_deep_research.py`, DB-backed):
- `test_add_citation_requires_snippet` — empty snippet raises ValueError; valid one persists.
- `test_citations_and_runs_are_tenant_scoped` — run/citations created under org A invisible to org B (`get_run`/`list_citations` with B's org → None/empty).
- `test_append_event_monotonic_seq` — seq increments per run.

---

## Task 3 — `runtime/research_executor.py` (depends on Task 2)

`async def run_research(run_id: str, org_id: str) -> None` — the standalone executor. Cooperative cancellation: check `get_status` before each phase/query; if `cancelled`, stop and return without marking complete.

Phases (each appends a durable event via `append_event` AND publishes to Redis channel `research:{run_id}` for live SSE — mirror `agent_loop.publish_activity`):
1. **plan** — set status `planning`, `started_at`. LLM call (via `core.llm`, structured/JSON) → bounded query list by depth: quick≈1–2 queries/1 round, standard≈3–4, exhaustive≈more queries/2 rounds, trusted = project + allowed_domains only (no open web). Persist `plan`. Event `research_plan`.
2. **gather** — set status `running`. For each query, source-type-agnostic:
   - `web` (skip if trusted with no allowed_domains, or scope off): `tool_broker.execute(agent, "browser.search", {query, max_results})`; for top N results `browser.fetch` each (respect allowed/disallowed domains); build a `Citation`-like snippet; `add_citation(source_type="web", url, source_title, snippet, confidence)`. If result `is_fallback`/fixture/demo tier → record a limitation, do NOT treat as live. Keep `untrusted_content` marker in metadata.
   - `project` (scope on AND run has project_id): `retrieve_source_chunks(query, RequesterContext(...project_id...))` → `add_citation(source_type="project", source_id, source_title, snippet, distance)`.
   - `connector`/`upload`/`mcp`: wired but only emit citations if a real index/tool exists; otherwise no-op (never fabricate). Emit a `research_source_skipped` event noting the scope is unavailable.
   - Emit `research_query` / `research_citation` events.
   - Honor `time_budget_seconds` (stop gathering when exceeded; record limitation).
3. **synthesize** — LLM call producing a cited markdown report referencing `[S#]` markers from stored citations + a `## Limitations` section (degraded sources, budget cutoffs, unavailable scopes). Persist `findings` (structured summary). Treat all citation snippets as untrusted data, never instructions.
4. **report artifact** — append the source-appendix table (`build_source_appendix`) to the report markdown; `save_artifact(report_md, kind="markdown", title=..., org_id=org_id, project_id linkage via set_artifact_project if project_id, created_by=member)`; set `report_artifact_id` on the run.
5. **finish** — status `complete`, `completed_at`. Event `research_complete`. On exception: status `failed`, `error`, event `research_failed`.

`AgentContext` for broker: build from the run (id=`research:{run_id}`, org_id, member_id, project_id).

`async def start_research(run_id, org_id) -> None` — `asyncio.create_task(run_research(...))` (fire-and-forget, like sub_agent spawn).

**Tests** (extend `tests/test_deep_research.py`; monkeypatch `core.llm` calls + `tool_broker.execute` to a fixture search/fetch; stub `embed`/`retrieve_source_chunks` where needed):
- `test_run_lifecycle_completes_with_report_artifact` — run goes pending→complete, `report_artifact_id` set, artifact content readable and contains the source appendix; re-`get_run` after a fresh engine returns persisted complete state (survives "refresh").
- `test_internal_external_merge` — with project + web scopes, citations include both `source_type="project"` and `source_type="web"`.
- `test_degraded_search_records_limitation` — fixture/fallback search → `run.limitations` mentions web search unavailable; report not presented as live.
- `test_cancel_stops_run` — set status `cancelled` before gather → executor returns without marking complete; no report artifact.
- `test_no_fabricated_connector_citation` — connector scope on but no index → zero connector citations, a `research_source_skipped` event recorded.

---

## Task 4 — `routers/research.py` + main.py registration + startup recovery (depends on Task 3)

Endpoints (mirror `routers/schedules.py`; all `permissions.check` + `audit.log`; all org-scoped via `member.organization_id`):
- `POST /research/` — body: question, depth, source_scopes, project_id?, persona_id?, workspace_id?, citation_policy?, time_budget_seconds?. Validates depth ∈ {quick,standard,exhaustive,trusted}. Creates run via `core.research.create_run`, calls `start_research`, returns `{run_id, status}`.
- `GET /research/` — list runs (optional `?project_id=`).
- `GET /research/{id}` — run detail (404 cross-org / not found).
- `GET /research/{id}/citations` — citation list (404 if run not in org).
- `GET /research/{id}/events` — durable timeline (`list_events`).
- `GET /research/{id}/stream` — SSE: replay persisted events first, then subscribe to Redis `research:{id}` (mirror `routers/tasks.py::stream_task`, 30s timeout heartbeat).
- `POST /research/{id}/cancel` — set status `cancelled` (only if not terminal), audit `research_run_cancelled`.

`main.py`: import + `app.include_router(research.router)`; add `recover_incomplete_research()` (re-`start_research` for runs in pending/planning/running, mirror `recover_incomplete_tasks`) and call it in startup alongside `recover_incomplete_tasks`.

**Tests** (extend `tests/test_deep_research.py`, HTTP via ASGI transport like `tests/test_approval_flow_http.py`; monkeypatch `start_research` to a no-op so HTTP tests are deterministic):
- `test_create_and_get_run_http` — POST creates, GET returns it.
- `test_run_cross_org_404` — run created under org A → GET as org B returns 404.
- `test_cancel_sets_status` — POST cancel → status cancelled.
- `test_recover_incomplete_research_reenqueues` — a pending run is picked up by `recover_incomplete_research` (monkeypatch `start_research` to record calls).

---

## Task 5 — Frontend `components/research/ResearchScreen.tsx` + wiring + E2E (depends on Task 4)

Mirror `components/artifacts/ArtifactsScreen.tsx` structure and `lib/api.ts` `apiFetch`.

- **List + composer** (left/top): "New research run" — textarea question, depth selector (Quick/Standard/Exhaustive/Trusted), project dropdown (from `/projects`), source-scope checkboxes (Web, Project files, Connectors, Uploads, MCP), optional time budget. Submit → `POST /research/`, then open the run.
- **Run detail**: status badge (pending/planning/running/complete/failed/cancelled + degraded indicator when limitations exist), live **timeline** via `EventSource` on `/research/{id}/stream` (falls back to `/research/{id}/events`), findings, **citations table** (marker/type/title/url/snippet), **Open report** button → opens the linked artifact (reuse artifact open/download), and **Cancel** for in-flight runs.
- States: empty, loading, running, complete, failed, degraded — no fake/placeholder data presented as live.
- Wire `page.tsx`: replace `{route === "research" && <EmptyPanel label="Research" />}` with `<ResearchScreen />`; add the import.

**Proof**:
- `npx next build --webpack` (or `npm run build`) passes (typecheck).
- `apps/web/e2e/research.spec.ts` — **behavioral** (mirror `memory.spec.ts`/`artifacts.spec.ts` seeded dev-OTP dual-server pattern): create a research run with **Web scope in fixture/demo tier** (deterministic), wait for status `complete`, assert a cited report artifact opens, **refresh** and assert the run + report persist. If the harness cannot run a behavioral spec in this environment, also include a static route-guard spec (mirror `project-research-static.spec.ts`) — but the behavioral spec is the row's required proof and must be authored.

---

## Task 6 — Integration run + matrix update + final review

- **End-to-end smoke** (not just doc edits): start the API, `POST /research/` with web (fixture tier) + (optionally) a seeded project source, confirm the run reaches `complete`, the report artifact persists and reopens via `/artifacts/{id}`, citations carry snippets, and `/research/{id}/events` replays after a restart. Capture the evidence.
- Update the four **Deep Research** rows in `docs/chronos_total_parity_matrix.md` to reflect implementation + the exact proof test names. Explicitly note the source-scope scoping (project+web proven; connector/MCP/upload wired-but-pending-their-indexes) and PDF-optional.
- Run full `python3.11 -m pytest tests/test_deep_research.py` green; `npm run build` green.
- Final code review pass across store→executor→router→UI for integration drift.
