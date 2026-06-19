# Chronos — Graduated Autonomy & Trust Ledger

Status: **design + initial vertical slice implemented**
Audience: regulated-enterprise buyers (vertical-agnostic, compliance-grade)
Branch: `claude/gifted-goldberg-kj9qq4`

---

## Why this is the wedge

Chronos already has the *static* half of governance: a `PolicyEngine` with risk
levels and approval modes, a binary `workspace_autonomy` (`supervised` /
`full_auto`), hard-coded safety limits, and an `ApprovalService`. What it lacked
is the *dynamic, learning* half — autonomy that is **earned, risk-priced, and
evidenced**, and that *tightens itself* from experience.

Graduated Autonomy makes the answer to a regulator's core question —
*"why was the AI allowed to do this unattended?"* — a row in a table:
a named human (or an audited `system` graduation) raised a specific
`(workspace × action_class)` to a specific risk ceiling, backed by an
append-only evidence trail. No competitor has the approval data or the audit
spine to reproduce this.

> The phrase that sells it: **autonomy where it's cheap, evidence where it's dangerous.**

---

## Decisions (locked)

| Decision | Choice |
|---|---|
| Graduation model | **Hybrid by risk tier** — LOW auto-graduates; MEDIUM needs a named human; HIGH never auto-graduates |
| Risk factor source | **Inference + registry override** — a standalone registry (not the model-facing tool schemas), seeded by inference, admin-overridable |
| Trust scope | **Workspace-isolated** — autonomy never leaks across teams |
| Cold start | **Seed low-risk defaults** — truly reversible, no-external-effect drafts auto-execute on day one; everything else earns from zero |

---

## The three components

All three plug into the existing seams; **no seam signature changes** and the
broker's hard floor + safety ceiling remain absolute.

### 1. Risk Pricer — `apps/api/core/risk.py`
Pure function. Prices a call in `[0,1]`; never decides allow/deny.

```
risk = 0.30·blast_radius + 0.30·irreversibility + 0.20·data_class
     + 0.10·magnitude + 0.10·novelty
```
- `blast_radius`, `irreversibility` — per-provider base, adjusted by the verb
  (read ↓, draft ↓↓, destructive ↑). From the registry; admin-overridable later.
- `data_class` — regulated identifiers (SSN/card) detected in args raise risk.
  Emails are deliberately excluded (ubiquitous, not regulated on their own).
- `magnitude` — recipients / $ amount / record count, normalized.
- `novelty` — supplied by the ledger; 1.0 = never seen (cold start).

`action_class = tool[:partition]` so *"email a colleague"* and *"email a list"*
earn trust **separately** (`gmail.send:single` vs `gmail.send:bulk`).
Tiers: `≤0.30 low`, `≤0.70 medium`, `>0.70 high`.

### 2. Trust Ledger — `apps/api/core/trust.py`
Two stores (migration `0036_graduated_autonomy`):
- `trust_levels` — mutable EWMA standing per `(scope × action_class)`.
- `trust_events` — **append-only** evidence (REVOKE UPDATE/DELETE + reject
  trigger, same posture as `audit_log`).

Earned slowly, lost instantly:
```
trust_score ← (1-α)·trust_score + α·value(outcome)     α = 0.15
value: auto_success 1.0 | approved 0.7 | rejected/incident/reverted 0.0
on negative outcome → score capped at 0.3, auto_threshold → NULL  (circuit breaker)
```
`approved = 0.7` (a human *had* to step in) is weaker evidence than unattended
`auto_success = 1.0`.

Hybrid graduation:
- **LOW** — auto-graduates at `score ≥ 0.8`, `successes ≥ 20`, `rejections == 0`
  (`graduated_by = 'system'`, fully audited).
- **MEDIUM** — accrues score; a *named human* must set `graduated_by`.
- **HIGH** — never auto-graduates.

All DB access is wrapped: if the ledger is unavailable, the broker still works —
cold-start defaults apply and recording is a no-op. **Trust can only loosen
governance, never break it.**

### 3. Autonomy Gate — `apps/api/core/autonomy.py`
The one new decision point. Order (supervised workspaces):
1. `full_auto` → allow (legacy collapse of the settings gate).
2. Human-ratified **learned policy** match → block (guardrails win over trust).
3. Settings policy doesn't require approval → allow (unchanged baseline).
4. HIGH tier → approval.
5. Earned graduation: `risk.value ≤ trust.auto_threshold` (MEDIUM also needs a
   human `graduated_by`) → allow.
6. Otherwise → approval.

### Broker integration — `apps/api/core/tool_broker.py`
One step inserted between the existing approval gate and the audit/execute steps;
the old binary `approval_required` line is replaced by the gate. After a
successful call the outcome is fed back to the ledger
(`auto_success` / `approved`). The hard floor (`_ALWAYS_APPROVAL_TOOLS`) and
`_check_safety_limits` are untouched and run first.

---

## Learned policies (from "no")

A rejection with a note is the highest-signal training data available. On
rejection: append a negative `trust_event`, then mint a `learned_policies`
**proposal** (an LLM extracts a structured matcher from the note + args). It is
**not enforced** until a named human confirms it (`ratified_by`). It then runs
*before* trust in the gate — a learned `deny` always beats earned trust, and an
auditor gets a complete answer: *this person, this rejection, this date, these
words.*

Table is included in migration `0036`; the rejection→proposal synthesis and the
admin confirmation UX are the **next implementation step** (see below).

---

## What's implemented (complete)

**Core engine**
- Migration `0036_graduated_autonomy` — `trust_levels`, `trust_events`
  (append-only), `learned_policies`. Migration `0037_risk_overrides` —
  admin-editable risk registry. Single clean alembic head.
- `core/risk.py` — Risk Pricer; now consumes registry overrides + ledger novelty.
- `core/trust.py` — ledger (EWMA, demotion, hybrid graduation) + admin ops
  (`list_levels`, `list_proposals`, `set_graduation`, `demote`),
  `recent_event_count` + `novelty_from_successes` for anomaly/novelty.
- `core/autonomy.py` — gate with learned-policy veto and the **anomaly
  circuit-breaker** (burst of a graduated action → synthetic incident + re-gate).
- `core/learned_policy.py` — rejection→matcher **synthesis** (LLM, best-effort) +
  propose/confirm/disable lifecycle. Enforced only after a named human ratifies.
- `core/risk_registry.py` — TTL-cached per-org override loader + CRUD.
- `core/evidence.py` — hash-chained, HMAC-signed **evidence bundles** over
  `trust_events`, with offline `verify()`.
- `tool_broker.py` — gate step + overrides/novelty pricing + outcome recording.

**Admin/approver API** (`routers/autonomy.py`, mounted at `/autonomy`)
- `GET /trust` — trust dashboard. `GET /proposals` — graduations awaiting a human.
- `POST /graduate` · `POST /demote` — ratify / revoke (admin, audited).
- `GET /learned-policies` · `POST /{id}/confirm` · `POST /{id}/disable`.
- `GET /risk-overrides` · `PUT /risk-overrides`.
- `GET /evidence?scope=&action_class=` — signed evidence bundle export.

**Rejection wiring** — `routers/approvals.py:decide_approval` now feeds the ledger
(`approved`/`rejected`) and, on rejection, proposes a learned policy from the note.

**Tests** — `tests/test_graduated_autonomy.py` (27 cases: pricer, gate, anomaly,
novelty, overrides, matcher, evidence chain/sign/verify). Pre-existing
`test_autonomy_broker` + `test_skill_broker` still green.

## Remaining surface (frontend only)

The backend + APIs for the admin UX are complete. The Next.js pages that consume
them (trust dashboard, proposal/learned-policy review screens) are the one
remaining piece and were intentionally left to follow the web app's conventions.
