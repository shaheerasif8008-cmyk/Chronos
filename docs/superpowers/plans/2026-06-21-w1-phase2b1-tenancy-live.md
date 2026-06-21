# W1 Phase 2B-1 — Make Tenancy Live (token-binding flip + C1/C2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Phase 1 tenant-binding mechanism *live* — every login mints an org-bound session token, an org-bound token is rejected on a foreign/no-tenant host in production (closing the C1 hole), and an opt-in flag can require org-bound tokens (closing C2's grandfathering) — without breaking the existing suite.

**Architecture:** Flip the 5 `create_access_token` call sites to pass `org_id`. Make `get_current_member`'s binding check environment-aware: in production a no-tenant host (`resolved_org_id is None`) rejects an org-bound token; in non-production it falls back to trusting the token's own org (no wildcard DNS in tests/dev). Add `settings.enforce_org_bound_tokens` (default off) that, when on, rejects org-less (legacy) tokens. **Data isolation does not depend on this check** — it is enforced downstream by `member.organization_id` scoping; token-binding is a secondary defense, which is why relaxing it in non-prod is safe.

**Tech Stack:** FastAPI, PyJWT (HS256), SQLAlchemy Core, pytest + httpx ASGITransport, Postgres.

**Spec:** `docs/superpowers/specs/2026-06-20-w1-tenant-onboarding-identity-design.md` (§2a, §2e) + the Phase-1 carried items C1/C2 and the Phase-2A plan's "Subsequent Phase 2 sub-plans".

**Out of scope (later phases):** cross-subdomain handoff endpoint/token → **2C** (it serves the apex→subdomain redirect, which doesn't exist until the UI). Self-serve login path, free-email idempotency, TOCTOU race, prod/Cognito signup → **2B-2**.

**Environment** (export before any python/pytest; use `python3.11`):
```bash
export DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:5432/chronos" \
  REDIS_URL="redis://localhost:6379/0" \
  VAULT_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" \
  OBJECT_STORAGE_BACKEND="s3" AWS_S3_BUCKET="chronos-ci-local-fallback" AWS_S3_REGION="us-east-1"
```
Authoritative test runs use a FRESH DB (the shared dev `chronos` DB has known state pollution): create `chronos_2b1`, `alembic upgrade head`, run, drop.

---

## File Structure

- `apps/api/routers/auth.py` — **Modify.** Flip 3 mint sites (`verify_otp`, `signup`, `cognito/callback`, `cognito/verify`) to org-bound.
- `apps/api/routers/sso.py` — **Modify.** Flip the SSO mint site.
- `apps/api/core/auth.py` — **Modify.** Env-aware C1 + C2 enforcement in `get_current_member`.
- `apps/api/core/config.py` — **Modify.** Add `enforce_org_bound_tokens`.
- `apps/api/tests/test_tenant_binding_http.py` — **Modify.** New tests: mint-sites carry org claim; prod no-tenant-host rejection (C1); C2 flag behavior.

---

## Task 1: Flip the 5 mint sites to org-bound tokens

**Files:** Modify `apps/api/routers/auth.py`, `apps/api/routers/sso.py`; Test `apps/api/tests/test_tenant_binding_http.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_tenant_binding_http.py`:

```python
@pytest.mark.asyncio
async def test_signup_mints_org_bound_token():
    """A signup token carries the new org's id as its `org` claim."""
    import jwt as _jwt
    from core.config import settings as _settings
    from routers.auth import _otp_store
    import time
    domain = f"flip{uuid.uuid4().hex[:8]}.com"
    email = f"founder@{domain}"
    _otp_store[email] = {"code": "123456", "expires_at": time.time() + 300, "attempts": 0}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        resp = await client.post("/auth/signup", json={"email": email, "code": "123456"})
    assert resp.status_code == 200
    body = resp.json()
    payload = _jwt.decode(body["access_token"], _settings.jwt_secret, algorithms=["HS256"])
    assert payload["org"] == body["org_id"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py::test_signup_mints_org_bound_token -v`
Expected: FAIL — `KeyError: 'org'` (signup currently mints an org-less token).

- [ ] **Step 3: Flip the mint sites**

In `apps/api/routers/auth.py`, change each `create_access_token(...)` call to pass the org:
- `verify_otp` (currently `token = create_access_token(member.id)`) → `token = create_access_token(member.id, org_id=member.organization_id)`
- `signup` (currently `token = create_access_token(result["member_id"])`, with the "Phase 2B flip" comment) → `token = create_access_token(result["member_id"], org_id=result["org_id"])` and update/remove the now-done comment.
- `cognito/callback` (`token = create_access_token(member.id)`) → `token = create_access_token(member.id, org_id=member.organization_id)`
- `cognito/verify` (`token = create_access_token(member.id)`) → `token = create_access_token(member.id, org_id=member.organization_id)`

In `apps/api/routers/sso.py`, the mint site (`token = create_access_token(member.id)`) → `token = create_access_token(member.id, org_id=member.organization_id)`.

(`member.organization_id` exists on the `Member` model; `result["org_id"]` is returned by `signup_or_join`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v`
Expected: PASS (new test + the existing binding tests still pass — legacy grandfather test still uses an explicitly org-less `create_access_token(member_a)`).

- [ ] **Step 5: Measure regression on a FRESH DB (the load-bearing check)**

The mint flip means every login now mints an org-bound token. Confirm this does NOT break the suite (current binding logic accepts org-bound tokens when `resolved_org_id is None`, i.e. on `Host: test`):
```bash
cd apps/api && export PGPASSWORD=chronos
psql -h localhost -U chronos -d chronos -c "DROP DATABASE IF EXISTS chronos_2b1;" -c "CREATE DATABASE chronos_2b1;"
DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:5432/chronos_2b1" REDIS_URL="redis://localhost:6379/0" VAULT_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" OBJECT_STORAGE_BACKEND="s3" AWS_S3_BUCKET="chronos-ci-local-fallback" AWS_S3_REGION="us-east-1" bash -c 'python3.11 -m alembic upgrade head >/dev/null && python3.11 -m pytest -q 2>&1 | tail -3'
psql -h localhost -U chronos -d chronos -c "DROP DATABASE chronos_2b1;"
```
Expected: only the 14 known `test_doc_authoring` optional-import failures; ZERO new failures. If any auth/login test newly fails, STOP and report — the flip assumption is wrong and must be understood before proceeding.

- [ ] **Step 6: Commit**

```bash
git add apps/api/routers/auth.py apps/api/routers/sso.py apps/api/tests/test_tenant_binding_http.py
git commit -m "feat(w1-2b1): mint org-bound session tokens at all login sites"
```

---

## Task 2: Environment-aware C1 — reject org-bound tokens on a no-tenant host in production

**Files:** Modify `apps/api/core/auth.py`; Test `apps/api/tests/test_tenant_binding_http.py`

- [ ] **Step 1: Write the failing test**

The prod-reject branch is the one security path the (non-prod) suite never exercises, so it needs a dedicated test that forces production. Append to `apps/api/tests/test_tenant_binding_http.py`:

```python
@pytest.mark.asyncio
async def test_org_bound_token_rejected_on_no_tenant_host_in_production(monkeypatch):
    """C1: in production, an org-bound token on a host that resolves to no tenant
    (apex / unknown subdomain) is rejected — closing the no-tenant-host hole."""
    _, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    org_a = (await _subdomain_of  # noqa: F821  (helper below already in file)
             ) if False else None
    # Build an org-bound token for member_a's org.
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        org_id = (await conn.execute(
            members.select().where(members.c.id == member_a)
        )).mappings().one()["organization_id"]
    token = create_access_token(member_a, org_id=org_id)
    # Force production so the no-tenant-host branch rejects. No X-Chronos-Org → resolved None.
    monkeypatch.setattr("core.auth.settings.is_production", True, raising=False)
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token not valid for this tenant"
```
NOTE: `settings.is_production` is a property; if `monkeypatch.setattr("core.auth.settings.is_production", True)` raises (property has no setter), instead patch the whole settings object the module sees: `monkeypatch.setattr("core.auth.settings", types.SimpleNamespace(is_production=True, jwt_secret=settings.jwt_secret, access_token_expire_minutes=settings.access_token_expire_minutes, enforce_org_bound_tokens=False))` — but that breaks other attribute reads. PREFERRED robust approach: add a tiny indirection — have `get_current_member` read `settings.is_production` via a module-level helper `_is_production()` that the test can monkeypatch: define `def _is_production() -> bool: return settings.is_production` in `core/auth.py` and call it; then the test does `monkeypatch.setattr("core.auth._is_production", lambda: True)`. Implement that indirection in Step 3.

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py::test_org_bound_token_rejected_on_no_tenant_host_in_production -v`
Expected: FAIL — currently `resolved is None` → token accepted → 200 (not 403).

- [ ] **Step 3: Implement env-aware C1**

In `apps/api/core/auth.py`, add the production indirection near the top (after imports):
```python
def _is_production() -> bool:
    return settings.is_production
```
Replace the binding block in `get_current_member` (the `token_org = payload.get("org")` block and its NOTE comment) with:
```python
    # Tenant binding (secondary defense — data isolation is enforced downstream by
    # member.organization_id scoping, not by this check). An org-bound token is
    # valid only on its own tenant.
    token_org = payload.get("org")
    if token_org is not None:
        resolved = getattr(request.state, "resolved_org_id", None)
        if resolved is None:
            # No tenant resolved from the host. In production this is the apex /
            # an unknown subdomain — reject (C1): the app only serves authed
            # traffic on a tenant subdomain. In non-production there is no
            # wildcard DNS (Host is "test"/localhost), so trust the token's org.
            if _is_production():
                raise HTTPException(status_code=403, detail="Token not valid for this tenant")
        elif resolved != token_org:
            raise HTTPException(status_code=403, detail="Token not valid for this tenant")
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v`
Expected: PASS — the new prod-reject test passes, and all existing binding tests (wrong-tenant 403 via X-Chronos-Org, own-tenant 200, legacy grandfather 200, cookie path) still pass.

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/auth.py apps/api/tests/test_tenant_binding_http.py
git commit -m "feat(w1-2b1): env-aware C1 — reject org-bound tokens on no-tenant host in prod"
```

---

## Task 3: C2 — `enforce_org_bound_tokens` flag closes grandfathering

**Files:** Modify `apps/api/core/config.py`, `apps/api/core/auth.py`; Test `apps/api/tests/test_tenant_binding_http.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_tenant_binding_http.py`:

```python
@pytest.mark.asyncio
async def test_enforce_flag_rejects_org_less_tokens(monkeypatch):
    """C2: with enforcement on, a legacy org-less token is rejected (401)."""
    _, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    legacy = create_access_token(member_a)  # no org claim
    monkeypatch.setattr("core.auth.settings.enforce_org_bound_tokens", True, raising=False)
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_enforce_flag_off_grandfathers_org_less_tokens():
    """C2 default: enforcement off → legacy org-less token still works."""
    _, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    legacy = create_access_token(member_a)
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {legacy}"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v -k "enforce_flag"`
Expected: the `rejects_org_less` test FAILS (legacy token returns 200, not 401); the `off_grandfathers` test passes.

- [ ] **Step 3: Add the flag + enforcement**

In `apps/api/core/config.py`, add to `Settings` (near the other auth settings):
```python
    # When true, reject legacy org-less session tokens (post-flip enforcement, C2).
    enforce_org_bound_tokens: bool = False
```
In `apps/api/core/auth.py` `get_current_member`, immediately BEFORE the `token_org = payload.get("org")` line, add:
```python
    # C2: once minting is flipped, optionally refuse legacy org-less tokens.
    if settings.enforce_org_bound_tokens and payload.get("org") is None:
        raise HTTPException(status_code=401, detail="Session token missing tenant binding")
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v`
Expected: PASS (both enforce-flag tests + all prior).

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/config.py apps/api/core/auth.py apps/api/tests/test_tenant_binding_http.py
git commit -m "feat(w1-2b1): enforce_org_bound_tokens flag rejects legacy tokens (C2)"
```

---

## Task 4: Regression gate (fresh DB)

**Files:** none.

- [ ] **Step 1: Full suite on a fresh DB**

```bash
cd apps/api && export PGPASSWORD=chronos
psql -h localhost -U chronos -d chronos -c "DROP DATABASE IF EXISTS chronos_2b1g;" -c "CREATE DATABASE chronos_2b1g;"
DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:5432/chronos_2b1g" REDIS_URL="redis://localhost:6379/0" VAULT_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" OBJECT_STORAGE_BACKEND="s3" AWS_S3_BUCKET="chronos-ci-local-fallback" AWS_S3_REGION="us-east-1" bash -c 'python3.11 -m alembic upgrade head >/dev/null && python3.11 -m pytest -q 2>&1 | tail -3'
psql -h localhost -U chronos -d chronos -c "DROP DATABASE chronos_2b1g;"
```
Expected: only the 14 known `test_doc_authoring` optional-import failures; ZERO new failures.

---

## Subsequent sub-plans (roadmap)

- **Phase 2B-2 — Self-serve robustness.** Self-serve login path (decide login-locus: **per-subdomain login** — resolve org from host/`X-Chronos-Org` → `get_member_in_org(resolved_org, email)` — is the clean, unambiguous model and removes multi-org ambiguity; pin this in the 2B-2 plan); free-email idempotency (existing-personal-org lookup before creating a new one); the TOCTOU race fix in `signup_or_join`'s unclaimed-domain branch; route prod/Cognito callback through `signup_or_join`.
- **Phase 2C — Signup/onboarding UI + handoff.** Next.js signup page + onboarding landing; the cross-subdomain handoff token/endpoint that the apex→subdomain redirect actually drives; `window.location.hostname` API-base derivation.

---

## Self-Review

**Spec coverage:** C1 (no-tenant-host policy) → Task 2; C2 (close grandfathering) → Task 3; org-bound-token flip across all mint sites → Task 1. Handoff explicitly deferred to 2C (it has no driver in 2B). Login path / free-email / TOCTOU / prod-signup explicitly deferred to 2B-2.

**Placeholder scan:** none — every code step is concrete. Task 2 Step 1 documents the `_is_production()` indirection so the prod-reject test is robust against the `is_production` property having no setter.

**Type consistency:** `create_access_token(member_id, *, org_id=None)` is the existing signature; all five flipped call sites pass `org_id=` with a real value. `_is_production()` defined in Task 2 and used in the same `get_current_member`. `settings.enforce_org_bound_tokens` defined in Task 3 config and read in `get_current_member`. `_make_org_and_member`, `_client`, `_subdomain_of`, `create_access_token`, `reflect_table`, `engine` are already imported/defined in `test_tenant_binding_http.py` (Phase 1).
