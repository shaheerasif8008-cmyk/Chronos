# W5 — Admin & Compliance Plan

_Workstream plan: design + phase decomposition. W5.1 is detailed and executable; later phases are scoped, not yet step-by-step. Each phase's FIRST step is "audit and confirm the gap against current code" — pieces of this subsystem already exist and the gaps below are grounded in the 2026-06-23 audit, not assumed._

## Goal

Give an org admin a complete, governable administrative surface and produce
**compliance-grade evidence**: a complete, filterable, self-describing audit
export; durable in-app notifications for the events that matter (approvals,
runtime failures, security); and a consolidated admin console. Everything stays
tenant-scoped, admin-gated, and audited — matching Chronos's enterprise posture.

## Current state (this is gap-closing, not greenfield)

Audited against the checkout on 2026-06-23:

- **Audit log core (`core/audit.py`).** `audit.log(...)` appends tenant-scoped,
  append-only entries (RULE 6: `audit_log` is INSERT-only; DELETE/UPDATE blocked
  by a DB trigger/grant). Writes use the actor's `organization_id` (the
  tenant-isolation fix proven in `tests/test_audit_tenant_isolation.py`).
- **Audit read/export (`routers/settings.py`).** `GET /settings/audit` lists
  rows filtered by `actor`/`action`/`query` (action `ILIKE`), admin-gated via
  `permission.check(member, "list_audit_log", ...)` (`list_audit_log` is in
  `_ADMIN_ACTIONS`). `GET /settings/audit/export.csv` exists.
- **Notification *settings* exist, delivery does not.** `settings_store.DEFAULTS`
  has a `notifications` section (email/in_app/approval/runtime/security toggles),
  and `/settings` exposes `notification_email_dispatch` as **truthful-degraded**
  ("not configured"). But there is **no notification record, no feed, no
  delivery** — nothing ever creates or reads a notification. The toggles control
  a system that isn't wired.
- **Admin surface is scattered.** Admin actions (members/roles, SSO/SCIM,
  connectors, authz reconcile, audit) live across `routers/settings.py`,
  `routers/sso.py`, `routers/scim.py`, etc. There is no consolidated admin
  console; the web `/audit` route is a stub that re-exports the chat page.

**The real gaps:**
1. **The audit export silently truncates.** `export_audit` calls
   `list_audit(limit=500)`, so an org with more than 500 audit events exports
   only the most recent 500 — and ignores the active filters. For a compliance
   request ("the complete audit trail for date range X–Y"), this is a correctness
   hole: incomplete evidence presented as complete.
2. **No date-range / event-type filtering.** The defining query for a compliance
   export ("everything between two timestamps") isn't expressible.
3. **No export integrity/manifest.** A recipient can't tell whether an export is
   complete (how many rows, what range, what filters, who generated it).
4. **No notification delivery.** The settings imply notifications; none exist.
5. **No consolidated admin console.** No single governed admin surface.

## Phase decomposition

- **W5.1 — Compliance-grade audit export (DETAILED below).** Fix the truncation
  bug; add date-range + event-type filtering to both list and export; stream the
  **complete** matching set (memory-bounded, no 500 cap); offer CSV and JSON
  formats; attach a self-describing manifest (count, range, filters, generated_at,
  org, generated_by); admin-gate and audit the export itself. Minimal UI: date
  range + format on the existing audit screen.
- **W5.2 — In-app notifications: durable records + feed (DONE).** `notifications`
  table (migration `0043_notifications`, tenant-scoped; `member_id` NULL = org-wide,
  set = targeted); `core/notifications.py` (`emit`/`list_for`/`unread_count`/
  `mark_read`/`dismiss`) with emission gated by the previously-inert
  `settings_store` notification toggles and every creation audited; `/notifications`
  router (list/unread_count/read/dismiss) + `/notifications` web feed. Emission
  wired into the real event sites: approval requested (`runtime/agent_loop.py`),
  approval decided (`routers/approvals.py`), and permanent task failure
  (`runtime/task_runner.py`) — all best-effort, never breaking the host flow.
  _Proof: `tests/test_notifications.py`._
- **W5.3 — Notification delivery channels (DONE).** `core/notification_delivery.py`
  email seam (SendGrid behind config, truthful-degraded otherwise): `deliver_pending`
  sends to org admins / the targeted member and marks `emailed_at` only on real
  send (degraded leaves it untouched so nothing is silently dropped); `build_digest`
  rolls up unread notifications by type. `/notifications/deliver` (admin-gated) +
  `/notifications/digest`; the `notification_email_dispatch` settings capability now
  reflects the real provider status. _Proof: `tests/test_notification_delivery.py`._
- **W5.4 — Admin console depth (DONE).** `routers/admin.py` `GET /admin/overview`
  aggregates org / members-by-role / connectors / pending approvals / audit volume /
  unread org notifications / governance posture (OpenFGA, SSO, email delivery) into
  one admin-gated, audited landing summary; `/admin` web console renders it with
  deep links to the existing admin surfaces (connectors, approvals, audit,
  notifications). _Proof: `tests/test_admin_console.py`._

---

## W5.1 — Compliance-grade audit export (executable)

**Goal:** An admin can export the **complete** audit trail for their org,
optionally narrowed by date range / actor / action / event-type, in CSV or JSON,
with a manifest that proves completeness — and the export itself is audited.

**Environment:** Standard backend env block (migrated Postgres on :55432, the
`DATABASE_URL`/`VAULT_ENCRYPTION_KEY`/`AWS_S3_BUCKET` test values). No new
migration — `audit_log` already has every column we read.

### Task 1: Shared, filterable audit query

- [x] **Step 1 (failing test):** add `tests/test_audit_export.py` asserting that
  `list_audit` honors a new `since`/`until`/`event_type` filter (rows outside the
  range are excluded; positive control inside the range is included), tenant-scoped.
- [x] **Step 2:** in `routers/settings.py`, factor the WHERE-clause construction
  used by `list_audit` into a private `_audit_select(audit_log, *, actor, action,
  query, event_type, since, until)` returning a filtered `select(...)`. Add
  `since`/`until` (ISO-8601 → parsed to `datetime`, inclusive lower / exclusive
  upper) and `event_type` params to `list_audit`. Keep the frozen response shape.

### Task 2: Complete (uncapped) streaming export + manifest

- [x] **Step 1 (failing test):** insert **>500** audit rows for an org, then
  assert the export contains **all** of them (the truncation bug: today it caps at
  500). Assert the JSON manifest's `count` equals the number of records and the
  reported `range`/`filters` match the request.
- [x] **Step 2:** add `GET /settings/audit/export` with `format` (`csv`|`json`,
  default `csv`), `since`/`until`/`actor`/`action`/`event_type`/`query`. Stream the
  **complete** filtered set in bounded batches (keyset/offset over `created_at,id`)
  so memory stays flat regardless of row count. JSON emits
  `{"manifest": {...}, "records": [...]}`; CSV emits the existing columns and
  carries the manifest in `X-Chronos-Audit-*` response headers. Re-point the old
  `/audit/export.csv` at the same complete path (fixing its truncation) for the
  existing web link.
- [x] **Step 3:** admin-gate the export via `permission.check(member,
  "export_audit_log", ...)` and emit an `audit.log("compliance", ...,
  action="export_audit_log", ...)` entry recording who exported what range/filters
  and the row count. (`export_audit_log` is already in `_ADMIN_ACTIONS`.)

### Task 3: Minimal UI

- [x] **Step 1:** in the audit screen (`apps/web/app/chat/page.tsx`,
  `AuditSettings`), add `since`/`until` date inputs (wired into both the list fetch
  and the export query) and an **Export JSON** action alongside **Export CSV**.
  Verify with `npx next build --webpack`.

### Task 4: Proof

- [x] **Step 1:** `pytest tests/test_audit_export.py` green (completeness,
  date-range, manifest, tenant isolation, export-is-audited, format selection);
  web build green. Confirm the existing `tests/test_audit_tenant_isolation.py`
  still passes (no regression to `list_audit`).

**Proof bar for W5.1:** an admin export returns the *complete* audit trail for a
date range (proven against >500 rows), CSV and JSON both work, the JSON manifest
proves completeness, the export is itself audited, and everything is tenant-scoped
and admin-gated.

---

## Self-review notes

- W5.1 leads with the genuine correctness bug (silent 500-row truncation of
  compliance evidence) — highest value, lowest risk, no new tables. Later phases
  (notifications, admin console) add product surface and each re-confirms its gap
  against the live code first, matching the W3/W4 cadence.
- Frozen seams respected: no signature change to `audit.log`, `permission.check`,
  or `memory.retrieve`. The export reads `audit_log` through the same
  tenant-scoped path as `list_audit` and never mutates it (RULE 6 intact).
