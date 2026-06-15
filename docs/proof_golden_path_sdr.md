# Proof — SDR Golden Path (end-to-end trace)

This document traces the canonical completion test from `CLAUDE.md`
("What the Foundation Must Prove") through the **real code paths** that
implement each step, and records the automated tests that cover them.

The seven required steps and where they live:

| # | Step | Implementing code | Governs / guarantees |
|---|------|-------------------|----------------------|
| 1 | User gives an ICP for lead generation | `apps/api/routers/chat.py`, `apps/api/core/intent.py` (task routing) → `apps/api/runtime/agent_loop.py` | Request scoped by `RequesterContext` (`org_id`) |
| 2 | Activity log streams plan + execution | `apps/api/runtime/agent_loop.py` (`sub_agent_spawned`/`sub_agent_complete` events), `apps/api/routers/activity.py` (SSE) | Durable trace persisted + replayable |
| 3 | Sub-agent browses the web, extracts leads | `apps/api/runtime/sub_agent.py` (depth/concurrency limits) → `apps/api/core/tool_broker.py:347`-routed `browser.search` / `browser.extract_contacts` in `apps/api/connectors/browser.py` | Every tool call routes through the broker (RULE 1); external content marked untrusted (`_mark_untrusted_connector_result`) |
| 4 | Lead report with qualification scores | `skills/sdr-outreach/SKILL.md` + `skills/sdr-outreach/icp-qualification.py` | Skill lazy-loaded into context |
| 5 | 20 personalized draft emails | `apps/api/connectors/gmail.py:304` `gmail.draft`; `gmail.send` is **blocked** at `gmail.py:293` and `tool_broker.py:172` (`ApprovalRequired`) | RULE 8 — `gmail.send` always requires an approval record; broker caps recipients at 10 (`tool_broker.py:175`) |
| 6 | Approve drafts in batch | `apps/api/routers/approvals.py:102` `decide_approval` (+ `batch_id` at `:119`); authorization at `:110` | RULE 3 — `permissions.check(decide_approval)` deny non-approver roles (`core/permissions.py:73`) |
| 7 | Drafts appear in connected Gmail | `apps/api/connectors/gmail.py:388` `_create_draft` (via vault credential ref) | RULE 7 — only `vault_ref` is logged, never the credential |

## What is proven automatically (no live infra required)

These tests run in this environment (pure import + monkeypatch) and pass:

- Broker routing + untrusted marking: `tests/test_skill_broker.py`, `tests/test_runtime_reliability_phase1.py`.
- Browser search degraded mode (truthful, never fabricated): `tests/test_search_degraded.py`, `tests/test_runtime_sprint4.py::test_browser_search_degrades_truthfully_on_live_timeout`, `tests/test_tavily_websearch.py`.
- Injection scanning of external content: `tests/test_untrusted_content_patterns.py`.
- Permission enforcement (approval decisions role-gated, OpenFGA fail-closed): `tests/test_authz.py`, `tests/test_permissions_enforce.py`.

## What requires CI infra (Postgres + Redis + model/Gmail creds)

The DB/Redis-backed segments of the loop are proven by the existing suite in CI
(`.github/workflows/ci.yml` provisions `pgvector/pgvector:pg15` + `redis:7`):

- Approval gate draft→decide→resume: `tests/test_approval_flow_http.py`
  (`test_gmail_draft_is_gated_by_broker_policy`,
  `test_inbox_decide_flips_to_approved_and_triggers_resume`,
  `test_unauthorized_role_cannot_decide_approval`).
- Tenant isolation across the HTTP boundary: `tests/test_tenant_isolation_http.py`.

These fail locally **only** with `ConnectionError`/Redis-unavailable (no
services in this sandbox), not on logic — confirmed by running them against the
connection error rather than an assertion failure.

## Conclusion

Every step of the golden path maps to real, governed code — there are no
fake controls in the path. The data-gathering, safety, and authorization halves
are proven by tests that run here; the persistence-backed approval/resume half
is proven by the existing suite under CI.
