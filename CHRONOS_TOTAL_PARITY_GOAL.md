# Chronos Total Parity Goal

## Canonical Goal

Chronos is an enterprise AI platform whose goal is total practical parity with the combined capability set of ChatGPT, Claude.ai, and Manus.ai, delivered reliability-first through governed autonomous execution, persistent organizational memory, approvals, auditability, tenant boundaries, and polished product UX.

Chronos should provide:

- ChatGPT-style general assistance, multimodal input/output, data analysis, coding help, connectors, agents, and workspace collaboration.
- Claude.ai-style projects, source knowledge, memory, artifacts, research, writing/coding workflows, and polished document/code creation.
- Manus.ai-style autonomous task execution, browser operation, cloud computer workspaces, local computer bridge, scheduled tasks, and long-running delegated work.
- Chronos-native enterprise controls: permissions, scoped memory, broker-governed tools, approvals, audit trails, connector policy, tenant isolation, and honest degraded-mode behavior.

This document is the north-star product goal. It is not an optional roadmap. Existing sprint and phase documents are useful implementation history, but future work should be judged against this goal.

The controlling implementation contract is `docs/chronos_total_parity_matrix.md`. Every parity feature must map to a matrix row before implementation and must satisfy that row's acceptance proof before it can be called complete.

## Non-Negotiable Engineering Rules

- Every external tool call routes through `tool_broker.execute`.
- Every memory read routes through `memory.retrieve`.
- Every action authorization routes through `permission.check`.
- Every risky write action requires approval by default unless explicit admin policy allows autonomy.
- Every generated output that users may reuse becomes a durable artifact.
- Every task must be replayable from persisted state, not only transient SSE or Redis events.
- Every connector/browser/file/web output is treated as untrusted until isolated, cited, and policy-checked.
- Every degraded mode must be visible and truthful.
- No feature is complete until it works through the real UI/API, persists durable state, respects tenant boundaries, emits audit evidence, survives refresh/restart where applicable, and has automated proof.

## Implementation Phases

### 0. Product Inventory and Acceptance Matrix

Maintain `docs/chronos_total_parity_matrix.md` as the source of truth for parity delivery:

- Every ChatGPT, Claude.ai, Manus.ai, and Chronos-native enterprise capability has a row.
- Every row records target behavior, current state, implementation area, required interface/persistence, and acceptance proof.
- No parity claim is valid unless the relevant matrix row is implemented with proof.
- New parity requests must either map to an existing row or add/update a row before implementation begins.

### 1. Runtime Reliability Foundation

Build the platform substrate required for reliable autonomous work:

- Task queue with priority, cancellation, retries, timeouts, concurrency limits, and dead-letter state.
- Durable trace model for model steps, tool calls, tool results, screenshots, artifacts, approvals, sub-agents, citations, memory events, and safety decisions.
- Crash recovery for native-loop tasks, DAG tasks, approvals, connector jobs, scheduled tasks, and browser/computer sessions.
- Idempotency keys for external write actions.
- Prompt-injection and untrusted-content isolation for browser pages, files, emails, connectors, MCP output, and OCR text.
- Policy gates that prevent untrusted content from triggering unauthorized actions.

### 2. Unified Product Shell

Make Chronos feel like one complete enterprise product:

- Main surfaces: Chat, Projects, Research, Tasks, Artifacts, Memory, Connectors, Agents, Workflows, Approvals, Activity/Audit, Settings/Admin.
- Global search across conversations, tasks, artifacts, project sources, memory, and connector-indexed content.
- Command palette and composer controls for model, mode, project, uploads, connectors, autonomy, and scheduled runs.
- Rich message model with model, mode, timestamps, memory refs, citations, tool traces, artifact refs, approval state, and runtime status.

### 3. Projects and Knowledge Sources

Match and exceed Claude/ChatGPT project knowledge:

- First-class projects with instructions, members, visibility, default tools, memory policy, sources, artifacts, conversations, tasks, and research runs.
- Upload/import/index/cite PDFs, DOCX, PPTX, XLSX, CSV, text, Markdown, code, images, URLs, Drive, SharePoint, Notion, GitHub, Slack, Gmail, and Teams sources.
- Source lifecycle: parse, chunk, embed, index, refresh, remove, reindex, permission resync, and citation.
- Retrieval across project sources, memory, task scratchpads, connector indexes, and conversation history with permission-aware ranking.

### 4. Memory Parity

Turn current scoped memory into a complete enterprise memory control system:

- Explicit, autonomous, project, workspace, org, persona, task scratchpad, synthesized, imported, and connector-derived memory.
- Confidence, staleness, conflict, archive, deletion, provenance, and access logs.
- Memory control center for search, edit, delete, archive, merge, pin, scope changes, import/export, and per-project/chat memory toggles.
- Per-message memory chips showing saved and used memories with undo/edit controls.

### 5. Artifact Workspace

Upgrade persisted artifacts into a Claude-grade workspace:

- Artifact types: Markdown, text, HTML, React, SVG, diagrams, JSON, CSV, charts, code, notebooks, images, documents, spreadsheets, slide decks, and zipped project bundles.
- Lifecycle: create, preview, edit, AI edit, version, diff, restore, duplicate, rename, move, export/download, publish/share, unpublish, and permission control.
- Side panel in chat, full artifact browser, version timeline, diff viewer, and artifact-aware editing composer.

### 6. Deep Research

Add a dedicated research product:

- Research runs with question, project, allowed/disallowed sources, depth, time budget, citation policy, status, plan, findings, citations, report artifact, and limitations.
- Source scopes: web, project files, connector-synced sources, uploaded files, specific domains, and MCP tools.
- Quick, standard, exhaustive, and trusted-sources-only modes.
- Live research timeline and final cited report with export to artifact/document/PDF.

### 7. Multimodal and Data Analysis

Match ChatGPT-style multimodal workflows:

- File upload and document intelligence for PDFs, docs, slides, spreadsheets, CSV/JSON, code, text, images, and audio.
- Image input for screenshots, diagrams, charts, UI inspection, OCR, image comparison, and visual reasoning.
- Image generation and editing through provider abstractions, with outputs saved as artifacts.
- Voice input/output with transcript persistence, audio attachments, TTS, and hands-free mode.
- Data analysis workspace over uploaded files with Python execution, tables, charts, reports, and downloadable artifacts.

### 8. Connectors and Apps

Productize broad app parity:

- Connector directory with setup, scopes, actions, risk levels, health, sync, policy, audit, and last-used state.
- Built-ins: Gmail, Google Drive, Google Calendar, Slack, GitHub, Notion, Linear, HubSpot, Airtable, Jira, Outlook, Teams, SharePoint/OneDrive, Salesforce, Stripe, webhooks, custom HTTP, and remote MCP.
- Synced/indexed knowledge connectors with incremental updates, deletion handling, permission mirroring, citations, and failure reporting.
- Custom remote MCP and HTTP connectors with schema discovery, allow/deny tools, health checks, approval policy, and audit.

### 9. Browser Operator

Expand browser from search/fetch into full Manus-style web operation:

- Persistent isolated browser sessions with current URL, screenshots, downloads, cookies/session reference, takeover state, and task binding.
- Broker-routed tools for navigate, click, type, select, scroll, wait, extract, screenshot, download, upload, read DOM, get state, close, and request takeover.
- Live browser view, user takeover, MFA/CAPTCHA handoff, hand-back, and session revocation.
- Authenticated web tasks with explicit session consent and per-task approval for sensitive sites.

### 10. Cloud Computer and Local Computer

Provide Manus-style computer workspaces:

- Cloud computer per task with filesystem, terminal, browser, editor, package install, screenshots, and artifact export.
- Tools for command execution, file read/write/list, app opening, screenshots, package installation, and output archiving.
- Sandbox boundaries, network policy, resource limits, command audit, and risky-command approvals.
- Optional desktop bridge for authorized local folders, terminal commands, local app launching where supported, local compute/GPU, and revocable per-task permissions.

### 11. Coding Agent

Add Codex/Claude Code-style coding workflows:

- GitHub/repo workspaces with clone, branch, inspect, edit, test, diff, commit, and PR creation where authorized.
- Code task UI with file tree, diff viewer, terminal output, test results, PR status, review comments, and artifact bundle.
- Mutation actions governed by repo policy and audit.

### 12. Scheduled Tasks, Workflows, and Monitors

Support durable recurring and event-driven work:

- Scheduled tasks: one-time, daily, weekly, monthly, interval, webhook, and connector-triggered.
- Workflow builder with steps, dependencies, conditions, approvals, connectors, retries, outputs, and run history.
- Monitors for websites, sources, connectors, inboxes, news, and recurring digests.

### 13. Agents, Personas, and Workspace Agents

Create reusable enterprise agents:

- Agent profiles with role, instructions, model, tools, connectors, projects, memory scopes, autonomy level, approval policy, and schedule permissions.
- Agent builder with templates for research, executive assistance, sales/SDR, support, engineering, data analysis, and operations.
- Publish agents to Slack, Teams, email, web embeds, and APIs while preserving Chronos conversations, tasks, audit, memory, and policy.

### 14. Collaboration and Enterprise Admin

Make Chronos usable by real organizations:

- Shared conversations, project comments, artifact comments, task assignment, mentions, approval routing, handoff, and shared pins.
- Admin for members, roles, groups, workspaces, projects, agents, connector policy, memory policy, retention, audit export, and data controls.
- Compliance reporting for audit, connector access, memory access, approvals, and task execution.

### 15. Mobile, Desktop, and Notifications

Deliver broad platform reach:

- Responsive web for chat, uploads, approvals, task monitoring, research, artifacts, and status views.
- Desktop app for local computer bridge, notifications, global shortcut, file/folder authorization, and tray status.
- Notifications for approvals, task completion, failures, takeover needed, scheduled task results, and monitor alerts.

### 16. Security, Privacy, and Compliance

Harden parity features for enterprise use:

- Encrypted credentials, secret redaction, token rotation, and no secret logs.
- Tenant isolation in every table, query, API, connector, task, artifact, and memory path.
- Browser/computer sandboxing, network controls, download scanning, file restrictions, and egress review.
- Least-scope connectors, permission sync, revocation, and truthful failure states.
- Memory opt-out, source exclusion, sensitive-memory classification, retention, export, deletion, and admin visibility rules.

### 17. Product Polish

Make the platform feel complete:

- No fake controls and no placeholder data presented as live.
- Clear empty/loading/running/paused/approval/takeover/failed/complete/degraded states.
- Dense enterprise UI, consistent layouts, accessibility, keyboard/focus states, and tooltips for advanced controls.
- First-run setup for org/workspace, connectors, project, source upload, first research, first approval, and first scheduled task.
- Runtime health dashboard for queue depth, connector health, browser/computer workers, model latency, token usage, approval bottlenecks, and scheduled task health.

## Final Acceptance Suite

Chronos is complete only when a repeatable proof suite demonstrates:

- ChatGPT-style parity: upload CSV and chart/image, analyze both, produce cited answer, create chart/report artifacts, regenerate/branch.
- Claude-style parity: create project, upload docs, ask project-grounded question, cite sources, create/edit/version artifact, save/inspect memory.
- Manus-style parity: browser task navigates/clicks/types, pauses for takeover, resumes, downloads file, creates artifact, and replays after refresh.
- Enterprise governance: risky write requires approval, unauthorized user cannot approve, authorized approval resumes exactly once, audit export proves the chain.
- Connector ecosystem: connected source syncs, source is used in research, permission revocation removes access.
- Reliability: task survives API restart, cancellation stops browser/computer work, prompt injection is blocked, degraded connector is reported honestly.

The product is not parity-complete until every matrix row has implementation proof, backend tests pass, web build passes, Playwright parity suite passes, security/governance tests pass, risky actions are governed, durable outputs reopen after refresh, and claims are backed by UI/API proof artifacts.
