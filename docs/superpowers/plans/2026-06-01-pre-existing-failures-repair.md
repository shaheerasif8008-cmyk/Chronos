# Pre-Existing Test Failures Repair — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drive the `apps/api` test suite to green by completing the half-built branch features whose tests already exist but fail. These failures pre-date the structured-response work (proven: 0 regressions from that work).

**Architecture:** Almost every failure has an **existing failing test that IS the spec** — this is red→green TDD where the red tests are already written. Each task makes a cluster of existing tests pass by building exactly what they assert. No new test design needed except where noted.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy Core, Alembic, Pydantic v2, Postgres. Frontend untouched by this plan.

---

## CRITICAL: Test Environment

**Run all tests with the repo virtualenv at `/Users/shaheer/Downloads/Chronos-may18/.venv` (Python 3.12, all deps + alembic installed).**

```bash
# From the repo root; falls back to the conventional .venv location.
VENV=${VIRTUAL_ENV:-"$(git rev-parse --show-toplevel)/.venv"}
cd apps/api && "$VENV/bin/python" -m pytest <args>
```

- Do **NOT** use system `python3` (3.9 — crashes on `str | None` annotations in `core/permissions.py`).
- Do **NOT** use bare `python3.11` (missing installed deps: `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `python-multipart`, `aiosqlite` — all are in `requirements.txt` but not installed in that interpreter). Using the wrong interpreter produces ~17 phantom `ModuleNotFoundError` failures that don't exist in `.venv`.
- alembic: `$VENV/bin/alembic` from `apps/api`.

**Baseline (measured in `.venv`):** 48 failed, 241 passed, 1 skipped. Target: 0 failed (modulo genuinely environment-only tests — see Task F). Confirm no regressions after each task with the full suite.

---

## Failure Inventory (measured in `.venv`)

| Cluster | Count | Root cause | Task |
|---|---|---|---|
| `RequesterContext` has no `project_id` | 12 | model field + plumbing missing | A |
| `artifacts.artifact_key` NOT NULL violated | 10 | insert path doesn't populate `artifact_key` | B |
| `routers.chat` missing message-control fns | 14 | unbuilt feature (pin/unpin/edit/branch/save-to-memory/convert-to-task) | C |
| `routers.chat` has no `stream_chat_turn` | 5 | chat router not wired to the inline-turn fn | D |
| Rich `_save_message` kwargs + `_normalize_traces` | 6 | unbuilt rich persistence | E |
| `_create_conversation(project_id=)` | 1 | param missing | A |
| misc (`doc_parsing` 1, `FakeReq.attachment_ids` 1, event-loop-closed flake) | ~3 | individual | F |

Total ≈ 48. Counts overlap slightly where one test hits two causes; the canonical acceptance is "the named test file passes."

---

### Task A: `project_id` on RequesterContext + conversation plumbing

**Fixes:** `test_project_instructions.py` (9), `test_llm_and_memory.py` (2), `test_source_retrieval.py` (5), `test_project_sources.py` (3), `test_projects.py` (1). ~20 tests (some also need Task D; re-measure).

**Files:**
- Modify: `apps/api/core/models.py` (`RequesterContext`)
- Modify: `apps/api/routers/chat.py` (`_create_conversation`, `send_message` hydration)
- Read first: `apps/api/tests/test_project_instructions.py`, `apps/api/core/context.py` (uses of `requester_context.project_id`)

- [ ] **Step 1: Read the specs.** Read `test_project_instructions.py` and grep the codebase for `project_id` usages on requester context / conversations: `grep -rn "project_id" apps/api/core apps/api/routers | grep -i "context\|conversation"`. Note exactly where `requester_context.project_id` and `_create_conversation(..., project_id=...)` are expected.

- [ ] **Step 2: Run the red tests.**
  Run: `$VENV/bin/python -m pytest tests/test_project_instructions.py tests/test_llm_and_memory.py -q`
  Expected: failures `AttributeError: 'RequesterContext' object has no attribute 'project_id'`.

- [ ] **Step 3: Add the field.** In `core/models.py`, add to `RequesterContext` (after `task_id`):
  ```python
      project_id: str | None = None
  ```
  Confirm `from_member` doesn't need it (defaults to None).

- [ ] **Step 4: Plumb `project_id` in chat.** In `routers/chat.py`:
  - Add `project_id: str | None = None` to `ChatRequest`.
  - Set `requester_context.project_id = req.project_id` where the other context fields are set.
  - Add a `project_id: str | None = None` param to `_create_conversation` and persist it into the `conversations` insert (the `conversations` table has a `project_id` column from migration `0017_messages_rich`). Match the existing hydration logic the tests assert (read `test_project_instructions.py` for the exact 422-on-mismatch and hydrate-from-existing-conversation behavior).

- [ ] **Step 5: Green.**
  Run: `$VENV/bin/python -m pytest tests/test_project_instructions.py tests/test_llm_and_memory.py tests/test_source_retrieval.py tests/test_project_sources.py tests/test_projects.py -q`
  Expected: PASS (any still-red are Task D `stream_chat_turn` — note which).

- [ ] **Step 6: Commit** `feat(context): add project_id to RequesterContext and conversation creation`.

---

### Task B: Populate `artifacts.artifact_key` on insert

**Fixes:** `test_artifact_workspace.py` (10) — `NotNullViolationError: null value in column "artifact_key"`.

**Files:**
- Read first: the migration that added `artifact_key` (`grep -rln artifact_key apps/api/migrations`), and `apps/api/core/artifacts.py` (`save_artifact` / insert path).
- Modify: `apps/api/core/artifacts.py` (and/or wherever artifacts rows are inserted).

- [ ] **Step 1: Understand `artifact_key`.** `grep -rn "artifact_key" apps/api` — determine its intended value (likely a stable storage key / slug / uuid used for MinIO path or dedupe). Read the migration's column definition (NOT NULL, any default/unique) and how reads use it.

- [ ] **Step 2: Run red tests.** `$VENV/bin/python -m pytest tests/test_artifact_workspace.py -q` → IntegrityError on `artifact_key`.

- [ ] **Step 3: Populate it on insert.** In the artifact insert path, set `artifact_key` to the intended value (read `test_artifact_workspace.py` to see whether it asserts a specific shape; if not, a `uuid4().hex` or a derived stable key is acceptable — match any uniqueness constraint). Keep it consistent across `save_artifact` and any version/share inserts that also require it.

- [ ] **Step 4: Green.** `$VENV/bin/python -m pytest tests/test_artifact_workspace.py -q` → PASS.

- [ ] **Step 5: Commit** `fix(artifacts): populate required artifact_key on insert`.

---

### Task C: Chat message controls (pin / unpin / edit / branch / save-to-memory / convert-to-task)

**Fixes:** `test_chat_controls.py` (14).

**Contract (from `test_chat_controls.py` — these are module-level async functions in `routers/chat.py`, called directly, plus FastAPI routes):**
- `pin_message(conversation_id, message_id, member)` → toggles `messages.pinned=True`; 404 if conversation not owned by member's org/owner.
- `unpin_message(conversation_id, message_id, member)` → sets `pinned=False`.
- `edit_message(conversation_id, message_id, req, member)` where `req` has the new content → updates content for a **user** message; returns 400 for an **assistant** message; 404 wrong owner.
- `branch_conversation(conversation_id, message_id, member)` → creates a new conversation with lineage (copies messages up to `message_id`); uses a SQLAlchemy `or_(...)` predicate (import `or_`); 404 wrong owner.
- `save_message_to_memory(conversation_id, message_id, req, member)` → calls `create_memory_entry(...)`; `personal` scope uses `member_id` as `scope_id`; 404 wrong owner.
- `convert_message_to_task(conversation_id, message_id, req, member)` → calls `create_task_record(...)`; 404 wrong org. Note `create_task_record` must be importable as `chat.create_task_record` (it currently lives in `routers/tasks.py` and is imported locally inside a function — hoist the import to module scope so tests can monkeypatch `chat.create_task_record`).

**Files:**
- Read first (the spec): `apps/api/tests/test_chat_controls.py` in full — each test fixes the exact monkeypatch surface, return shape, and status codes.
- Modify: `apps/api/routers/chat.py` (add the 6 functions + FastAPI route decorators + `from sqlalchemy import or_` + module-level `from routers.tasks import create_task_record`).

- [ ] **Step 1: Read `test_chat_controls.py` fully.** For each function note: arguments, the DB rows the fakes set up, the expected return dict/object, and the exact HTTPException status codes (404/400).

- [ ] **Step 2: Run red.** `$VENV/bin/python -m pytest tests/test_chat_controls.py -q` → AttributeErrors for each missing function.

- [ ] **Step 3: Implement** the 6 functions in `routers/chat.py`, each with `await permissions.check(...)` and `await audit.log(...)` consistent with the existing handlers, plus the FastAPI routes (e.g. `@router.post("/conversations/{conversation_id}/messages/{message_id}/pin")`). Define the small Pydantic request bodies (`EditReq` content; `SaveMemReq` scope; `ConvertReq` goal) matching the field names the tests construct. Hoist `create_task_record` and add `or_` import.

- [ ] **Step 4: Green** in chunks (pin/unpin, then edit, then branch, then save-memory, then convert): `$VENV/bin/python -m pytest tests/test_chat_controls.py -q` → all 14 PASS.

- [ ] **Step 5: Commit** `feat(chat): message controls — pin, edit, branch, save-to-memory, convert-to-task`.

> Frontend note: `MessageActionMenu` in `apps/web/app/chat/page.tsx` already calls some of these endpoints (it has onRefresh/onBranch). After the backend lands, verify the menu's fetch paths match the new route URLs; adjust if needed (separate small step, `npx tsc --noEmit` to gate).

---

### Task D: Wire `stream_chat_turn` into `routers.chat`

**Fixes:** `test_chat_modes.py` (1) + the `test_project_instructions.py` cases asserting `chat.stream_chat_turn`.

**Background:** `runtime/agent_loop.py` defines `stream_chat_turn` (the in-process inline-turn streamer). The live chat router currently uses `_agent_loop_stream` (task-runner based). Tests monkeypatch `chat.stream_chat_turn`, so the router must reference it as a module attribute and use it on the appropriate path.

**Files:**
- Read first: `apps/api/tests/test_chat_modes.py` (~line 245) and the `stream_chat_turn` cases in `test_project_instructions.py` — determine exactly which path must call `stream_chat_turn` and with what arguments.
- Modify: `apps/api/routers/chat.py`.

- [ ] **Step 1: Read the specs** to learn the expected call (args, when it's used vs `_agent_loop_stream`). Decide minimally: import `stream_chat_turn` at module scope (`from runtime.agent_loop import stream_chat_turn`) and route the inline (non-task) path through it as the tests expect — without breaking the structured-response fast-path envelope (Task 7 of the prior plan).
- [ ] **Step 2: Run red.** `$VENV/bin/python -m pytest tests/test_chat_modes.py -q`.
- [ ] **Step 3: Implement** the minimal wiring so `chat.stream_chat_turn` exists and is used on the path the tests assert. Preserve the structured-response emission.
- [ ] **Step 4: Green** + regression: `$VENV/bin/python -m pytest tests/test_chat_modes.py tests/test_project_instructions.py tests/test_chat_turn.py tests/test_structured_response.py -q`.
- [ ] **Step 5: Commit** `feat(chat): route inline turns through stream_chat_turn`.

> ⚠️ This task interacts with the structured-response fast path. If wiring `stream_chat_turn` changes which path runs for ordinary chat, ensure the `direct_answer` envelope is still produced and persisted (re-run `test_structured_response.py`). If they genuinely conflict, STOP and escalate — the two designs may need reconciliation.

---

### Task E: Rich `_save_message` kwargs + `_normalize_traces`

**Fixes:** `test_rich_messages.py` (6).

**Contract (from `test_rich_messages.py`):**
- `_save_message(...)` must accept keyword-only metadata: `model`, `mode`, `citations`, `tool_traces`, `memory_refs`, `artifact_refs`, `approval_state`, `runtime_status`, `parent_message_id`, `pinned` (columns exist from migration `0017_messages_rich`) and include them in the INSERT. (It already accepts `structured_response` from the prior plan — keep that.)
- `_normalize_traces(...)` helper: read the test for the exact input (raw trace events) → output (merged tool_call+tool_result entries; thinking entries skipped; required fields present).

**Files:**
- Read first: `apps/api/tests/test_rich_messages.py` (all 6 tests).
- Modify: `apps/api/routers/chat.py`.

- [ ] **Step 1: Read `test_rich_messages.py`** — extract the exact `_save_message` kwarg list and the `_normalize_traces` input/output contract.
- [ ] **Step 2: Run red.** `$VENV/bin/python -m pytest tests/test_rich_messages.py -q`.
- [ ] **Step 3: Implement.** Extend `_save_message` with the keyword-only params (all defaulting to None/[]/False), adding each to `.values(...)`. Add `_normalize_traces` per the test contract.
- [ ] **Step 4: Green** + regression: `$VENV/bin/python -m pytest tests/test_rich_messages.py tests/test_structured_response.py -q`.
- [ ] **Step 5: Commit** `feat(chat): rich message persistence kwargs and trace normalization`.

---

### Task F: Miscellaneous stragglers

**Fixes:** remaining individual failures after A–E. Re-measure first — several will already be green.

- [ ] **Step 1: Re-measure.** `$VENV/bin/python -m pytest -q --tb=line` → list whatever still fails.
- [ ] **Step 2: Triage each remaining failure** individually:
  - `test_doc_parsing.py` (1 remaining): read the test; likely an OCR/`pdfium`/scanned-doc edge. If it needs an uninstalled OCR engine, mark as environment-only and document; otherwise fix.
  - `FakeReq object has no attribute 'attachment_ids'`: a test-local fake missing a field the (now-changed) `send_message` reads — update the test's fake to include `attachment_ids = []` (test-only fix).
  - `RuntimeError: Event loop is closed`: teardown flake from `redis.asyncio` connection `__del__`; if it's only a teardown warning (test still passes), ignore; if it fails a test, ensure the redis client is closed in the relevant fixture/finalizer.
- [ ] **Step 3: Fix or explicitly document** each as environment-only (with reason).
- [ ] **Step 4: Commit** `fix(tests): resolve remaining stragglers`.

---

### Task G: Full green + governance

- [ ] **Step 1: Full suite.** `cd apps/api && $VENV/bin/python -m pytest -q` → 0 failed (or only documented environment-only skips). Compare against the 48-failure baseline.
- [ ] **Step 2: Lint.** `$VENV/bin/python -m ruff check core/ routers/ tests/` → clean for touched files.
- [ ] **Step 3: Frontend** (if Task C frontend note touched `page.tsx`): `cd apps/web && npx tsc --noEmit`.
- [ ] **Step 4: Commit** any fixups.

---

## Self-Review

- **Coverage:** every failing test file in the inventory maps to a task (A: project_id files; B: artifact_workspace; C: chat_controls; D: chat_modes + project_instructions remainder; E: rich_messages; F: stragglers). ✅
- **Specs exist:** all acceptance is "named existing test passes" — no invented behavior. Tasks that need exact signatures explicitly say "read the test first." ✅
- **Env hazard:** the `.venv` requirement is stated up front and repeated in every run command — this was the single biggest time-sink during triage. ✅
- **Interaction risk:** Task D is flagged as potentially conflicting with the structured-response fast path, with an escalation instruction. ✅
- **Ordering:** quick high-yield field/dep fixes (A, B) before feature builds (C, E) before risky wiring (D); stragglers (F) last after re-measuring. ✅

## Execution Handoff

After approval: Subagent-Driven (fresh subagent per task A–G, two-stage review, all tests via `.venv`) or Inline. Recommend Subagent-Driven, same as the structured-response plan.
