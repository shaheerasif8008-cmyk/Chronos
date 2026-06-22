# W3 — Self-Serve Safety & Cost Governance Plan

_Workstream plan: design + phase decomposition only. Each phase's FIRST step is "audit and confirm the gap against current code" — the subsystem is substantially built and the specific gaps below are hypotheses to verify, not confirmed work. The detailed, executable plan for a phase is written when that phase is picked up._

## Goal

Make it safe for strangers to run the agent self-serve against the org's LLM keys, browser, connectors, and data: per-tenant cost/rate budgets with hard stops, secrets isolation, defenses against prompt-injection from untrusted org data, and abuse circuit-breakers. This is a launch-blocker for self-serve GA, not a scale nicety.

## Current state (substantially built — verify before building)

Already present (confirmed by reading):
- **Per-org token budget:** `core/token_budget.py` (`estimate_tokens`, daily counter in Redis) + `tool_broker._check_token_budget` enforces `settings.per_org_daily_token_limit` (0 = unlimited).
- **Per-org rate limiting:** `tool_broker._check_rate_limit` (Redis sliding window, `_RATE_LIMIT = 10/min per org`).
- **Safety limits:** `_SAFETY_LIMITS` (gmail.send max_recipients, image.generate max_count, …) enforced in the broker; CLAUDE.md lists more (finance/payment caps, always-approval tools).
- **Concurrency caps:** `settings.concurrent_sub_agents = 5`, `settings_store.max_concurrent_runtimes = 3`.
- **Abuse/trust machinery:** `core/autonomy.py` + `core/trust.py` — graduated autonomy, trust scoring, and **anomaly circuit-breakers** that collapse a trust score and revoke autonomy on a burst of a graduated action.
- **Untrusted content:** `tests/test_untrusted_content_patterns.py` exists (prompt-injection pattern tests).

So W3 is **hardening + closing specific holes + proving**, not building the system.

## Implementation status (2026-06-22)

Implemented:
- **W3.1 cost metering and budgets:** `core/governance.py` records token and estimated dollar usage per org/day, enforces token/cost hard-stops before chat/task model calls, auto-suspends an org when daily cost is exhausted, and surfaces usage through `/settings`.
- **W3.2 rate and task admission controls:** request, connector, task-queue, and concurrent-runtime checks are centralized in `core/governance.py`; chat requests, broker calls, task creation, and task startup now pass through those checks.
- **W3.4 secrets isolation:** `connectors.vault.get` now requires `org_id`, uses tenant-qualified cache keys, and queries vault rows by `(organization_id, vault_ref)` before decrypting. Gmail, generic OAuth, and connector-framework credential loaders pass the tenant through.
- **W3.5 abuse circuit-breakers:** runaway connector-loop detection and daily cost exhaustion suspend the org through the governance circuit-breaker. Suspended orgs are blocked from new requests, task creation, task startup, and model spend.
- **Usage visibility:** the settings overview includes token usage, cost usage, hard-stop/suspension state, and runtime controls for daily cost budget plus request/connector rate limits.

Proof:
- `tests/test_w3_governance.py`
- Focused regressions: `tests/test_settings.py`, `tests/test_token_efficiency.py`, selected runtime reliability tests, connector framework/operations/phase8 tests, and autonomy broker tests.

## Hypothesized gaps (MUST be audit-confirmed before planning a phase)

1. **Cost (dollars) vs tokens.** Budgets are token-count based; there may be no per-model $-cost accounting or a $-denominated cap. Verify whether real cost (model price × tokens, across providers) is tracked/enforced, or only token counts.
2. **Hard-stop coverage.** `_check_token_budget` guards tool-broker calls; verify whether *LLM/chat* calls (the main token spend) and *sub-agent/task* loops are also budget-gated, or only broker tool calls.
3. **Per-tenant rate-limit breadth.** `_RATE_LIMIT` covers broker tool calls; verify whether request-level (chat/task creation) and connector-call rates are bounded per org, and whether limits are configurable per plan/tenant.
4. **Prompt-injection depth.** Pattern tests exist; verify whether agent loops actually *defend* (sanitize/segregate untrusted org-data and tool output) vs only test for patterns.
5. **Secrets isolation.** Verify the credential vault enforces tenant boundaries (a connector cred for org A is unreachable from org B's agent context) and that `vault_ref`-only logging (Rule 7) holds everywhere.
6. **Abuse circuit-breakers for self-serve.** Trust/autonomy breakers exist for graduated actions; verify whether a *new self-serve org* (no trust history) has sane default caps and a runaway-cost/loop auto-suspend.
7. **Usage visibility.** Verify whether per-org usage (tokens/cost/rate) is exposed (admin endpoint/UI) for transparency and self-throttling.

## Phase decomposition (each starts with a gap-confirming audit)

- **W3.1 — Cost metering & per-tenant budgets.** Confirm token-only vs cost; if needed, add $-cost accounting (per-model pricing) and a $-denominated per-org budget with soft-warn + hard-stop, gating LLM/chat + task loops (not just broker). Expose per-org usage.
- **W3.2 — Per-tenant rate & concurrency limits.** Confirm coverage; extend rate limiting to request-level + connector calls; make caps per-tenant/plan-configurable; enforce concurrent task/sub-agent caps per org.
- **W3.3 — Untrusted-content / prompt-injection defense.** Confirm whether agent loops segregate untrusted org-data + tool output from instructions; harden (delimiting, provenance, allowlists) beyond pattern tests; prove with adversarial tests.
- **W3.4 — Secrets isolation.** Confirm vault tenant-boundary enforcement + no-credential-in-logs across all paths; close any cross-tenant credential reachability; prove.
- **W3.5 — Abuse circuit-breakers for self-serve.** Confirm defaults for trust-less new orgs; add runaway-cost/loop auto-suspend + admin alerting; safe defaults so a stranger can't burn the keys before a human notices.

## Sequencing note

W3.1 (cost hard-stop) and W3.4 (secrets isolation) are the two true launch-blockers for self-serve — a stranger burning the org's LLM budget or reaching another tenant's credentials are the worst outcomes. Recommend leading with those once W2 lands. W3.3 (injection) and W3.5 (abuse breakers) are close behind.

## Self-review notes

- This plan stays at decomposition depth deliberately: the gaps above are **hypotheses** drawn from a quick read, and `autonomy.py`/`trust.py`/`token_budget.py`/the vault already do more than a detailed plan could assume. Writing bite-sized tasks now would risk planning against gaps that don't exist. Each phase's executable plan is written after its audit step confirms the real gap.
