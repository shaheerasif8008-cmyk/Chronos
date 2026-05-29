# Phase 2 & 3 — Unified Product Shell + Projects and Knowledge Sources

> **For agentic workers:** Use superpowers:subagent-driven-development to execute this plan task-by-task. Each task is implemented by a fresh subagent, then reviewed for spec compliance and code quality before moving on.

**Source of truth:** `CHRONOS_TOTAL_PARITY_GOAL.md` (Implementation Phases **2** and **3**) and the controlling contract `docs/chronos_total_parity_matrix.md`. Each task below names the matrix row(s) it satisfies and copies that row's acceptance proof into its Done criteria.

**Phase 2 = Unified Product Shell.** Phase 3 = Projects and Knowledge Sources.

---

## Non-Negotiable Rules (every subagent must obey)

These are from `CLAUDE.md` and override any instinct to "improve" things:

1. **Three seams, no exceptions.** Every memory read goes through `memory.retrieve()`. Every external tool call goes through `tool_broker.execute()`. Every action check goes through `permission.check()`. Never inline these.
2. **Every new table** has `organization_id UUID/TEXT NOT NULL DEFAULT 'default'` and `region TEXT NOT NULL DEFAULT 'us'`.
3. **`audit_log` is append-only.** Use `core.audit.log(...)` for audit events; never UPDATE/DELETE it.
4. **Credentials never logged** — only `vault_ref`.
5. **Surgical changes only.** Touch only what the task needs. Do not refactor unrelated code, do not reformat adjacent lines, match existing style. Remove only orphans your own change creates.
6. **Tenant isolation** is part of acceptance for every Phase 3 row: queries filter by `organization_id`; cross-tenant access returns nothing.
7. **TDD.** Write the failing test first, then implement, then make it pass. Run the named test command.

## Architecture facts (verified in the current checkout)

- **Frontend is a single monolithic SPA.** `apps/web/app/chat/page.tsx` (~2776 lines) is the whole app. Every other route (`memory`, `approvals`, `connectors`, `activity`, `assistants`, `settings`) is a one-line `export { default } from "../chat/page";`. The component reads `usePathname()`, maps it to a `Route` union via `routeFromPath`/`pathForRoute`, and renders the matching surface. Navigation lives in the `Sidebar` component's `nav` array. **New surfaces extend this pattern** — add a `Route` value, a path mapping, a `nav` entry, a re-export page file, and a render branch. Do NOT rewrite the monolith or introduce a competing layout system.
- **Frontend helpers** (top of `chat/page.tsx`): `apiBase()`, and elsewhere `apiFetch`, `getToken`. Tailwind classes use project utilities like `surface`, `border-soft`, `btn btn-ghost`. Icons come from an `IC.*` set.
- **Backend** is FastAPI + SQLAlchemy Core with `reflect_table(name)` for table access and `engine.begin()` for transactions. Routers live in `apps/api/routers/`, registered in `apps/api/main.py`. Auth dependency is `get_current_member`. Config constants: `settings.org_id` (="default"), `settings.region` (="us").
- **Migrations** are Alembic, sequential, in `apps/api/migrations/versions/`. **Head is `0016_task_checkpoints`.** Pre-allocated numbers below — do not deviate, to avoid collisions.
- **Embeddings:** `core.embeddings.embed(text) -> list[float]`, dimension **1536** (`EXPECTED_EMBEDDING_DIMENSIONS`). pgvector is installed; memory_entries uses `VECTOR(1536)` with ivfflat cosine index.
- **Context assembly:** `core.context.assemble_context()` builds layered system prompt (base → org → tool manifest → persona → skills → memory → task → history). New context layers insert here.
- **Artifacts:** `core.artifacts.save_artifact(content, *, kind, title, conversation_id, task_id, message_id, org_id, region, mime_type) -> id`; `get_artifact`, `read_artifact_content`. Stored in MinIO with local `/tmp` fallback.
- **Tests:** pytest + pytest-asyncio, mostly `monkeypatch`-mocked (no live DB needed for unit tests); some integration tests touch DB/MinIO. Mirror the closest existing test's style. Run from `apps/api`.

## Pre-allocated migration numbers (DO NOT CHANGE)

| Rev id | Adds |
|---|---|
| `0017_messages_rich` | `messages`: `model`, `mode`, `citations JSONB`, `tool_traces JSONB`, `memory_refs JSONB`, `artifact_refs JSONB`, `approval_state TEXT`, `runtime_status TEXT`, `parent_message_id UUID`, `pinned BOOLEAN`. `conversations`: `project_id UUID`. `tasks`: `project_id UUID`, `mode TEXT`. All nullable / safe defaults. |
| `0018_projects` | `projects`, `project_members` tables. |
| `0019_attachment_parsing` | `artifacts`: `parent_artifact_id UUID`, `parse_status TEXT` (from the doc-parsing plan). |
| `0020_project_sources` | `project_sources`, `project_source_chunks` (vector) tables. |

Each migration's `down_revision` chains to the previous in this table (0017→0016, 0018→0017, 0019→0018, 0020→0019).

---

# PHASE 2 — Unified Product Shell

## Task 1 — Shell foundation: all 12 surfaces in the nav

**Matrix rows:** "Main surfaces" (Phase 2 goal §2); supports every later surface.

**Files:**
- Modify: `apps/web/app/chat/page.tsx` (Route union, `routeFromPath`, `pathForRoute`, `Sidebar` nav array, top-level render switch)
- Create re-export pages: `apps/web/app/projects/page.tsx`, `apps/web/app/research/page.tsx`, `apps/web/app/tasks/page.tsx`, `apps/web/app/artifacts/page.tsx`, `apps/web/app/agents/page.tsx`, `apps/web/app/workflows/page.tsx`, `apps/web/app/audit/page.tsx` (each: `export { default } from "../chat/page";`)

**Steps:**
1. Read `chat/page.tsx` lines 1–170 and 460–560 to learn the exact `Route` union, `routeFromPath`, `pathForRoute`, and `Sidebar.nav` shapes.
2. Extend the `Route` union with: `projects`, `research`, `tasks`, `artifacts`, `agents`, `workflows`, `audit`. Add their `/path` ↔ route mappings in both helper functions.
3. Add nav entries (icon + label) for each new surface in the `Sidebar` `nav` array, grouped sensibly (Chat, Projects, Research, Tasks, Artifacts, Memory, Connectors, Agents, Workflows, Approvals, Activity/Audit, Settings). Reuse an existing `IC.*` icon or a sensible placeholder; do not invent a new icon system.
4. Add a render branch in the main switch for each new route. For now each renders a **clear, honest empty state** ("Projects — coming together below", etc.) — NOT fake data. Later tasks fill them in. Each empty-state panel must render without crashing and be reachable by URL.
5. Create the seven re-export page files.

**Done criteria (acceptance proof):**
- Every surface is reachable by URL and from the sidebar; existing surfaces (chat/memory/approvals/connectors/activity/assistants/settings) are unchanged in behavior.
- `cd apps/web && npx tsc --noEmit` is clean.
- No placeholder data is presented as live (rule from goal §17): empty states say "nothing here yet", not fabricated rows.

---

## Task 2 — Rich message model (persistence + render)

**Matrix row:** "General chat assistant" — *Conversation/message records include model, mode, citations, tool traces, memory refs, artifact refs, approval state, status.* Also underpins "Reasoning and tool mode control".

**Files:**
- Create: `apps/api/migrations/versions/0017_messages_rich.py`
- Modify: `apps/api/routers/chat.py` (`_save_message` writes the new columns; `_agent_loop_stream` and `stream()` collect tool traces / artifact refs / memory refs / model / mode / status and persist them on the assistant message; `list_messages` already returns all columns via `select(messages)`)
- Modify: `apps/web/app/chat/page.tsx` (render persisted `tool_traces`, `artifacts`, `citations`, `model`, `mode`, `status` from history, not only live SSE)
- Test: `apps/api/tests/test_rich_messages.py`

**Steps:**
1. Write migration `0017_messages_rich` (down_revision `0016_task_checkpoints`) adding to `messages`: `model TEXT`, `mode TEXT`, `citations JSONB DEFAULT '[]'`, `tool_traces JSONB DEFAULT '[]'`, `memory_refs JSONB DEFAULT '[]'`, `artifact_refs JSONB DEFAULT '[]'`, `approval_state TEXT`, `runtime_status TEXT`, `parent_message_id UUID`, `pinned BOOLEAN DEFAULT FALSE`; to `conversations`: `project_id UUID`; to `tasks`: `project_id UUID`, `mode TEXT`. Provide a working `downgrade()`.
2. Extend `_save_message` to accept and persist `model`, `mode`, `citations`, `tool_traces`, `memory_refs`, `artifact_refs`, `approval_state`, `runtime_status`, `parent_message_id` (all optional, default to existing behavior when omitted). Keep the signature backward compatible (keyword-only new args).
3. In `_agent_loop_stream` and the fast `stream()` path, accumulate the tool traces / artifact refs already surfaced as SSE events and the model/mode, then persist them on the assistant message row when it is saved. (The agent loop currently persists the answer itself; thread metadata through `create_task_record`/loop or do a post-hoc `update` of the assistant message — pick the lowest-risk path and document it in the PR.)
4. Frontend: when loading history (`list_messages`), hydrate `tool_traces`/`artifacts`/`citations`/`model`/`mode`/`status` from the row so a **refreshed** page shows the same metadata the live run showed. The `Message` type already has `tool_traces`/`artifacts`; extend it with `citations`/`model`/`mode`.
5. Tests (`test_rich_messages.py`, mock DB via the existing patterns): assert `_save_message` persists the new fields; assert `list_messages` round-trips them.

**Done criteria (acceptance proof):**
- *API and Playwright test: send message, stream answer, refresh, history and metadata persist.* (Backend half here; the metadata is durable and returned by `list_messages`. Playwright persistence is exercised in Task 5/Final.)
- `cd apps/api && pytest tests/test_rich_messages.py -v` green; `alembic upgrade head` applies cleanly; `npx tsc --noEmit` clean.

---

## Task 3 — Composer mode selector

**Matrix row:** "Reasoning and tool mode control" — *User can select default, research, agent, browser, computer, data, image, voice, or coding modes; message/task mode persisted and shown in UI.*

**Files:**
- Modify: `apps/api/routers/chat.py` (`ChatRequest.mode: str | None`; thread `mode` into the assistant message and into `create_task_record`)
- Modify: `apps/api/routers/tasks.py` (`create_task_record(..., mode=...)` persists `tasks.mode`)
- Modify: `apps/web/app/chat/page.tsx` (mode chips/selector in composer; send `mode`; show mode on messages)
- Test: `apps/api/tests/test_chat_modes.py`

**Steps:**
1. Add `mode` to `ChatRequest` (default `null` → "default"). Validate against the allowed set: `{default, research, agent, browser, computer, data, image, voice, coding}`; unknown → "default".
2. Persist `mode` on the assistant message (uses Task 2's `_save_message`) and, when a task is created, on `tasks.mode`.
3. Frontend composer: a compact mode selector (chips or dropdown) above/in the textarea; selection is sent as `mode` and displayed on resulting messages. Default mode requires no extra click.
4. Test: mode flows from request → message row + task row; unknown mode coerced to default.

**Done criteria (acceptance proof):**
- *Each mode creates the expected task/message type and status* — message + task carry the chosen `mode`, persisted and shown.
- `pytest tests/test_chat_modes.py -v` green; `npx tsc --noEmit` clean.

---

## Task 4 — Global search + command palette

**Matrix row:** "Conversation search" — *Search across conversations, tasks, artifacts, memory, and sources; returns only authorized results with type filters.*

**Files:**
- Create: `apps/api/routers/search.py` (`GET /search?q=&types=`)
- Modify: `apps/api/main.py` (register router)
- Modify: `apps/web/app/chat/page.tsx` (Cmd/Ctrl-K palette + a search surface; both call `/search`)
- Test: `apps/api/tests/test_search.py`

**Steps:**
1. `GET /search` takes `q` (required) and optional `types` (csv of `conversations,messages,tasks,artifacts,memory,sources`). For each requested type, run an `ILIKE`/text query **filtered by `organization_id == member.organization_id`** (and `member_id` where the entity is member-scoped, e.g. conversations). Memory results must go through `memory.retrieve()` (the seam) rather than a raw query. Return a unified list: `{type, id, title, snippet, url}`. Call `permission.check(member, "search", "global")` first.
2. Cap each type (e.g. 10) and total results; order by recency within type.
3. Frontend: Cmd/Ctrl-K opens a palette that queries `/search` as you type (debounced) and lets you jump to a result (push its `url`). Also add a `search` surface for full results with type filters (this can reuse the palette component).
4. Test (`test_search.py`, mocked): results are filtered to the caller's org; unauthorized/other-org rows never appear; type filter narrows results; memory path uses `memory.retrieve`.

**Done criteria (acceptance proof):**
- *Search returns only authorized results with type filters.* Cross-org rows excluded; `types=` narrows.
- `pytest tests/test_search.py -v` green; `npx tsc --noEmit` clean.

---

## Task 5 — Chat controls

**Matrix row:** "Chat controls" — *Edit prompt, regenerate, branch, retry from here, pin, copy, export, save to memory, convert to task/workflow; message action endpoints and branch/fork lineage; survive refresh.*

**Files:**
- Modify: `apps/api/routers/chat.py` (new message-action endpoints)
- Modify: `apps/web/app/chat/page.tsx` (per-message action menu)
- Test: `apps/api/tests/test_chat_controls.py`

**Steps:**
1. Backend endpoints (all behind `permission.check`, all audited):
   - `POST /chat/conversations/{cid}/messages/{mid}/pin` and `/unpin` → toggle `messages.pinned`.
   - `PATCH /chat/conversations/{cid}/messages/{mid}` → edit a **user** message's content (records `parent_message_id` lineage / new branch as appropriate).
   - `POST /chat/conversations/{cid}/messages/{mid}/branch` → fork the conversation from a message into a new conversation, copying prior messages and setting `parent_message_id`. Returns the new conversation id.
   - `POST /chat/conversations/{cid}/messages/{mid}/save-memory` → write the message content to memory via `core.memory_writes.create_memory_entry` (org scope).
   - `POST /chat/conversations/{cid}/messages/{mid}/convert-task` → create a task from the message via `create_task_record`. Returns task id.
   - Regenerate / retry-from-here reuse the existing send path with the prior user message and `parent_message_id` set (document the approach).
   - Copy and export are client-side (copy to clipboard; export = download conversation as markdown/json); export may use an endpoint if cleaner.
2. Frontend: a per-message action menu wiring each control. Edits/branches/pins must reflect after **refresh** (durable).
3. Tests: pin toggles persist; branch creates a new conversation with copied history + lineage; save-memory writes through the memory seam; convert-task creates a task. All filtered by org/owner.

**Done criteria (acceptance proof):**
- *Playwright: edit/regenerate/branch/save-memory/convert-task survive refresh.* Backend + durable state here; Playwright covered in Final suite.
- `pytest tests/test_chat_controls.py -v` green; `npx tsc --noEmit` clean.

---

# PHASE 3 — Projects and Knowledge Sources

## Task 6 — Projects core (schema + CRUD + surface)

**Matrix rows:** "Projects" — *projects with instructions, members, conversations, sources, memory, tasks, artifacts, tools; E2E create project, add member/source/chat/artifact, enforce access.*

**Files:**
- Create: `apps/api/migrations/versions/0018_projects.py`
- Create: `apps/api/routers/projects.py`
- Modify: `apps/api/main.py` (register)
- Modify: `apps/api/routers/chat.py` (accept `project_id`; persist on conversation), `apps/api/routers/tasks.py` (`create_task_record(..., project_id=...)`)
- Modify: `apps/web/app/chat/page.tsx` (Projects list surface + project detail with tabs `chat | sources | artifacts | tasks | research`; URL space `/projects` and `/projects/[id]` — add a dynamic page or query param within the monolith pattern)
- Test: `apps/api/tests/test_projects.py`

**Steps:**
1. Migration `0018_projects` (down_revision `0017_messages_rich`):
   - `projects`: `id UUID pk`, `organization_id`, `region`, `name TEXT NOT NULL`, `instructions TEXT`, `visibility TEXT DEFAULT 'private'`, `default_tools JSONB DEFAULT '[]'`, `memory_policy TEXT DEFAULT 'default'`, `created_by TEXT`, `created_at`, `updated_at`.
   - `project_members`: `id`, `organization_id`, `region`, `project_id UUID NOT NULL`, `member_id UUID NOT NULL`, `role TEXT DEFAULT 'member'`, `created_at`, unique `(project_id, member_id)`.
2. `projects.py` router: CRUD + membership (`POST /projects`, `GET /projects`, `GET /projects/{id}`, `PATCH /projects/{id}`, `DELETE /projects/{id}`, `POST /projects/{id}/members`, `DELETE /projects/{id}/members/{mid}`, `GET /projects/{id}/conversations`, `GET /projects/{id}/artifacts`, `GET /projects/{id}/tasks`). Every route `permission.check` + audited; every query filtered by `organization_id`; access requires the caller be a project member (or org admin). List endpoints return only projects the caller can see.
3. Thread `project_id`: `ChatRequest.project_id` persists to `conversations.project_id`; `create_task_record(..., project_id=...)` persists to `tasks.project_id`.
4. Frontend: `/projects` lists the caller's projects with a create form; `/projects/[id]` shows tabs. Chat opened inside a project sends `project_id`.
5. Tests: create project; add member; non-member of another org/project cannot read it (403/empty); conversations/tasks created with `project_id` are linked.

**Done criteria (acceptance proof):**
- *E2E create project, add member/source/chat/artifact, enforce access* — create + member + linkage + access enforcement proven (sources arrive in Task 8; artifacts link via existing artifact rows).
- `pytest tests/test_projects.py -v` green; `alembic upgrade head` clean; `npx tsc --noEmit` clean.

---

## Task 7 — Project instructions in context

**Matrix row:** "Project instructions" — *Project-level instruction layer merged safely with system/user context; affects answers only inside the project; edits audited.*

**Files:**
- Modify: `apps/api/core/models.py` (`RequesterContext.project_id: str | None = None`)
- Modify: `apps/api/core/context.py` (new layer: load project instructions when `project_id` set, insert between persona and skills)
- Modify: `apps/api/routers/chat.py` (set `requester_context.project_id` from conversation/request)
- Modify: `apps/api/routers/projects.py` (audit instruction edits — likely already audited via PATCH; ensure an explicit `project_instructions_updated` audit event)
- Test: `apps/api/tests/test_project_instructions.py`

**Steps:**
1. Add `project_id` to `RequesterContext`.
2. In `assemble_context`, after the persona layer, if `requester_context.project_id` is set, load that project's `instructions` (org-filtered) and append as `# Project Instructions\n...` within the system budget.
3. In chat, resolve `project_id` (from request or the conversation row) and set it on `requester_context`.
4. Audit instruction edits with a dedicated event.
5. Test: project instructions appear in assembled context only when `project_id` is set and the project belongs to the org; absent otherwise.

**Done criteria (acceptance proof):**
- *Test project instruction affects answers only inside the project.* Context includes instructions iff in-project.
- `pytest tests/test_project_instructions.py -v` green.

---

## Task 8 — Source upload (execute doc-parsing plan + project_sources wrapper)

**Matrix rows:** "Source upload" — *Upload PDFs, docs, slides, sheets, text, code, images, URLs; source record, object path, parse status, extracted text, chunks.* Plus "General file upload" and "Document intelligence" (multimodal) foundations.

**This task has two parts.**

### 8a. Execute the existing doc-parsing plan verbatim
Follow `docs/superpowers/plans/2026-05-26-doc-parsing-ocr-attachments.md` tasks 1–12 **as written**, with ONE change: its migration is renumbered to **`0019_attachment_parsing`** with `down_revision = "0018_projects"` (not `0016`). This delivers: parsing engine (`parsing/engine.py`), `vision_ocr`, `doc__parse`/`doc__read` broker tools, `POST /attachments`, parse-on-send + agent-context injection, and chat upload UI. Run its full suite (`pytest tests/test_doc_parsing.py -v`).

### 8b. Project sources wrapper
**Files:** Create `apps/api/migrations/versions/0020_project_sources.py` (defines `project_sources` + `project_source_chunks`; see Task 9 for chunk columns — create both tables here so indexing has its target); modify `apps/api/routers/attachments.py` (accept optional `project_id`; when present, after storing the attachment, insert a `project_sources` row); test `apps/api/tests/test_project_sources.py`.

- `project_sources`: `id UUID pk`, `organization_id`, `region`, `project_id UUID NOT NULL`, `source_type TEXT` (`upload|url|connector`), `title TEXT`, `artifact_id UUID` (for uploads), `uri TEXT` (for url/connector), `parse_status TEXT DEFAULT 'pending'`, `index_status TEXT DEFAULT 'pending'`, `connector_id UUID`, `permissions JSONB DEFAULT '{}'`, `created_by TEXT`, `created_at`, `updated_at`.
- When `POST /attachments` is called with `project_id` (caller must be a project member), create the attachment artifact (existing flow) and a `project_sources` row referencing it. Audit `source_added`.

**Done criteria (acceptance proof):**
- *Upload PDF/CSV/code folder and see indexed status* (indexed status arrives in Task 9; here: uploaded file becomes a project source with `parse_status`).
- doc-parsing suite green; `pytest tests/test_project_sources.py -v` green; migrations apply.

---

## Task 9 — Source indexing (chunk, embed, refresh, remove, reindex)

**Matrix row:** "Source indexing" — *Chunk, embed, refresh, remove, reindex, cite, permission-resync; ask source-grounded question with cited chunk; remove source removes retrieval.*

**Files:**
- Create: `apps/api/memory/source_indexing.py` (chunk + embed pipeline)
- Modify: `apps/api/routers/projects.py` or new `apps/api/routers/sources.py` (`POST /projects/{id}/sources/{sid}/reindex`, `DELETE .../sources/{sid}`, `POST .../sources/{sid}/refresh`, `GET .../sources`)
- Modify: `apps/api/main.py` if new router
- (uses `0020_project_sources` `project_source_chunks` table)
- Test: `apps/api/tests/test_source_indexing.py`

**`project_source_chunks` columns (created in 0020):** `id UUID pk`, `organization_id`, `region`, `project_id UUID`, `source_id UUID NOT NULL`, `chunk_index INT`, `content TEXT`, `embedding VECTOR(1536)`, `token_count INT`, `created_at`. ivfflat cosine index on `embedding`.

**Steps:**
1. `source_indexing.py`: given a `project_sources` row whose attachment is parsed (reuse `parsing.engine` / the parsed_text artifact from Task 8), split into ~800-token chunks with overlap, `embed()` each (1536-dim), insert `project_source_chunks` rows, set `project_sources.index_status='indexed'`. Make it idempotent (delete prior chunks on reindex).
2. Endpoints: reindex (re-run pipeline), refresh (re-parse + reindex), delete (remove source + its chunks → retrieval no longer returns it), list (with parse/index status). All org-filtered, member-gated, audited.
3. Trigger indexing automatically after a project upload parses (call the pipeline as a background task), or via explicit reindex.
4. Tests: indexing produces chunks with embeddings; delete removes chunks; reindex is idempotent (no duplicate chunks).

**Done criteria (acceptance proof):**
- *Ask source-grounded question with cited chunk; remove source removes retrieval* (retrieval+citation in Task 10; here: chunks exist, delete removes them, reindex idempotent).
- `pytest tests/test_source_indexing.py -v` green.

---

## Task 10 — Permission-aware retrieval + citations

**Matrix rows:** "Source indexing" (retrieval half) + "Citation collector" + "Retrieval across project sources/memory/history" (Phase 3 goal §3 final bullet).

**Files:**
- Create: `apps/api/memory/source_retrieval.py` (`retrieve_source_chunks(query, requester_context) -> list[Citation]`)
- Modify: `apps/api/core/context.py` (when `project_id` set, add a "Project Knowledge" layer with retrieved chunks + citation markers)
- Modify: `apps/api/routers/chat.py` (collect citations used and persist them on the assistant message's `citations` column from Task 2)
- Modify: `apps/web/app/chat/page.tsx` (render citations under assistant messages)
- Test: `apps/api/tests/test_source_retrieval.py`

**Steps:**
1. `source_retrieval.py`: embed the query, vector-search `project_source_chunks` filtered by `organization_id` AND `project_id` (and only sources the caller may access). Return chunk content + `{source_id, source_title, chunk_index}` citation metadata. **Permission-aware:** a caller who is not a project member gets nothing.
2. In `assemble_context`, when `project_id` is set, retrieve top source chunks and add a `# Project Knowledge` block with inline citation markers (e.g. `[S1]`). Respect token budget like other layers.
3. In chat, persist the citations actually surfaced on the assistant message (`citations` JSONB). No citation without a stored source snippet (the collector stores the snippet that grounded it).
4. Frontend renders a citations list under the message linking to the source.
5. Tests: source-grounded query returns chunks from the right project only; non-member gets none; removing a source (Task 9) removes it from retrieval; a citation always has a backing snippet.

**Done criteria (acceptance proof):**
- *Ask source-grounded question with cited chunk; remove source removes retrieval* and *no citation without a source snippet; report cites stored sources.*
- `pytest tests/test_source_retrieval.py -v` green; `npx tsc --noEmit` clean.

---

## Task 11 — Connector-synced knowledge

**Matrix row:** "Connector-synced knowledge" — *Drive/SharePoint/Notion/GitHub/Slack/Gmail/Teams indexed as project/org sources; sync state, source permissions, chunk index, failure status; sync fixture connector, cite result, revoke permission removes access.*

**Files:**
- Create: `apps/api/jobs/source_sync.py` (sync a connector into `project_sources` + index via Task 9 pipeline)
- Modify: `apps/api/routers/projects.py`/`sources.py` (`POST /projects/{id}/sources/connector` to register a connector source; `POST .../sources/{sid}/sync`)
- Modify: `apps/web/app/chat/page.tsx` (project sources tab: add connector source, show sync/failure status)
- Test: `apps/api/tests/test_source_sync.py`

**Steps:**
1. `source_sync.py`: given a connector source, pull documents **through `tool_broker.execute`** (e.g. `gmail.search`, drive list/read) — never call connectors directly. For each fetched doc, create/update a `project_sources` row (source_type=`connector`, `connector_id`, `permissions` mirrored from the connector result) and run the Task 9 indexing pipeline. Record `index_status`/failure honestly (degraded-mode honesty: a failed/fallback sync is reported, never silently faked).
2. Revoke: when the connector is revoked/removed, mark its sources inaccessible and remove their chunks from retrieval (permission mirroring → revoke removes access).
3. Use a **fixture connector** for the test (mirror how `test_connector_*` tests fixture connectors) so this runs without live OAuth.
4. Frontend: add-connector-source control + sync status in the project sources tab.
5. Tests: fixture sync creates indexed sources; a query cites a synced source; revoking the connector removes those chunks from retrieval.

**Done criteria (acceptance proof):**
- *Sync fixture connector, cite result, revoke permission removes access.*
- `pytest tests/test_source_sync.py -v` green; all connector access via the broker (grep proof).

---

## Task 12 — Source viewer UI

**Matrix row:** "Source viewer" — *Inspect original, extracted text, chunks, warnings, index status; reindex/delete controls.*

**Files:**
- Modify: `apps/web/app/chat/page.tsx` (project → sources tab: list sources; click opens a viewer with original download, extracted text, chunk list, parse/index status + warnings, and reindex/delete buttons wired to Task 9/11 endpoints)
- Modify: `apps/api/routers` as needed for a `GET .../sources/{sid}` detail endpoint returning metadata + a chunk preview + the parsed-text artifact ref
- Test: `apps/api/tests/test_source_viewer.py` (the detail endpoint)

**Steps:**
1. Detail endpoint: returns source metadata, `parse_status`, `index_status`, any parser `note`/warning, chunk count + first-N chunk previews, and the original artifact id for download. Org + membership gated.
2. Frontend viewer: original (download via artifact), extracted text (from parsed_text artifact / `doc__read`), chunk list, status + warnings, reindex/delete controls.
3. Test: detail endpoint returns correct metadata and is org/membership gated.

**Done criteria (acceptance proof):**
- *Playwright source viewer and reindex/delete controls* — viewer renders real status (no fake data); reindex/delete call the real endpoints.
- `pytest tests/test_source_viewer.py -v` green; `npx tsc --noEmit` clean.

---

## Final verification (after all tasks)

1. `cd apps/api && alembic upgrade head` applies 0017→0020 cleanly on a fresh DB.
2. `cd apps/api && pytest -q` — full suite green (no regressions).
3. `cd apps/web && npx tsc --noEmit && npm run build` — web build passes.
4. `cd apps/api && ruff check . && ruff format --check .` (if configured) — clean.
5. Static seam grep: every new connector/tool call goes through `tool_broker.execute`; every memory read through `memory.retrieve`; every protected route calls `permission.check`.
6. Dispatch a final code-reviewer over the whole branch.
7. Report each matrix row's status honestly using the legend (Foundation present / Partial / Missing) with the proof command for each.

## Matrix rows covered by this plan

Phase 2: General chat assistant (rich message model), Reasoning/tool mode control, Chat controls, Conversation search, Main surfaces (shell).
Phase 3: Projects, Project instructions, Source upload, Source indexing, Connector-synced knowledge, Source viewer, plus the permission-aware retrieval + citation collector rows. (General file upload + document-intelligence foundations land via Task 8.)
