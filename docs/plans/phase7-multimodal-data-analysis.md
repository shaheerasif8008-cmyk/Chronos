# Phase 7 — Multimodal and Data Analysis (Implementation Plan)

Maps to `CHRONOS_TOTAL_PARITY_GOAL.md` §7 and the matrix section **"Multimodal, Files, and Data"**
in `docs/chronos_total_parity_matrix.md` (7 rows). This plan is the controlling work order; each
task targets the **acceptance-proof delta** for one matrix row (not a from-scratch rebuild — much
foundation already exists).

## Global constraints (apply to EVERY task — restate in every subagent spec)

- **RULE 1 (broker):** image-gen, image-edit, TTS, STT, and data-execution are tool calls — they
  MUST route through `core/tool_broker.execute` (new providers added to `_route` in
  `apps/api/core/tool_broker.py`, like `doc`/`code`/`fs`). NEVER call a provider/library inline
  from a router. Every call is permission-checked + audit-logged by the broker.
- **RULE 2 (memory):** any memory read goes through `core/memory.retrieve`.
- **RULE 3 (permissions):** any action check goes through `core/permissions.check`.
- **RULES 4/5 (tenancy):** every new table has `organization_id UUID NOT NULL` + `region TEXT NOT
  NULL DEFAULT 'us'`. Every new route is tenant-scoped: cross-org access returns 404 (pattern:
  `tests/test_tenant_isolation_http.py`, `test_artifact_workspace.py::test_router_blocks_cross_org_access`).
- **RULE 6/7 (audit/secrets):** durable significant events go to append-only `audit_log`; never log
  credentials/keys, only refs.
- **Durable artifacts:** every reusable output (generated image, transcript, TTS audio, chart,
  report) becomes an `artifacts` row via `core/artifacts.save_artifact(...)` (version-addressed
  storage with local fallback) and survives refresh.
- **HONEST DEGRADED MODE (user-confirmed completion bar):** where no live provider key exists, the
  live leaf returns a truthful, explicit `degraded`/`unavailable` result (NOT a mock presented as
  live) — exactly like `core/llm.vision_ocr` returning `""` when `settings.vision_model` is empty,
  and like fixture connectors. The provider abstraction is real; only the leaf degrades. Result
  metadata must carry `provider`, `tier`, and a `fallback_reason` when degraded. UI must render the
  degraded state visibly. This satisfies matrix rule 5 ("no scaffolding/mocks/UI-only as complete")
  because the pipeline is real and the degradation is honest and tested.
- **Available providers in this env:** `OPENROUTER_API_KEY` is set (can power **live** vision/image
  *input* via a vision-capable model). No dedicated image-generation, TTS, or STT keys → those rows
  ship full pipeline + honest degraded leaf. Add new optional settings to `core/config.py`
  (`image_model: str = ""`, `tts_model/voice: str = ""`, `stt_model: str = ""`) defaulting to `""`.

## Proof conventions (every task)

- Backend tests live in `apps/api/tests/test_<row>.py`, named for the acceptance proof, run against
  the **isolated test DB**:
  ```
  cd apps/api && DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:55433/chronos" \
    REDIS_URL="redis://localhost:6379/9" .venv/bin/python -m pytest tests/test_<row>.py -x -q
  ```
- Degraded-provider behavior is proven by injecting a stub provider (monkeypatch the provider
  function / setting) — assert both the live-shape path and the degraded path.
- Frontend changes must pass `cd apps/web && npm run build` (typecheck). Add a Playwright E2E in
  `apps/web/e2e/` where a deterministic flow exists (follow existing specs; isolated dual-server +
  dev-OTP auth).
- New migrations go in `apps/api/migrations/versions/00NN_<name>.py` with a unique revision id and
  correct `down_revision` chained off the current head (`0025_task_dead_letter`; note two `0024_*`
  exist — verify the actual head with `alembic heads` before writing). Apply to the test DB before
  running tests.
- Update the matrix row to "Implemented (Phase 7)" with the proof reference ONLY after proof exists.

---

## Task 1 — General file upload (matrix: "General file upload")

**Current state:** `routers/attachments.py` uploads → `save_artifact(kind="attachment")`, links to
conversation + optional project source, parses on first use (`routers/chat.py::_parse_attachments`).
Chat composer (`apps/web/app/chat/page.tsx`) already uploads, previews images, sends
`attachment_ids`.

**Gap / scope (delta only):**
1. Attachment chips must **persist on messages across refresh** — verify `messages.artifact_ids`
   round-trips: user message records the uploaded `attachment_ids`, and chat history GET returns
   them so the composer/message renderer shows the attached files after reload. If not wired, wire it.
2. Allow attaching to **task and research** contexts (not just conversation/project): accept
   `task_id` / `research_run_id` form fields, link the artifact, enforce membership/permission, audit.
3. Honest size/type errors already exist (413). Surface upload failure to the user (currently chat
   page fails silently — add a visible error state).

**Interface/persistence:** uploaded file record (artifact), object path, linked entity id; message
`artifact_ids` populated and returned by history.

**Acceptance proof:** `tests/test_file_upload.py` — upload file with conversation → send message →
read history back → attachment id present on the message (survives "refresh" = re-fetch). Upload with
`task_id` links + audits. Cross-org upload/link → 404. Web build passes; extend an E2E (or add
`file-upload.spec.ts`) asserting an attached file chip reappears after reload.

---

## Task 2 — Document intelligence (matrix: "Document intelligence")

**Current state:** `parsing/engine.py` parses PDF/DOCX/XLSX/PPTX/CSV/text/image-OCR →
`ParsedDocument`; agent tools `doc.parse`/`doc.read` via `parsing/tool.py` through broker;
`test_doc_parsing.py` passes (26 tests).

**Gap / scope:** Product-level **summarize / compare / extract with citations** + parser-warning
surfacing.
1. Add broker-routed doc operations for **summarize** and **compare** (e.g. `doc.summarize`,
   `doc.compare`) that run the parsed text through `core/llm` and return structured output with
   **citations** (source artifact id + page/char offsets) — no claim without a stored source span.
2. Surface `ParsedDocument.note` / `parser_used` / `truncated` as visible **parser warnings** in the
   attachment/source UI and in tool results (degraded honesty for unparseable types).
3. Per-file-class coverage: prove summarize/compare/extract for PDF, DOCX, XLSX, CSV (reuse fixtures
   from `test_doc_parsing.py`).

**Interface/persistence:** extracted text/tables + parser warnings already on artifacts; summaries
become artifacts or message metadata with citation refs.

**Acceptance proof:** `tests/test_document_intelligence.py` — per file class: parse → summarize/extract
returns content with at least one citation tied to a stored source span; unparseable type returns an
honest warning (no fabricated content). All via broker (assert audit). Web build passes.

---

## Task 3 — Image input / vision (matrix: "Image input")

**Current state:** images are run through **OCR only** (`vision_ocr` → text) and fed as text seed
context. True vision reasoning (passing the actual image to a multimodal model) does NOT happen in a
chat turn.

**Gap / scope:** True multimodal chat — when a message has image attachments and the selected model
is vision-capable, assemble `image_url` content blocks (data URL or stored ref) into the chat turn
so the model reasons over the **image itself** (screenshots, diagrams, charts, UI inspection), not
just OCR text. Keep OCR as a fallback/augmentation.
1. Add a vision-capable model to the model registry / `settings.vision_model` wiring; mark vision
   capability in `available_chat_models()` metadata.
2. In `core/context.py` / `routers/chat.py`, when image attachments + vision-capable model, build the
   multimodal `content` array (text + `image_url`) for the user turn. When the model/key is absent →
   honest degraded: fall back to OCR text with a visible "vision unavailable, used OCR" note.
3. Persist that the message had image input; renderer shows image thumbnails on the message.

**Interface/persistence:** image attachment + extracted metadata; message records image refs.

**Acceptance proof:** `tests/test_image_input.py` — with a stub vision model, a message with an image
produces a turn whose content array contains an `image_url` block (assert assembly); with no vision
model, falls back to OCR with an honest note. Tenant-scoped. Web build passes; E2E or static check
that an image attachment renders on the sent message and persists after reload.

---

## Task 4 — Image generation (matrix: "Image generation")

**Current state:** Missing.

**Gap / scope:** New broker-routed tool `image.generate` with style/size/count controls →
**provider abstraction** in a new connector (`apps/api/connectors/image_gen.py` or `core/image.py`)
→ output saved as image **artifact(s)** with generation metadata. Composer "Image" mode already
exists in chat UI (`page.tsx` mode id `"image"`) — wire it to invoke generation and render the
resulting image artifact.
1. New tool routed via `_route` (provider `image`), permission + audit + safety (count cap) through
   broker. Honest degraded leaf when `settings.image_model` empty: returns
   `{status:"unavailable", fallback_reason:"no image provider configured"}` (no fake image).
2. Generated image → `save_artifact(kind="image", mime_type=...)` with prompt/size/count metadata.
3. UI: Image mode generates → renders image artifact in chat + artifact workspace; degraded state
   shows a truthful "image generation unavailable" message.

**Interface/persistence:** image artifact + generation metadata (prompt, size, count, provider, tier).

**Acceptance proof:** `tests/test_image_generation.py` — stub provider returns bytes →
`image.generate` through broker creates an image artifact reopenable after refetch; count cap
enforced; no-provider path returns honest unavailable (no artifact, audited). Cross-org 404. Web
build passes.

---

## Task 5 — Image editing (matrix: "Image editing")

**Current state:** Missing. Depends on Task 4's provider abstraction.

**Gap / scope:** New broker-routed tool `image.edit` (edit/variation/mask/background) reusing Task 4's
provider → output saved as a **new artifact version** of the source image (non-destructive, like the
artifact-versioning pattern in Phase 5).
1. `image.edit` through broker with source image artifact id + edit params; honest degraded leaf when
   no provider.
2. Output written as a new version via `core/artifact_versions` (source image + edit params recorded).

**Interface/persistence:** source image, edit params, output artifact/version.

**Acceptance proof:** `tests/test_image_editing.py` — stub provider: edit creates a new image version
without clobbering the source (assert version lineage); no-provider → honest unavailable. Cross-org
404. Web build passes.

---

## Task 6 — Voice input/output (matrix: "Voice input/output")

**Current state:** Missing.

**Gap / scope:** STT + TTS + transcript persistence + audio artifacts, full pipeline with honest
degraded leaves (no STT/TTS key in env).
1. Broker-routed `voice.transcribe` (STT): audio artifact in → transcript text out → transcript saved
   (message and/or artifact). Honest degraded when `settings.stt_model` empty.
2. Broker-routed `voice.speak` (TTS): text in → audio artifact out with TTS metadata. Honest degraded
   when `settings.tts_model`/voice empty.
3. Chat UI: voice input control (record/upload audio → transcribe → transcript becomes the message);
   TTS playback control on assistant messages; both show truthful "voice unavailable" when degraded.

**Interface/persistence:** audio attachment, transcript message, TTS audio artifact + metadata.

**Acceptance proof:** `tests/test_voice.py` — stub STT: audio → transcript persisted; stub TTS: text →
audio artifact persisted with metadata; no-provider paths return honest unavailable (audited).
Tenant-scoped. Web build passes.

---

## Task 7 — Data analysis workspace (matrix: "Data analysis workspace") — LARGEST, LAST

**Current state:** `connectors/code.py` (`code.python`) exists but its sandbox **regex blocks
imports** and **pandas/matplotlib are NOT in `requirements.txt`**. No user-facing data workspace.

**Gap / scope:** A Python-backed data analysis path over uploaded CSV/XLSX/JSON producing tables,
charts, and reports as artifacts, with a UI.
1. **Dependency + sandbox decision:** add `pandas`, `matplotlib` (and `numpy`) to
   `apps/api/requirements.txt`; install into `apps/api/.venv`. Provide a data-exec path that **safely
   permits these libs** while keeping the existing network/subprocess/fs blocks — either relax
   `code.python`'s import allowlist for a curated data set, or add a dedicated broker-routed
   `data.run` tool with a data-analysis-scoped sandbox. Keep matplotlib in a non-interactive backend
   (`Agg`); capture produced figures as image artifacts and stdout/tables as report/CSV artifacts.
2. **Dataset record:** new `datasets` table (`organization_id`, `region`, source artifact id, parsed
   schema/columns, row count, status) — tenant-scoped CRUD + route.
3. **Charts/reports → artifacts:** generated charts saved as image artifacts; tabular results as
   CSV/table artifacts; a summary report as a markdown/document artifact. All via `save_artifact`.
4. **UI:** a Data workspace surface (new `apps/web/app/data/` screen or a data panel in chat/artifacts)
   — upload CSV → see schema → run analysis → view generated chart/report artifacts. No fake data.

**Interface/persistence:** dataset record, generated charts/reports as durable artifacts.

**Acceptance proof:** `tests/test_data_analysis.py` — upload CSV → dataset record with parsed schema;
run analysis through the broker-routed exec path → produces a chart image artifact and a report
artifact, both reopenable after refetch; sandbox still blocks network/subprocess (assert a blocked
import raises). Cross-org 404. `requirements.txt` updated and importable. Web build passes; E2E or
static route check for the data workspace.

---

## Execution order (serial, one subagent per task, two-stage review each)

1 → 2 → 3 → 4 → 5 → 6 → 7. (Upload/parse first; vision next; shared image provider for gen→edit;
voice; data workspace last because of the dependency + sandbox change.)

## Done = all 7 matrix rows updated to "Implemented (Phase 7)" with proof refs, backend tests green
against the isolated DB, `apps/web` build passes, new E2E specs pass where added, and a final
cross-cutting review confirms broker/tenant/audit/degraded-honesty rules hold across all rows.
