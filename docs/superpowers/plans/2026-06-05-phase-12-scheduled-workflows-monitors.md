# Phase 12 Scheduled Workflows Monitors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully implement Phase 12 from `CHRONOS_TOTAL_PARITY_GOAL.md`: scheduled tasks, reusable workflows, event triggers, monitors, alerts, run history, pause/resume, and recovery.

**Architecture:** Extend the existing in-process APScheduler and workflow runtime instead of introducing Celery or a separate scheduler. Scheduled work, workflow triggers, monitor configs, alerts, and histories remain tenant-scoped database records; all task materialization and workflow execution continues through existing runtime seams, permission checks, and audit logging.

**Tech Stack:** FastAPI, SQLAlchemy Core/reflection, Alembic, APScheduler, Pydantic v2, Next.js App Router, TypeScript, Playwright static tests.

---

### Task 1: Backend Phase 12 Tests

**Files:**
- Modify: `apps/api/tests/test_scheduled_tasks.py`
- Create: `apps/api/tests/test_phase12_workflows_monitors.py`
- Create: `apps/web/e2e/workflows-static.spec.ts`

- [ ] **Step 1: Write failing schedule tests**

Add tests that assert one-time, daily, weekly, monthly, interval, webhook, and connector-trigger schedule metadata can compute next runs; paused schedules are skipped; due schedule execution records run history and audit payload scope.

- [ ] **Step 2: Write failing workflow/monitor tests**

Add tests that assert workflow definitions persist triggers, conditions, retry metadata, run history, pause/resume/recovery state, monitor evaluation creates cited alerts, and event-trigger dispatch starts eligible workflows.

- [ ] **Step 3: Write failing UI static test**

Add a Playwright static route guard for `/workflows` that expects real schedule, workflow, monitor, alert, and run-history controls rather than an empty placeholder.

### Task 2: Backend Runtime and API

**Files:**
- Modify: `apps/api/migrations/versions/0021_scheduled_tasks.py`
- Create: `apps/api/migrations/versions/0029_phase12_scheduled_workflows_monitors.py`
- Modify: `apps/api/jobs/scheduled_tasks.py`
- Modify: `apps/api/routers/schedules.py`
- Modify: `apps/api/routers/workflows.py`
- Modify: `apps/api/connectors/framework/workflows.py`
- Modify: `apps/api/connectors/framework/repository.py`
- Modify: `apps/api/main.py`

- [ ] **Step 1: Expand schedule semantics**

Support `one_time`, `daily`, `weekly`, `monthly`, `interval`, `webhook`, and `connector_trigger` kinds with timezone-aware next-run calculation and paused/enabled state.

- [ ] **Step 2: Persist run history**

Add `scheduled_task_runs` and write a row for every scheduled/manual/event-triggered execution with status, trigger source, task/workflow/monitor refs, evidence, and next-run metadata.

- [ ] **Step 3: Add workflow trigger APIs**

Persist workflow triggers and expose endpoints for list/create triggers, start runs, event dispatch, pause/resume/cancel, run detail, and recovery.

- [ ] **Step 4: Add monitors**

Add `monitors` and `monitor_alerts`, endpoints for create/list/update/pause/resume/evaluate, and deterministic monitor evaluation for website/source/connector/inbox/news/digest configs.

### Task 3: Workflows Product Surface

**Files:**
- Modify: `apps/web/app/chat/page.tsx`

- [ ] **Step 1: Replace empty Workflows panel**

Render a real `WorkflowsScreen` with schedules, workflow definitions, workflow runs, monitors, monitor alerts, pause/resume/run controls, and truthful empty/degraded states.

- [ ] **Step 2: Wire API calls**

Use existing `apiFetch` and route conventions to load `/schedules`, `/workflows`, `/workflows/runs`, `/workflows/triggers`, `/monitors`, and `/monitors/alerts`.

### Task 4: Docs and Verification

**Files:**
- Modify: `docs/chronos_total_parity_matrix.md`

- [ ] **Step 1: Update Phase 12 rows**

Mark scheduled tasks, workflows, and monitors implemented with exact proof commands and honest scope notes.

- [ ] **Step 2: Verify**

Run focused backend tests, the static Playwright test, and `npm --prefix apps/web run build`.
