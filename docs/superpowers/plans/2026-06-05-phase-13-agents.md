# Phase 13 Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fully implement Phase 13 by making Chronos agents reusable, governed, runnable, publishable workspace agents with durable state and proof.

**Architecture:** Add durable agent profile and publishing tables, a focused `core.agents` service, a `/agents` API router, and a real `/agents` product surface. Agent runs create normal Chronos tasks through the existing task runner so conversations, audit, memory, projects, and policy stay on current governed seams.

**Tech Stack:** FastAPI, SQLAlchemy reflected tables, Alembic, pytest, Next.js App Router, React.

---

### Task 1: Backend Agent Profiles

**Files:**
- Create: `apps/api/tests/test_agents_phase13.py`
- Create: `apps/api/core/agents.py`
- Create: `apps/api/routers/agents.py`
- Create: `apps/api/migrations/versions/0029_agents_phase13.py`
- Modify: `apps/api/main.py`

- [ ] **Step 1: Write failing profile tests**

Write tests that create an agent with role, instructions, model, tools, connectors, projects, memory scopes, autonomy level, approval policy, schedule permissions, and template id; list/get/update the profile; run a constrained task; and prove cross-org reads are blocked.

- [ ] **Step 2: Run tests to verify red**

Run: `pytest apps/api/tests/test_agents_phase13.py -q`
Expected: FAIL because the router/service/tables do not exist.

- [ ] **Step 3: Implement profile persistence and API**

Add `agent_profiles` and `agent_profile_events`; implement create/list/get/patch/delete/run/template endpoints with permission checks and audit logging.

- [ ] **Step 4: Run tests to verify green**

Run: `pytest apps/api/tests/test_agents_phase13.py -q`
Expected: PASS for profile tests.

### Task 2: Agent Publishing

**Files:**
- Modify: `apps/api/tests/test_agents_phase13.py`
- Modify: `apps/api/core/agents.py`
- Modify: `apps/api/routers/agents.py`
- Modify: `apps/api/migrations/versions/0029_agents_phase13.py`

- [ ] **Step 1: Write failing publishing tests**

Add tests that publish an agent to Slack/Teams/email/web/API, receive an external fixture message, create a Chronos task linked to the mapping/conversation, emit audited events, and reject unauthorized/cross-org publication access.

- [ ] **Step 2: Run tests to verify red**

Run: `pytest apps/api/tests/test_agents_phase13.py -q`
Expected: FAIL on missing publishing behavior.

- [ ] **Step 3: Implement publishing mappings and inbound task bridge**

Add `agent_publications`, webhook-style inbound endpoint, channel validation, mapping status, payload metadata, and normal task creation through `create_task_record`.

- [ ] **Step 4: Run tests to verify green**

Run: `pytest apps/api/tests/test_agents_phase13.py -q`
Expected: PASS.

### Task 3: Agents Product UI

**Files:**
- Modify: `apps/web/app/agents/page.tsx`
- Modify: `apps/web/app/assistants/page.tsx`
- Create or modify: `apps/web/e2e/agents-static.spec.ts`

- [ ] **Step 1: Write failing static UI test**

Verify `/agents` is no longer a chat alias and contains agent builder, templates, tool/project/memory policy, publish targets, and run controls.

- [ ] **Step 2: Run test to verify red**

Run: `npx playwright test apps/web/e2e/agents-static.spec.ts --config apps/web/e2e/static.config.ts`
Expected: FAIL before UI implementation.

- [ ] **Step 3: Implement real UI surface**

Build dense enterprise agent workspace with templates, profile list/detail, builder form, run panel, publishing panel, and truthful empty/degraded states.

- [ ] **Step 4: Run web verification**

Run: `npx next build --webpack`
Expected: PASS.

### Task 4: Documentation and Matrix

**Files:**
- Modify: `docs/chronos_total_parity_matrix.md`

- [ ] **Step 1: Update matrix only after proof**

Mark `Agent profiles` and `Agent publishing` implemented with exact test/build proof and scope notes for credential-dependent external live delivery.

- [ ] **Step 2: Final verification**

Run targeted backend tests and web build; summarize any credential-dependent limitations.
