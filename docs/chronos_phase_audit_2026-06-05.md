# Chronos Phase 0–13 Audit & Launch-Readiness Report

_Date: 2026-06-05 · Auditor: Claude (Opus 4.8) · Method: read goal + matrix, then verify against code/tests/migrations empirically (not trusting self-reported status)._

## How this was verified

- Read `CHRONOS_TOTAL_PARITY_GOAL.md` and the controlling contract `docs/chronos_total_parity_matrix.md`.
- Ran the backend test suite (`apps/api/tests`, 53 files) against an isolated Postgres test DB.
- Reproduced the Alembic migration chain on a clean database twice.
- Inventoried web routes (`apps/web/app/*/page.tsx`) and migrations (`apps/api/migrations/versions`).

**Caveat on evidence quality:** my verification is overwhelmingly **backend pytest**. I did **not** run the Next.js production build or any Playwright E2E. Several matrix rows marked "Done" rest on `npm run build` typechecks or `*-static.spec.ts` route guards rather than behavioral UI proof. Where I cite UI/E2E results (chat, memory, artifacts, research specs), those are the matrix's own claims, not independently re-run here.

## Headline findings

1. **🔴 Clean-deploy blocker (proven): `alembic upgrade head` fails on a fresh database.** Migration revision id `0030_phase12_scheduled_workflows_monitors` (41 chars) overflows Alembic's default `alembic_version.version_num VARCHAR(32)`, raising `StringDataRightTruncationError`. Reproduced on two fresh DBs. The DDL itself is sound — widening the column to `varchar(128)` lets `upgrade head` complete and creates all Phase 12/13 tables. Net effect: **a brand-new Chronos org cannot finish its migrations**, so the Phase 12 workflow/monitor tables never get created out of the box. Trivially fixable (shorten the revision id or pre-widen the column).
   - Knock-on: Phase 12's "Done via pytest" is **weak proof** — those tests go green against a DB that lacks the 0030 migration (the suite self-provisions or the tables pre-exist), so "tests pass" ≠ "app migrates."

2. **🔴 Core enterprise governance guarantee is unproven: "unauthorized user cannot approve a risky write."** The matrix itself admits this (`permission.check` only enforces with OpenFGA, which is **off** in this environment; the permission seam is "Foundation present"). The *authorized* approve→resume path is proven; the *negative* path — the central enterprise safety claim — is not. For an enterprise launch this belongs next to the migration blocker.

3. **🟡 Test suite is not green out of the box.** Against a correctly-migrated DB: **436 passed, 32 failed, 1 skipped.** The 32 break down as:
   - **~20 environment-only** — missing optional libs (`pypdf`, `docx`, `openpyxl`, `pptx`, `pandas`, `aiosqlite`). All are in `requirements.txt`; failures are because the audit ran on system Python, not the project venv. Not product defects.
   - **~7 stale-test / drift** — fakes that don't accept the newer `reasoning_effort` kwarg (`test_runtime_sprint4`, `test_orchestration_category1`), and `test_project_instructions` expecting `send_message` to work without a model (code now requires one). Product code is fine; the tests are stale. Still, the "automated proof" pillar requires a green suite, so this is hygiene debt.

4. **🟢 The matrix under-claims Phase 3.** The Projects row says "Missing as first-class surface," but migrations 0018/0020/0023, an `/app/projects` route, and `test_projects.py` + `test_project_sources.py` + `test_source_indexing/sync/viewer.py` (**68 passed**) all exist and pass. Projects/sources are substantially implemented; the matrix text is stale.

## Phase-by-phase verdict (0–13)

| Phase | Title | Verdict | Evidence / caveat |
|---|---|---|---|
| 0 | Acceptance matrix | ✅ Present | Matrix exists with tagged rows; but it has **drifted** (under-claims Projects, over-relies on typecheck proof). |
| 1 | Runtime reliability | ✅ Backend-proven | Queue, dead-letter, cancellation, timeouts/retries, crash recovery, durable trace, idempotent writes — `test_runtime_reliability_phase1.py` passes. Single-instance by design (no distributed leases — intentional per CLAUDE.md). |
| 2 | Unified shell | 🟡 Mostly | All 18 surfaces exist as routes (chat, projects, research, tasks, artifacts, memory, connectors, agents, workflows, approvals, activity, audit, computer, browser, coding, data, settings). **Gaps: global/conversation search (matrix: Missing) and command palette.** |
| 3 | Projects & sources | ✅ Substantial | Upload/index/sync/viewer/retrieval all pass (68 tests). Matrix's "Missing" is stale. |
| 4 | Memory parity | ✅ Backend-proven | Control center, conflict/staleness, privacy controls, usage logs — `test_memory_parity.py` passes. Note: conversation-level disable enforced on write path only (frozen `retrieve` signature). |
| 5 | Artifact workspace | ✅ Proven (best-covered) | Create/version/diff/restore/publish; backend + a real UI E2E (`artifacts.spec.ts`, per matrix). |
| 6 | Deep research | ✅ Proven | Durable runs, citation-requires-snippet invariant, report artifact; matrix cites a live full-stack run + `research.spec.ts`. |
| 7 | Multimodal & data | 🟡 Done but "honest-degraded" | Doc intelligence, vision, data sandbox proven; **image gen/edit, voice STT/TTS are stubbed honest-degraded** (no provider keys in env) — wiring proven, live output not. |
| 8 | Connectors | 🟡 Framework-proven | Catalog + framework install/action/policy/health/audit proven deterministically. **Live provider calls (Drive/Calendar/Slack/etc.) are credential-dependent** and not exercised. |
| 9 | Browser operator | 🟡 Backend + degraded | Session manager, takeover, consent, revocation persist and pass; **if Playwright isn't installed in the API runtime it reports degraded instead of live control.** |
| 10 | Cloud/local computer | 🟡 Backend-proven | Sandbox jail, command audit, approval-gated local bridge pass; desktop bridge is API-runtime degraded (no packaged app). |
| 11 | Coding agent | 🟡 Local-proven | repo.* clone/branch/edit/test/diff/commit/PR/review pass locally; **live private GitHub clone + real PR publish are credential-bound** (local approval-gated artifact only). |
| 12 | Scheduled/workflows/monitors | 🟠 Code present, **migration blocked** | Logic tests pass, but tables come from migration 0030 which **fails on clean deploy** (finding #1). Live provider polling deferred to schedules/connectors. |
| 13 | Agents & publishing | ✅ Backend-proven | agent_profiles, templates, runs-as-governed-tasks, publication inbound bridge pass; **live Slack/Teams/email delivery is credential-dependent.** |

Phases 14–17 (collaboration/comments, admin/RBAC, audit export, mobile, desktop, notifications, billing, deep polish) are explicitly **Missing/partial** in the matrix and out of scope for this 0–13 audit — but they matter for "launch-ready" (see below).

## What Chronos can do today (well-grounded)

- Streaming governed chat with model/mode selection, rich message metadata, and message controls (edit/branch/regenerate/pin/convert-to-task).
- Scoped enterprise memory with a control center, conflict detection, and privacy gating.
- Durable artifacts with non-destructive versioning, diff/restore, sandboxed renderers, and revocable share links.
- Deep research runs producing cited report artifacts (web + project + indexed-connector sources), with a no-citation-without-snippet invariant.
- Reliable single-instance task runtime: priority queue, dead-letter, cancellation, timeouts, crash recovery, idempotent writes, replayable traces.
- Projects with uploaded/indexed/cited sources.
- A broad connector **framework** (catalog, policy, approval-gated writes, audit, health) and MCP/HTTP registration.
- Backend seams for browser operation, cloud/local computer sandboxes, a coding agent, agent profiles, and scheduled work.

## What it can't do / isn't proven

- **Migrate a fresh deployment** (finding #1) — the most concrete blocker.
- **Enforce the enterprise approval guarantee** — unauthorized-approve is unproven (OpenFGA off).
- **Live external actions** at the product level — image/voice generation, real connector calls, live browser control, private GitHub PRs, and real Slack/Teams/email delivery are all stubbed or credential-gated, reported as honest-degraded. The *governance/wiring* is proven; the *live integrations* are not.
- **Collaboration & admin** — shared conversations, comments/mentions, RBAC depth, audit/compliance export, notifications: Missing/partial.
- **Global search & command palette** (Phase 2 gaps).
- **Behavioral UI proof at scale** — most "Done" rows are backend pytest + typecheck; full refresh-survival/E2E coverage is thin and not independently verified here.

## Launch-readiness verdict

**Not launch-ready as a self-serve enterprise product; close to a credible gated/design-partner pilot once two blockers clear.**

The architecture is real and disciplined — the three seams (permission/broker/memory) are respected, durable state and audit are pervasive, and the backend capability surface for phases 0–13 is genuinely broad and mostly test-backed. This is well past "demo-ware."

But the goal's own bar ("works through real UI/API, persists, survives restart, emits audit, automated proof") is **not met end-to-end** because:

1. A clean deploy can't migrate (trivial fix, but currently blocking).
2. The defining enterprise guarantee (unauthorized can't approve) is unproven.
3. The "automated proof" pillar is undercut by a non-green suite and heavy reliance on backend-only/typecheck evidence.
4. Most outward-facing integrations run in honest-degraded mode — defensible for a governed pilot, not for parity marketing.

## Fixes Applied (2026-06-05, same session)

All five recommended fixes were implemented in this branch.

1. **Migration blocker — FIXED & proven.** Revision id shortened to `0030_phase12_sched_wf_monitors` (≤32 chars) and the file renamed. `alembic upgrade head` now completes on a brand-new database (verified on three fresh DBs) and creates the workflows/monitors/agent_profiles tables.
2. **Approval guarantee — FIXED & proven.** `core/permissions.py` now denies approval decisions to non-approver roles (`_APPROVER_ROLES = {admin, owner, approver}`) deterministically, independent of OpenFGA — so the guarantee holds even with the policy engine off. New negative test `test_unauthorized_role_cannot_decide_approval` proves a same-org `role="user"` member gets HTTP 403, the approval stays `pending`, and no resume fires; the positive path was updated to decide as an admin (matching the seeded admin). The seed admin and the web E2E (seeded admin) are unaffected.
3. **Green suite — FIXED.** Stale fakes updated to accept the `reasoning_effort` kwarg (`test_runtime_sprint4`, `test_orchestration_category1`); `FakeReq` given a valid model + `reasoning_effort`; `citations_payload` expectation updated for the `source_type` field; `pytest.ini` now sets `testpaths=tests`/`norecursedirs` so the coding-agent fixture repos (intentionally red) aren't collected. Full suite: **469 passed, 1 skipped** against a clean-migrated DB.
4. **CI — ADDED.** `.github/workflows/ci.yml` with `backend-tests` (Postgres pgvector + Redis → `alembic upgrade head` → `pytest`; the migrate step also guards regression #1), `web-build` (`npm ci` + `npm run build`), `e2e-static` (9 self-contained route-guard specs — validated locally, all pass), and a secret-gated `e2e-behavioral` job for the model-dependent specs. The three non-secret jobs were validated locally.
5. **Browser off degraded mode — DONE & proven; connectors plumbed.** Playwright + Chromium installed in the API runtime; the broker-routed `browser.navigate` returns `status="active"` with a real page title (verified live). Captured as a durable, skip-guarded test `test_browser_live.py`. `scripts/dev.sh` now runs `playwright install chromium` so dev/deploy get a live browser by default; the behavioral CI job installs Chromium into the API venv. Connector/model credential plumbing already exists in `.env.example` (OpenRouter model key wired+proven; Google OAuth placeholders present) — **live third-party OAuth connector smoke still requires operator-supplied credentials** (the one piece that cannot be completed without a real secret).

**Net post-fix state:** the two hard blockers (clean-deploy migration, approval guarantee) are cleared and proven; the suite is green; CI exists; the browser runs live. Remaining launch gaps are unchanged: live third-party connector credentials, behavioral UI/E2E breadth, and Phases 14+ (collaboration/admin/compliance).

### Recommended pre-launch order

1. Fix migration 0030 revision id (or widen `alembic_version`) and verify `alembic upgrade head` on a clean DB. **(blocker, hours)**
2. Turn on OpenFGA and prove the unauthorized-approve negative path. **(blocker for enterprise positioning)**
3. Get the test suite green in the project venv; fix the `reasoning_effort`/`model-required` test drift. **(proof integrity)**
4. Stand up the Playwright parity suite in CI for refresh-survival proof on the "Done" rows.
5. Wire real credentials for 1–2 flagship connectors + browser/Playwright in the API runtime to move flagship flows off degraded mode.
6. Then decide whether collaboration/admin (Phases 14+) is needed for the target buyer before GA.
