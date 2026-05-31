# Chronos Total Parity Matrix

## Purpose

This matrix is the controlling implementation contract for the Chronos Total Parity Program. It translates the north-star goal in `CHRONOS_TOTAL_PARITY_GOAL.md` into concrete, testable capability rows.

No parity claim is valid unless the matching row is marked implemented with proof. A row is complete only when it works through the real UI/API, persists durable state where applicable, respects tenant boundaries, emits audit evidence where applicable, survives refresh/restart where applicable, and has automated proof.

## Status Legend

- **Foundation present**: meaningful repo substrate exists, but the full product capability is not complete.
- **Partial**: user-facing or backend behavior exists for a subset of the target.
- **Missing**: no complete implementation found in the current checkout.
- **Planned**: deliberately listed as required parity work; no completion claim.

## Current Foundation Snapshot

Chronos already has foundations for chat, tool use, governed execution, scoped memory, browser search/fetch, Gmail drafting/search, filesystem/code tools, artifacts, approvals, connector framework, activity streams, settings, and task orchestration. These foundations are not enough for total parity. The rows below define the remaining product contract.

## Matrix Columns

- **Target parity**: what Chronos must provide.
- **Current state**: current repo/product state, without overstating readiness.
- **Implementation area**: subsystem that owns the work.
- **Interface and persistence**: public UX/API/state required.
- **Acceptance proof**: minimum proof before marking complete.

## Core Assistant and Chat

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| General chat assistant | ChatGPT/Claude-grade conversational assistant with streaming, history, model selection, citations, artifacts, memory refs, and tool traces. | E2E-proven for the core loop: send → live model stream → durable persistence (read back via API) → reply visible in UI. Proof: apps/web/e2e/chat.spec.ts (passes against real DeepSeek). Richer metadata persistence (citations, tool traces, memory refs) remains partial. | Chat UI, chat router, LLM layer, message model. | Conversation/message records include model, mode, citations, tool traces, memory refs, artifacts, status. | API and Playwright test: send message, stream answer, refresh, history and metadata persist. **Core loop done** via apps/web/e2e/chat.spec.ts. |
| Model selection | User can choose models/modes by task with safe fallback and clear provider status. | Partial: chat models endpoint and selected model storage exist; selection now E2E-proven to persist across reload (apps/web/e2e/model-selection.spec.ts). Capability/provider-status registry still minimal. | LLM config, chat composer, settings. | Model registry with label, provider, capabilities, status, policy constraints. | Test model selection persists across a tool task and fallback is visible. **Persistence proven** via apps/web/e2e/model-selection.spec.ts. |
| Reasoning and tool mode control | User can select default, research, agent, browser, computer, data, image, voice, or coding modes. | Partial: native agent loop and simple chat/tool routing exist; productized mode selector incomplete. | Chat composer, router, runtime planner. | Message/task mode persisted and shown in UI. | Playwright: each mode creates expected task/message type and status. |
| Chat controls | Edit prompt, regenerate, branch, retry from here, pin, copy, export, save to memory, convert to task/workflow. | Partial: operational UI scaffolding exists from prior work, completeness must be verified. | Chat UI, message actions, task/workflow APIs. | Message action endpoints and branch/fork lineage. | Playwright: edit/regenerate/branch/save-memory/convert-task survive refresh. |
| Conversation search | Search across conversations, tasks, artifacts, memory, and sources. | Missing as unified product feature. | Search service, UI global search. | Search index or queries over persisted entities with permission filters. | Search returns only authorized results with type filters. |

## Runtime, Agent, and Reliability

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Native tool loop | Model-native decide-act-observe loop with parallel tool calls and honest failure handling. | Foundation present in runtime agent loop. | Runtime agent loop, tool registry, broker. | Persisted agent history, iteration count, tool calls/results/errors. | Unit/integration: multi-tool call, tool error, replan/continue, final answer. |
| DAG execution | Complex task DAG with dependencies, conditions, parallel execution, checkpoints, and replanning. | Foundation present in executor/planner. | Runtime executor, planner, task state. | Task plan, completed/skipped steps, result context, checkpoint records. | Tests cover conditional skip, parallel step, approval gate, resume, replan. |
| Task queue | Priority queue, per-org/user/tool concurrency, worker limits, dead-letter state. | Phase 1 foundation implemented: queued task status, priority runner, retry/timeout policy, startup re-enqueue, UI queued state. Broader distributed worker leases remain later scaling work. | Runtime task runner, Redis/Postgres queue. | Queue records, priority, leases, attempts, worker heartbeat. | Integration: priority ordering, concurrency cap, worker crash recovery. |
| Cancellation | User can cancel running tasks and browser/computer/tool work stops promptly. | Phase 1 foundation implemented and tested: queued-task cancellation + native-loop stop-on-cancel (test_runtime_reliability_phase1.py); HTTP cancel endpoint + tenant scoping + already-terminal no-op (test_task_cancel_http.py). Tool-adapter interruption broadening remains later work. | Runtime runner, tool adapters, UI controls. | Cancellation token/status persisted; trace records cancellation. | Test: cancel long browser/code task and verify no further steps execute. **Queued/loop/HTTP cancel proven.** |
| Timeouts and retries | Step-level and task-level timeouts, retry policy, final failure taxonomy. | Phase 1 foundation implemented in task runner with settings-backed max attempts and timeout failure reason; connector-specific policies continue under connector framework. | Runtime runner, tool broker, settings. | Per-task policy, retry count, timeout reason. | Tests for timeout, retry then success, retry exhaustion. |
| Crash recovery | Native loop, DAG, approvals, connector jobs, browser/computer sessions recover after restart. | Phase 1 foundation implemented: startup re-enqueues queued/pending/planning/running tasks, approval decisions enqueue resume exactly once through runner, connector workflow recovery remains in startup. Browser/computer session recovery becomes concrete when those session managers ship. | Startup recovery, task runner, session managers. | Durable task/session state sufficient for resume or safe fail. | Restart proof: task pauses, API restarts, resumes exactly once. |
| Durable trace replay | Full task timeline replayable after refresh/restart. | Phase 1 foundation implemented and now E2E-proven: replay-significant activity events persist to append-only audit; high-frequency heartbeat remains Redis-only. Tool calls/results, approvals, artifacts, sub-agents, task completion/failure/cancel, route/model steps are durable. Proof: apps/web/e2e/activity.spec.ts (create task → complete → open Activity timeline → refresh twice → persisted event count matches). | Trace persistence, activity API, UI timeline. | Trace table/events for model, tools, screenshots, artifacts, approvals, memory, safety. | Playwright: refresh task page and timeline matches live run. **Done** via apps/web/e2e/activity.spec.ts. |
| Idempotent writes | External write actions execute at most once across retries/recovery. | Phase 1 foundation implemented at broker seam: write tools accept idempotency keys, replay cached provider responses, and audit redacted idempotency evidence. Provider-native idempotency expansion remains per connector. | Tool broker, connector adapters. | Idempotency key per write action and provider response reference. | Test restart/retry around approved write does not duplicate draft/send/post. |

## Governance, Safety, and Audit

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Permission seam | Every action uses `permission.check`; future policy engine can replace implementation without call-site changes. | Foundation present. | Permissions core, routers, runtime. | Audit of permission decisions and tenant/resource context. | Static grep plus tests for protected routes/tools. |
| Tool broker seam | Every external action routes through `tool_broker.execute`. | Foundation present. | Broker, tool registry, connectors. | Tool call/result audit with redacted args. | Static grep plus tool execution tests. |
| Approval system | Risky writes pause, route to authorized approver, resume exactly once, reject cleanly. | Proven deterministically: real broker gate (gmail.draft → ApprovalRequired via gmail provider policy) + HTTP decide→approved→resume in apps/api/tests/test_approval_flow_http.py; real inbox→Approve UI flow in apps/web/e2e/approvals.spec.ts (seeded). NOTE: "unauthorized cannot approve" is NOT yet proven — permission.check only enforces with OpenFGA, which is off in this env. The live agent-mode trigger is non-deterministic (model-dependent) so it is not used in CI. | Approvals router/UI, broker policy. | Approval records, payload, status, actor, resume result. | E2E: unauthorized cannot approve; authorized approval resumes task once. **Authorized approve→resume done; unauthorized-block pending OpenFGA.** |
| Audit log | Searchable/exportable audit for tool, memory, approval, policy, connector, task, and admin events. | Foundation present; searchable/exportable audit product incomplete. | Audit service, admin UI. | Append-only audit with redaction and export endpoints. | Test export includes expected chain and no secrets. |
| Prompt injection defense | Untrusted browser/file/email/connector content cannot issue instructions or trigger actions. | Phase 1 foundation implemented: browser and read-like connector outputs are marked untrusted, prompt-injection markers are surfaced, native loop gates writes after injected content, and broker rejects runtime-flagged untrusted-triggered writes without approval. Broader file/email/OCR ingestion wrapping expands as those ingestion surfaces ship. | Content ingestion, LLM context assembler, broker policy. | Untrusted content wrappers, scan result, action grounding record. | Injection fixture cannot cause email/send/post/delete or policy bypass. |
| Tenant isolation | Every API/query/tool/artifact/memory path enforces organization and permission scope. | Proven at multiple layers: search org-isolation (test_search.py), in-process artifact cross-org (test_artifact_workspace.py), and now an HTTP-boundary cross-org 404 over the real ASGI app with real auth/tokens (test_tenant_isolation_http.py). Broader per-resource HTTP coverage (tasks/connectors/projects) still expanding. | DB queries, routers, broker, storage. | Org/region on every persisted row and storage path. | Cross-tenant tests for memory, artifacts, tasks, connectors, projects. **Artifacts proven over HTTP** via test_tenant_isolation_http.py. |
| Secret handling | Credentials encrypted, never logged, revocable, and redacted in traces. | Foundation present with vault concepts; full coverage needed. | Vault, connectors, audit redaction. | Vault refs only in logs; rotation/revoke state. | Redaction test suite and connector revoke proof. |
| Degraded-mode honesty | Fixture/demo/fallback data is never presented as live. | Foundation present in browser warning behavior; must cover all connectors. | Connector framework, UI status. | Result metadata includes tier/provider/fallback reason. | Tests assert fallback warning shown and answer reports limitation. |

## Projects and Knowledge Sources

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Projects | Claude/ChatGPT-style projects with instructions, members, conversations, sources, memory, tasks, artifacts, and tools. | Missing as first-class product surface. | Projects API/UI, DB schema, context assembly. | `projects`, memberships, instructions, settings, linked entities. | E2E create project, add member/source/chat/artifact, enforce access. |
| Project instructions | Project-level instruction layer merged safely with system/user context. | Partial via context folder and persona/workspace fields; no project product. | Context assembler, projects. | Instruction version and audit of edits. | Test project instruction affects answers only inside project. |
| Source upload | Upload PDFs, docs, slides, sheets, text, code, images, URLs. | Missing as general source upload workflow. | Upload service, storage, parser pipeline. | Source record, object path, parse status, extracted text, chunks. | Upload PDF/CSV/code folder and see indexed status. |
| Source indexing | Chunk, embed, refresh, remove, reindex, cite, permission-resync sources. | Partial memory embeddings exist; project source indexing missing. | Ingestion pipeline, embeddings, retrieval. | Source chunks, embeddings, permissions, freshness. | Ask source-grounded question with cited chunk; remove source removes retrieval. |
| Connector-synced knowledge | Drive/SharePoint/Notion/GitHub/Slack/Gmail/Teams indexed as project/org sources. | Missing as complete synced knowledge product. | Connector sync jobs, retrieval, projects. | Sync state, source permissions, chunk index, failure status. | Sync fixture connector, cite result, revoke permission removes access. |
| Source viewer | Inspect original, extracted text, chunks, warnings, index status. | Missing. | Project source UI, artifacts/storage. | Source metadata and preview endpoint. | Playwright source viewer and reindex/delete controls. |

## Memory

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Scoped retrieval | Authorized multi-signal memory retrieval with query expansion, ranking, dedupe. | Foundation present. | Memory core, embeddings, requester context. | Memory rows with scope/source/importance/provenance. | Tests for personal/workspace/org/restricted retrieval. |
| Explicit memory | User says remember/forget/edit and memory updates immediately. | E2E-proven: add (remember) → retrieve (list) → edit → delete through the real memory UI, stable across reruns. Proof: apps/web/e2e/memory.spec.ts. | Chat route, memory router/UI. | Memory entry and audit event. | E2E remember, retrieve, edit, delete. **Done** via apps/web/e2e/memory.spec.ts. |
| Autonomous memory | Assistant extracts useful durable facts visibly with undo. | Partial: extraction exists; inline visibility/undo completeness uncertain. | Extraction job, chat UI. | Candidate memory, source conversation, undo event. | Test autonomous memory appears, undo removes retrieval. |
| Memory control center | Search/edit/delete/archive/merge/pin/scope/import/export, usage logs. | Partial memory page exists; full control center missing. | Memory UI/API, import/export. | Memory status, provenance, usage records. | Playwright full CRUD plus export/import round trip. |
| Memory conflict/staleness | Detect stale/conflicting memories and propose merge/replacement. | Missing. | Memory ranking, synthesis jobs. | Conflict links, stale flag, resolution history. | Test newer preference suppresses old conflicting one. |
| Memory privacy controls | Disable memory per project/chat/user, classify sensitive memories. | Missing. | Settings, memory policy, extraction. | Policy settings and classification. | Test disabled memory prevents write/retrieval. |

## Artifacts

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Artifact creation | Assistant creates durable reusable outputs from chat/tasks. | Implemented (Phase 5): create via POST /artifacts and runtime save_artifact; version-addressed storage; list/download/duplicate; route boundary enforces tenant isolation (404 cross-org). Proof: tests/test_artifact_workspace.py::test_create_read_download_roundtrip_survives_refetch, ::test_duplicate_creates_independent_copy, ::test_router_blocks_cross_org_access. | Artifact core/router, runtime. | Artifact row, object storage/local fallback, message/task linkage. | E2E create artifact, refresh, download. |
| Artifact workspace | Side-by-side preview, full artifact browser, project/task grouping. | Implemented (Phase 5): full artifact browser (search + kind filter + conversation/task grouping), two-pane workspace, Preview/Edit/Versions tabs, version timeline, diff viewer, plus an in-chat side panel (open/edit artifacts without leaving chat). Typecheck-verified via npm run build; route /artifacts (behavioral E2E deferred to harness). | Artifacts UI/API. | Artifact collections and filters. | Playwright artifact browser, permissions, refresh. |
| Artifact editing | User and AI can edit artifacts without destructive overwrite. | Implemented (Phase 5): non-destructive versioning (artifact_versions), manual edit, AI edit, restore, unified diff — all via permission seam + audit. Proof: tests/test_artifact_workspace.py::test_edit_creates_new_version_without_clobbering **plus UI E2E** apps/web/e2e/artifacts.spec.ts (create → preview → edit → new version → diff → restore through the real workspace; stable across repeated runs). | Artifact editor, version API. | `artifact_versions`, diff metadata. | Edit creates new version; restore works. **Done** (backend unit + UI E2E). |
| Artifact renderers | Markdown, HTML, React, SVG, diagrams, CSV/table, JSON, code, image, docs/sheets/slides. | Implemented (Phase 5): markdown/code/json/csv-table/image renderers + sandboxed iframe (no allow-same-origin) for HTML/SVG with CSP; download fallback for binary/office types. Proof: components/artifacts/ArtifactRenderer.tsx; typecheck-verified via npm run build (behavioral render E2E deferred to harness). | Renderer UI, sandbox. | Renderer selection and safe preview policy. | Renderer tests per supported type. |
| Artifact publish/share | Controlled share/publish/unpublish with permissions. | Implemented (Phase 5): governed publish/unpublish with signed-token public /shared/{token} link and revocation; seam-gated + audited. Proof: tests/test_artifact_workspace.py::test_public_share_boundary_serves_then_revokes. | Sharing API/UI, permissions. | Share links, ACL, publish status. | Unauthorized cannot access; unpublish revokes. |

> Phase 5 proof strategy: backend behavior is proven by DB-backed pytest against an isolated database. A Playwright behavioral E2E harness now exists in the repo (`apps/web/playwright.config.ts` + `apps/web/e2e/`, isolated dual-server with real dev-OTP auth); the artifact edit/version/diff/restore lifecycle is covered by `apps/web/e2e/artifacts.spec.ts`. Rows not yet covered by an E2E spec remain typecheck-verified via `npm run build`.
>
> Phase 5 lifecycle coverage: create, preview, edit, AI edit, version, diff, restore, rename, **duplicate**, delete (soft), publish/unpublish (revocable share link). **`move` is deferred**: it requires a Projects destination surface, which is Phase 3 (not yet built); there is no target to move artifacts between today.

## Deep Research

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Research mode | Dedicated quick/standard/exhaustive/trusted research runs. | Missing as product mode; agent can search/fetch. | Research API/UI, planner, runtime. | Research run, plan, source scope, depth, status. | E2E web research with cited report. |
| Citation collector | Store source snippets, metadata, confidence, limitations. | Missing as dedicated service. | Retrieval/browser/research. | Citation records linked to report/message/artifact. | Test no citation without source snippet; report cites stored sources. |
| Internal + external research | Merge project files, web, connector sources, uploads, MCP. | Missing as cohesive research product. | Research planner, retrieval, connectors. | Source scope and retrieval provenance. | Mixed project + web + connector research proof. |
| Research report artifact | Export report to artifact/document/PDF with source table. | Missing. | Research UI, artifacts, export. | Report artifact and source appendix. | Download/open report after refresh. |

## Multimodal, Files, and Data

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| General file upload | Attach files to chat/project/task/research/artifact. | Missing as general workflow. | Upload service, storage, UI composer. | Uploaded file record, object path, linked entity. | Upload file, refresh, file remains attached. |
| Document intelligence | Parse/summarize/compare/extract PDFs, DOCX, PPTX, XLSX, CSV, code, text. | Missing as complete product; code/data tools exist. | Parsers, ingestion, LLM, artifacts. | Extracted text/tables/slides and parser warnings. | Tests per file class with cited extraction. |
| Image input | Vision for screenshots, diagrams, charts, UI inspection, OCR. | Missing. | LLM provider abstraction, upload, message renderer. | Image attachment and extracted metadata. | Screenshot question returns grounded answer. |
| Image generation | Generate images with style/size/count controls. | Missing. | Image provider tool, artifacts. | Image artifact and generation metadata. | Generate image artifact and reopen. |
| Image editing | Edit/variation/masking/background operations where provider supports. | Missing. | Image provider tool, artifact versions. | Source image, edit params, output artifact/version. | Edit creates new image version. |
| Voice input/output | Speech-to-text, TTS, hands-free mode, audio artifacts/transcripts. | Missing. | Voice provider, chat UI, storage. | Audio attachment, transcript message, TTS metadata. | Record/upload audio, transcript persists, TTS metadata present. |
| Data analysis workspace | Python-backed analysis of CSV/XLSX/JSON with tables, charts, reports. | Partial: `code.python` exists; user-facing data workspace missing. | Code sandbox, data UI, artifacts. | Dataset record, generated charts/reports. | Upload CSV, generate chart/report artifacts. |

## Connectors and Apps

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Connector directory | App catalog with setup, scopes, actions, risk, health, policy, audit. | Foundation present. UI directory E2E-proven to render the catalog and reflect a connected app (apps/web/e2e/connectors.spec.ts, seeded). Framework connector install→actions→policy→execute proven deterministically in tests/test_connector_framework.py + tests/test_connector_operations.py. NOTE: the directory's real connect path is OAuth (not E2E-driven); framework fixture connectors (internal_echo/internal_time) are not surfaced in the OAuth catalog UI. | Connectors UI/API, repository, health. | Connector definitions, installations, policies, health. | Connect app fixture, see health/actions/policy. **Directory render + connected-state proven (UI); install/actions/policy proven (backend).** |
| Gmail | Search/read and draft/send-with-approval. | Foundation present for search/draft; send governed. | Gmail connector, broker, approvals. | Connector credential, drafts, approval records. | Search, draft, approve send/draft path according to policy. |
| Google Drive | Search/read/create files; synced knowledge. | Catalog entry present; typed runtime/sync incomplete. | OAuth connector, sync/index. | Drive source records and file actions. | Read indexed Drive file with citation. |
| Google Calendar | Read/create/update events with approval policy. | Catalog entry present; typed runtime incomplete. | OAuth connector actions. | Calendar action records and approvals. | Create event pauses/approves and audits. |
| Slack/Teams | Search/read/send/publish agents to channels. | Catalog for Slack present; Teams missing/incomplete. | Connectors, agent publishing. | Channel mappings, message events. | Slack/Teams fixture creates Chronos task and response. |
| GitHub | Search/read repos/issues/PRs, code tasks, PR creation. | Catalog entry present; coding workflow incomplete. | GitHub connector, coding agent. | Repo workspace, branch/PR refs. | Read repo, create branch/PR under policy. |
| Notion/Linear/HubSpot/Airtable/Jira/Salesforce/Stripe | Typed read/write actions with health and policies. | Some catalog entries present; broad typed tools incomplete. | Connector adapters. | Action schemas, approvals, audit. | Per-connector smoke for read and approval-gated write. |
| Custom MCP | Remote MCP setup, discovery, allow/deny, health, policy. | Foundation present with MCP client path. | MCP connector framework. | Server config, discovered tools, policy. | Discover tool, execute read action, gate risky action. |
| Custom HTTP/OpenAPI | User-defined API connector from OpenAPI/manual schema. | Missing as productized feature. | Connector builder. | Auth config, action schemas, test calls. | Import fixture OpenAPI, execute tool through broker. |

## Browser, Computer, and Autonomous Operation

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Browser search/fetch | Web search/fetch with screenshots and fallback honesty. | Foundation present. | Browser connector, broker, storage. | Tool result includes provider/tier/warnings; screenshots stored. | Live/fixture search tests and screenshot persistence. |
| Full browser operator | Navigate/click/type/select/scroll/wait/extract/download/upload/read DOM. | Missing beyond search/fetch/contact extraction. | Browser session manager, tool registry, UI. | Browser session, current URL, screenshots, downloads. | E2E form fill, download artifact, timeline replay. |
| Live browser view | User sees browser session and task actions. | Missing. | Browser UI, streaming screenshot/WebRTC/VNC equivalent. | Session state and screenshots/video frames. | Playwright opens task and sees current page state. |
| User takeover | Pause automation for user click/type/MFA/CAPTCHA, then hand back. | Missing as complete feature. | Browser UI, runtime pause/resume. | Takeover state, hand-back summary. | E2E takeover fixture and resume. |
| Authenticated browser sessions | User-consented saved sessions, revocation, per-task approval. | Missing. | Browser profile manager, settings. | Session refs, consent, last used, revocation. | Session isolation and revoke tests. |
| Cloud computer | Sandboxed terminal/files/browser/editor/package install/artifact export. | Missing. | Sandbox service, computer tools, UI. | Computer session, filesystem, command logs, artifacts. | Build small app in sandbox, export artifact, enforce timeout. |
| Local computer bridge | Desktop bridge with authorized folders/apps/commands/local compute. | Missing. | Desktop app/bridge, broker, policy. | Device registration, folder grants, command audit. | Unauthorized folder blocked; approved command runs. |
| Long-running delegated work | Manus-style task console with plan, live actions, artifacts, pause/resume/cancel. | Partial task/activity UI exists; complete console missing. | Tasks UI, runtime trace. | Durable task timeline and controls. | Long task monitored, paused, resumed, completed after refresh. |

## Coding Agent

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Repo workspace | Clone/import repos, create branch, inspect/edit/test/diff. | Missing as productized coding agent. | Coding runtime, GitHub connector, cloud/local computer. | Repo workspace, branch, diff, command logs. | Clone fixture repo, edit, run tests, show diff. |
| Commit/PR flow | Commit and open PR under approval/policy. | Missing. | GitHub connector, broker, approvals. | Commit SHA, PR URL, approval chain. | Approved PR creation records URL and audit. |
| Code review | Review PR/issues with inline findings and suggested patches. | Missing. | Coding agent, GitHub connector, UI. | Review artifact/comments. | Review fixture PR and produce actionable findings. |
| Test/debug loop | Run tests, inspect failures, patch, rerun, summarize. | Partial via `code.python` only; repo command loop missing. | Computer/coding tools. | Test command records and result artifacts. | Failing fixture test fixed with green rerun. |

## Scheduled Work, Workflows, and Agents

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Scheduled tasks | One-time/daily/weekly/monthly/interval/event-triggered tasks. | Missing as productized feature. | Scheduler, tasks UI/API. | Schedule record, timezone, next run, history. | Schedule runs once at correct time; paused schedule skips. |
| Workflows | Convert tasks to reusable workflows with steps/dependencies/conditions/approvals. | Foundation present in connector workflow framework; product incomplete. | Workflow runtime/UI. | Workflow definition, runs, steps, artifacts. | Convert task to workflow and rerun. |
| Monitors | Watch sites/sources/connectors/inbox/news and alert or run task. | Missing. | Monitor service, scheduler, connectors. | Monitor config, condition, evidence, alerts. | Fixture change creates cited monitor alert. |
| Agent profiles | Reusable agents/personas with instructions/tools/projects/memory/autonomy/policy. | Partial assistants/personas surfaces exist; full profiles incomplete. | Agents API/UI, settings, runtime. | Agent profile, tool grants, memory scopes. | Create agent, attach project/tool, run constrained task. |
| Agent publishing | Publish agents to Slack/Teams/email/web/API. | Missing. | Connectors, webhooks, agent runtime. | Channel mappings and conversation/task linkage. | External fixture message creates Chronos task and audited reply. |

## Collaboration, Admin, Mobile, and Compliance

| Capability | Target parity | Current state | Implementation area | Interface and persistence | Acceptance proof |
|---|---|---|---|---|---|
| Shared conversations | Share chats with members/projects and role-based visibility. | Missing/partial unknown; not complete product. | Chat/projects/permissions. | Conversation ACL/share records. | Viewer can read but not mutate; unauthorized blocked. |
| Comments and mentions | Comments on projects/artifacts/tasks, mentions, assignments, handoff. | Missing. | Collaboration UI/API. | Comment, mention, assignment records. | Mention creates notification and access respects role. |
| Admin/RBAC | Members, roles, groups, workspaces, projects, agents, policies, retention. | Partial settings/members exist; complete admin missing. | Settings/admin, permissions. | Role/group/policy records. | Role test for project/tool/memory access. |
| Audit/compliance export | Export audit, connector access, memory access, approvals, task reports. | Missing as productized export. | Admin/audit API. | Export jobs/artifacts. | Export contains expected events and no secrets. |
| Responsive mobile web | Core flows work on mobile viewports. | Unknown/incomplete. | Frontend. | Same APIs; responsive layouts. | Playwright mobile smoke for chat, approvals, task, artifacts. |
| Desktop app | Desktop shell for local bridge, notifications, folder auth. | Missing. | Desktop packaging/bridge. | Device registration and local grants. | Desktop bridge proof and revoke. |
| Notifications | In-app/email/Slack/Teams/desktop notifications for approvals/tasks/monitors. | Missing/partial. | Notification service, connectors. | Notification prefs, delivery records. | Approval notification creates actionable link; prefs honored. |
| Billing/plan controls | Enterprise plan/account controls if commercialized. | Missing; settings mention unsupported billing. | Admin/billing integration. | Plan/customer/subscription records if enabled. | Billing disabled is truthful or integration smoke passes. |

## Final Parity Proof Scenarios

| Scenario | Required flow | Required proof |
|---|---|---|
| ChatGPT-style parity | Upload CSV and chart/image, analyze both, create cited answer, chart/report artifacts, regenerate/branch. | Playwright + API proof with persisted files, artifacts, branch lineage, and citations. |
| Claude-style parity | Create project, upload docs, ask project-grounded question, cite sources, create/edit/version artifact, save/inspect memory. | Project source citations, artifact version history, memory usage proof, refresh persistence. |
| Manus-style parity | Browser task navigates/clicks/types, pauses for takeover, resumes, downloads file, creates artifact, replays after refresh. | Browser session trace, takeover trace, artifact, and restart/refresh proof. |
| Enterprise governance | Risky write requires approval, unauthorized user blocked, authorized approval resumes exactly once, audit export proves chain. | Approval and audit records plus no duplicate external write. |
| Connector ecosystem | Connected source syncs, source is used in research, permission revocation removes access. | Sync state, citation, revoked access test. |
| Reliability and safety | Task survives API restart, cancellation stops work, prompt injection blocked, degraded connector reported honestly. | Restart/cancel/injection/fallback tests. |

## Completion Workflow

1. Pick a matrix row before implementing a feature.
2. Update the row status only after implementation and proof exist.
3. Add or update tests named for the row's acceptance proof.
4. Link proof artifacts or commands in the relevant PR/implementation summary.
5. Do not mark any row complete based on scaffolding, mocks, or UI-only controls.
