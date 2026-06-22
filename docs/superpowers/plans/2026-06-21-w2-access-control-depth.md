# W2 — Access-Control Depth (OpenFGA) Plan

_Workstream plan: design + phase decomposition. Phase 1 is detailed and executable; later phases are scoped, not yet step-by-step._

## Goal

Make relationship/group/scope-based authorization **real and proven** — the "unauthorized user cannot act on a resource they don't have a relationship to" guarantee — across the resource surface, via OpenFGA, without changing the frozen `permission.check(actor, action, resource)` signature.

## Current state (this is gap-closing, not greenfield)

The permission seam (`apps/api/core/permissions.py`) is mature:
- **Role-gates, always-on (no FGA needed):** `_APPROVAL_DECISION_ACTIONS` (decide_approval, resolve_connector_approval) and `_ADMIN_ACTIONS` (~30 governance/connector/SSO actions) deny non-approver/non-admin human actors. **This half was proven in W1** (the negative-path 403s).
- **Relationship checks (OpenFGA):** `_resource_for(action)` maps project actions (`view_project`/`update_project`/… → `can_view`/`can_edit`/`can_manage` on `project`) and workspace actions to FGA relations; `check()` calls `authz.check(user, relation, object)` and **fails closed** when the model denies or the server is unreachable.
- **Tuple lifecycle:** `grant_org_membership`, `grant_project_access`, `grant_workspace_access` + `revoke_*` write/delete tuples idempotently (no-ops when FGA off). `core/authz.py` is the OpenFGA client; the model/store are resolved/written at bootstrap.
- **Generic allowlist:** unmapped actions are **denied** (`denied_unmapped`) UNLESS in `_GENERIC_ALLOWED_ACTIONS` (broad: `chat`, `create_task`, `create_memory`, `create_project`, `list_*`, `cancel_*`, …) or matching `_GENERIC_ALLOWED_PREFIXES` (`use_tool:`, `connect_`, `disconnect_`), which pass through as `granted_stub`.

**The real gaps:**
1. **The FGA-enabled relationship path is unproven.** With `openfga_api_url` empty (default + the test env), `enforce` is False and every mapped action returns `granted_stub` — the relationship checks are **never exercised by the suite**. This is the W1 audit's still-open half ("OpenFGA off → unauthorized-* on *resources* unproven").
2. **Coverage breadth.** Many org-wide actions (create/list/cancel, tool use) pass through the generic allowlist with no relationship/role scoping. Some are legitimately org-wide; others (e.g. acting on a *specific* task/memory/connector by id) arguably warrant resource-relationship checks.
3. **Groups/departments.** SCIM groups map to a single effective role today; FGA group/department relations for scoped access aren't modeled.
4. **Memory-scope enforcement.** `memory.retrieve` filters by `organization_id` only; the CLAUDE.md-noted "Phase 3 replacement" (authorized scope pairs via FGA) isn't wired.
5. **Enablement & backfill.** Turning FGA on for an existing org requires backfilling tuples for current members/projects/workspaces; no productized reconciliation.

## Phase decomposition

- **W2.1 — Enable + prove the relationship path (DETAILED below).** Stand up OpenFGA (already in `docker-compose.yml`), enable it in a test config, and prove the negative relationship paths (unauthorized project/workspace access denied; fail-closed when the server is down; authorized access allowed). Emit a mapped-vs-unmapped action inventory. This proves the moat's unproven half.
- **W2.2 — Coverage expansion.** Using W2.1's inventory, map high-value resource-scoped actions (act-on-specific-task/memory/connector/artifact by id) to FGA relations + tuple seeding on resource creation. Each task starts by confirming the action is genuinely under-scoped.
- **W2.3 — Groups & departments.** Model FGA group/department relations; reconcile SCIM groups → group tuples; effective access via group membership.
- **W2.4 — Memory-scope enforcement.** Replace `memory.retrieve`'s org-only filter with FGA-authorized scope pairs (the frozen-signature drop-in noted in CLAUDE.md), proven with negative-path tests.
- **W2.5 — Enablement & backfill.** Productize turning FGA on for an existing org: a reconciliation job that backfills membership/project/workspace tuples from the DB, idempotent and audited; a documented two-step enable.

---

## W2.1 — Enable + prove the relationship path (executable)

**Goal:** Prove, in CI, that with OpenFGA enabled an unauthorized actor is denied a project/workspace relationship action (and fail-closed when the server is down), and produce a coverage inventory.

**Environment:** OpenFGA is in `docker-compose.yml` (`openfga/openfga:latest`, ports 8080/8081/3010). Tests that exercise the enabled path must point `openfga_api_url` at a running OpenFGA and bootstrap the model/store. Standard backend env block otherwise.

### Task 1: A test fixture that enables OpenFGA against a live server

- [ ] **Step 1:** Confirm OpenFGA reachability and how the model/store bootstrap works — read `core/authz.py` for `is_enabled()`, the bootstrap (store/model creation), `check()`, `write_tuples`/`delete_tuples`, and how `settings.openfga_api_url/store_id/model_id` are consumed. Confirm `docker-compose up -d openfga` exposes `:8080`.
- [ ] **Step 2:** Add a pytest fixture (in a new `tests/test_authz_enforced.py`, marked so it's skipped when OpenFGA isn't reachable) that: sets `settings.openfga_api_url` to the live server (monkeypatch or env), triggers bootstrap (store + model), and yields. Use `pytest.importorskip`/a reachability probe to skip gracefully in environments without OpenFGA so the default suite stays green; CI runs it where OpenFGA is up.

### Task 2: Prove the negative + positive relationship paths

- [ ] **Step 1 (failing test):** With FGA enabled, a member with **no** `can_edit` tuple on `project:X` calling `permission.check(member, "update_project", "X")` must raise `PermissionDenied`. Also: granting the tuple (`grant_project_access`) then checking the same action returns True. And: with FGA enabled but the server made unreachable, a mapped action **fails closed** (`PermissionDenied`, decision `denied_authz_unavailable`).
- [ ] **Step 2:** These should pass against the *existing* `check()` logic once FGA is enabled (no product code change expected — this is a proof task). If a test reveals a real enforcement bug, fix `permissions.py`/`authz.py` minimally and note it.

### Task 3: Coverage inventory

- [ ] **Step 1:** Add a test (or a small script `scripts/authz_coverage.py`) that introspects `permissions.py` and prints, for every known action, whether it is: role-gated (`_ADMIN_ACTIONS`/`_APPROVAL_DECISION_ACTIONS`), relationship-mapped (`_resource_for`), generic-allowed (`_GENERIC_ALLOWED_*`), or unmapped→denied. Output the mapped-vs-allowlisted-vs-denied breakdown. Commit the inventory as `docs/authz_coverage.md` — it drives W2.2's scope.

### Task 4: CI wiring + gate

- [ ] **Step 1:** Ensure `.github/workflows/ci.yml`'s backend job provisions OpenFGA (compose already has it) and that `test_authz_enforced.py` runs there (skipped locally when absent). Confirm the enabled-path tests pass in CI.

**Proof bar for W2.1:** CI runs the FGA-enabled suite; the unauthorized-relationship-action denial + fail-closed are green; `docs/authz_coverage.md` exists. The moat's relationship half is now proven, and the coverage gap is quantified for W2.2.

---

## Self-review notes

- W2.1 is deliberately a *prove-what-exists* phase, not new product code — the highest-value, lowest-risk first step (matches the advisor's read that the enabled path is the genuinely-unproven thing). Later phases add product code (coverage, groups, memory scopes) and each begins by confirming the gap against the live `permissions.py` rather than assuming it.
- Frozen seam respected: no change to `check()`'s signature; coverage expansion adds entries to the action→relation maps, not new call-site contracts.
