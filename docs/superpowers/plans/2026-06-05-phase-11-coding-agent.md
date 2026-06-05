# Phase 11 Coding Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully implement Phase 11 Coding Agent parity rows from `CHRONOS_TOTAL_PARITY_GOAL.md` and `docs/chronos_total_parity_matrix.md`.

**Architecture:** Extend the existing broker-routed `repo.*` connector instead of adding a parallel coding runtime. Keep repo files jailed in the task workspace, persist review/PR artifacts as repo-local durable metadata, and make risky mutation/PR actions explicit and auditable through tool results.

**Tech Stack:** Python 3.11, FastAPI runtime tool schemas, pytest, Next.js static route guard.

---

### Task 1: Expand Repo Workspace Tools

**Files:**
- Modify: `apps/api/connectors/repo_workspace.py`
- Modify: `apps/api/runtime/tool_registry.py`
- Test: `apps/api/tests/test_repo_workspace.py`

- [x] **Step 1: Write failing tests** for import/list/status/command-scoped tests/commit/PR/review behavior.
- [x] **Step 2: Run focused pytest** and verify the new tests fail because tools are missing.
- [x] **Step 3: Implement minimal connector and registry support** for the new `repo.*` actions.
- [x] **Step 4: Run focused pytest** and verify all repo workspace tests pass.

### Task 2: Add Coding Agent Product Surface

**Files:**
- Create: `apps/web/app/coding/page.tsx`
- Create: `apps/web/components/coding/CodingAgentScreen.tsx`
- Modify: `apps/web/app/chat/page.tsx`
- Test: `apps/web/e2e/coding-agent-static.spec.ts`

- [x] **Step 1: Write static route/UI guard** for the coding agent surface.
- [x] **Step 2: Run Playwright static test** and verify it fails before the route/component exists.
- [x] **Step 3: Add the coding route and route navigation entry** using the existing product shell style.
- [x] **Step 4: Run Playwright static test** and web build.

### Task 3: Update Parity Contract

**Files:**
- Modify: `docs/chronos_total_parity_matrix.md`

- [x] **Step 1: Update Coding Agent rows** only after backend and static UI proof pass.
- [x] **Step 2: Run final focused backend, static frontend, and build verification.**
