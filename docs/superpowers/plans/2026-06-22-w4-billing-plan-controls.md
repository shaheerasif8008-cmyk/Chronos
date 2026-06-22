# W4 — Billing & Plan Controls Plan

_Workstream plan: design + phase decomposition. W4.1 is detailed and executable; later phases are scoped._

## Goal

Give each org a **plan** with enforced **entitlements** (seats, daily cost budget, feature gates), an admin view of plan + usage, and a billing-provider seam that is truthfully degraded until a provider (Stripe) is configured — so self-serve orgs can be metered and gated, and commercialization can land without blocking on a live payment account.

## Current state

- `organizations.plan` exists (default `'trial'`); `settings.py` surfaces it read-only (`_current_org` → `plan`).
- Billing is honestly degraded: `settings.py` returns `_unsupported("No billing provider is configured.")` for the billing capability.
- **W3 governance already has the budget hook:** `core/governance.py` reads per-org `daily_token_limit` / `daily_cost_limit_usd` from a settings runtime doc and enforces hard-stops + auto-suspend. W4 can drive those values from the plan.
- Invitations (`/settings/invitations`) count members (`_current_org` → `seats = member_count`) but enforce **no seat cap**.
- No plan/entitlements model, no subscription lifecycle, no provider integration.

## Design

A static **entitlements map** keyed by plan tier (in `core/plans.py`), the org's tier read from `organizations.plan`. Entitlements: `max_seats`, `daily_cost_limit_usd`, `daily_token_limit`, and a `features` set. Enforcement points:
- **Seats:** invite/activation checks `active members + pending invites < max_seats`.
- **Budgets:** the plan's budget values become the defaults `core/governance.py` uses (org-level overrides still win where allowed).
- **Features:** a `has_feature(org_plan, feature)` helper gates feature-flagged endpoints.

The payment provider is a **seam** (`core/billing.py`) added in W4.2 — truthfully degraded when unconfigured (matching the repo's honest-degraded posture); a webhook syncs `organizations.plan` from the provider. No live Stripe needed for W4.1.

## Phase decomposition

- **W4.1 — Plan & entitlements model + enforcement (DETAILED below).** `core/plans.py` entitlements map + `get_entitlements(org)`; seat-cap enforcement on invite; plan → governance budget wiring; `has_feature` gate; `GET /settings/plan` (plan + entitlements + usage). Admin can change plan via an internal/admin path (no provider yet).
- **W4.2 — Billing provider seam + subscription lifecycle.** `core/billing.py` provider seam (Stripe behind config, truthful-degraded otherwise); checkout/portal links; webhook → `organizations.plan` sync; audited.
- **W4.3 — Usage-based metering → billing.** Tie W3 cost metering to billing usage records / overage reporting.
- **W4.4 — Billing UI.** Settings billing page: current plan, usage vs entitlements, upgrade/manage (provider portal link or truthful-degraded notice). _Carried from W4.1 review: the runtime-settings UI's "Token/Cost budget daily" inputs now render blank (the keys were removed from `DEFAULTS["runtime"]` so plan budgets take effect); W4.4 should display the **plan-derived effective budget** there instead of empty, so admins see the real value._

---

## W4.1 — Plan & entitlements model + enforcement (executable)

**Environment:** standard backend env block; `python3.11`; fresh DB for authoritative runs.

Read first: `apps/api/routers/settings.py` (`_current_org`, `require_admin`/`ADMIN_ROLES`, `create_member_invitation`, the settings-doc helpers), `core/governance.py` (how `daily_token_limit`/`daily_cost_limit_usd` are resolved per org), and `core/invitations.py` (`list_invitations`). Match their patterns.

### Task 1: `core/plans.py` — entitlements model

- [ ] **Step 1 (failing test):** create `tests/test_plans.py` asserting: `get_entitlements("trial")`, `get_entitlements("pro")`, `get_entitlements("enterprise")` return dataclasses with the expected `max_seats`/`daily_cost_limit_usd`/`features`; an unknown plan falls back to `trial`; `has_feature("pro", <pro-feature>)` is True and a trial-only check is False.
- [ ] **Step 2:** create `core/plans.py`:
```python
"""Plan tiers and entitlements (W4). Static map; the org's tier is organizations.plan."""
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Entitlements:
    plan: str
    max_seats: int
    daily_cost_limit_usd: float
    daily_token_limit: int
    features: frozenset[str] = field(default_factory=frozenset)

_PLANS: dict[str, Entitlements] = {
    "trial":      Entitlements("trial", max_seats=3,  daily_cost_limit_usd=5.0,   daily_token_limit=200_000,  features=frozenset({"chat", "projects"})),
    "pro":        Entitlements("pro",   max_seats=25, daily_cost_limit_usd=100.0, daily_token_limit=5_000_000, features=frozenset({"chat", "projects", "connectors", "sso"})),
    "enterprise": Entitlements("enterprise", max_seats=10_000, daily_cost_limit_usd=0.0, daily_token_limit=0, features=frozenset({"chat", "projects", "connectors", "sso", "scim", "audit_export"})),
}
# enterprise: 0 budget == unlimited (matches governance's "0 == unlimited" convention).

def get_entitlements(plan: str | None) -> Entitlements:
    return _PLANS.get((plan or "trial").lower(), _PLANS["trial"])

def has_feature(plan: str | None, feature: str) -> bool:
    return feature in get_entitlements(plan).features
```
- [ ] **Step 3:** run `tests/test_plans.py` → PASS. Commit.

### Task 2: Seat-cap enforcement on invite

- [ ] **Step 1 (failing test):** HTTP test — an org on `trial` (max_seats=3) with 3 active members + 0 pending: `POST /settings/invitations` returns **402** (or 403) "Seat limit reached for the trial plan"; on `pro` it succeeds. Seed org with a chosen `plan`.
- [ ] **Step 2:** in `create_member_invitation` (settings.py), before creating the invite, resolve the org's plan (`organizations.plan`), count active members + pending invitations, and reject when `count >= entitlements.max_seats`. Use `get_entitlements`. Return a clear 402 with the plan name. (Confirm the existing duplicate-member 409 check stays.)
- [ ] **Step 3:** run the invite tests + existing `tests/test_invitations.py` → PASS (seed those orgs on a plan with enough seats if needed). Commit.

### Task 3: Plan drives the governance budget

- [ ] **Step 1 (failing test):** assert that for an org whose `plan` is `trial`, `core/governance.py`'s resolved daily cost/token budget equals the trial entitlement **when no explicit per-org override is set** — i.e. the plan supplies the default. (Read governance's resolution first; if it currently defaults to `settings.per_org_daily_token_limit`/0, the change is: fall back to `get_entitlements(org.plan)` values before the global default.)
- [ ] **Step 2:** wire `core/governance.py`'s budget resolution to use `get_entitlements(org_plan)` as the default (explicit settings-runtime override still wins). Keep the "0 == unlimited" convention. Minimal change; don't disturb the existing override path.
- [ ] **Step 3:** run `tests/test_w3_governance.py` + the new test → PASS. Commit.

### Task 4: `GET /settings/plan` (plan + entitlements + usage)

- [ ] **Step 1 (failing test):** `GET /settings/plan` (any member) returns `{plan, entitlements: {...}, usage: {seats_used, tokens_today, cost_today_usd}}`. Seed an org + members; assert seats_used == member count and the entitlements match the plan.
- [ ] **Step 2:** add the endpoint to settings.py, composing `get_entitlements(org.plan)`, the member count (reuse `_current_org` logic), and the governance usage readout (reuse whatever `core/governance.py` exposes for tokens/cost today). Read-only.
- [ ] **Step 3:** run → PASS. Commit.

### Task 5: Regression gate

- [ ] Fresh-DB full suite → only the known `test_doc_authoring` optional-import failures; zero new. (Watch `test_invitations.py`/`test_settings.py` — the seat cap may need their seed orgs on a roomy plan.)

**Proof bar for W4.1:** plan entitlements enforced on seats + budgets, feature gate available, plan/usage visible via API; suite green on a fresh DB. Commercialization gating works without any payment provider.

## Self-review notes

- W4.1 deliberately needs **no external provider** — it's the plan/entitlements/enforcement core, fully testable. The Stripe seam (W4.2) is honest-degraded until configured, matching the codebase. Budgets reuse the W3 governance hook rather than a parallel system. Seat enforcement reuses the existing invitation path. No frozen-seam changes.
