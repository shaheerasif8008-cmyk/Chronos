# W1 Phase 2B-2 — Self-Serve Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make self-serve orgs actually usable: a member of a self-serve org can log in (per-subdomain), repeat free-email signup logs in instead of duplicating, the unclaimed-domain signup race can't 500 or orphan an org, and production Cognito signup creates/joins the right org instead of dumping everyone into `default`.

**Architecture:** **Per-subdomain login** — login resolves the org from the request's tenant context (`request.state.resolved_org_id`, set by Phase-1 middleware; `X-Chronos-Org` in dev/test) and looks the member up *in that org*. Apex/no-tenant requests fall back to the `default` org (dev convenience). `signup_or_join` gains a global existing-member check for free-email idempotency and an `IntegrityError`-guarded unclaimed-domain branch. Cognito callback routes through `signup_or_join`.

**Tech Stack:** FastAPI, SQLAlchemy Core, pytest + httpx ASGITransport, Postgres.

**Spec/context:** Phase-2A plan's "Subsequent Phase 2 sub-plans" (the pinned 2B-2 items) + `docs/superpowers/specs/2026-06-20-w1-tenant-onboarding-identity-design.md`.

**Login-locus decision (settled):** per-subdomain login. You authenticate at your org's subdomain; the host resolves the org; the member is looked up in that org. This is unambiguous for an email that belongs to multiple orgs and matches the subdomain-per-tenant architecture. Apex login (no org context) falls back to `default` for dev only.

**Out of scope:** the cross-subdomain handoff token/endpoint and the signup UI → **Phase 2C**.

**Environment** (export before any python/pytest; `python3.11`):
```bash
export DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:5432/chronos" \
  REDIS_URL="redis://localhost:6379/0" \
  VAULT_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" \
  OBJECT_STORAGE_BACKEND="s3" AWS_S3_BUCKET="chronos-ci-local-fallback" AWS_S3_REGION="us-east-1"
```
Authoritative runs use a FRESH DB (`chronos_2b2`), `alembic upgrade head`, then drop.

---

## File Structure
- `apps/api/core/members.py` — **Modify.** Add `get_member_by_email_global`.
- `apps/api/core/signup.py` — **Modify.** Free-email idempotency + `IntegrityError`-guarded unclaimed-domain branch.
- `apps/api/routers/auth.py` — **Modify.** `verify_otp` per-subdomain resolution; `cognito_callback` routes through `signup_or_join`.
- `apps/api/tests/test_signup.py`, `apps/api/tests/test_auth_login.py` (new) — tests.

---

## Task 1: Per-subdomain login in `verify_otp`

**Files:** Modify `apps/api/routers/auth.py`; Test `apps/api/tests/test_auth_login.py` (new)

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_auth_login.py`:
```python
"""W1 Phase 2B-2 — per-subdomain login resolves the member's own org."""
from __future__ import annotations

import time
import uuid
import httpx
import pytest

import main
from core.db import engine, reflect_table
from routers.auth import _otp_store


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _make_org_member(subdomain: str, email: str):
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=subdomain, subdomain=subdomain, name="T"))
        await conn.execute(members.insert().values(
            id=member_id, organization_id=org_id, email=email.lower(), role="owner",
        ))
    return org_id, member_id


@pytest.mark.asyncio
async def test_login_resolves_member_in_subdomain_org():
    sub = f"acme{uuid.uuid4().hex[:8]}"
    email = f"founder@{sub}.io"
    org_id, member_id = await _make_org_member(sub, email)
    _otp_store[email.lower()] = {"code": "123456", "expires_at": time.time() + 300, "attempts": 0}
    async with _client() as client:
        resp = await client.post("/auth/verify-otp", json={"email": email, "code": "123456"},
                                 headers={"X-Chronos-Org": sub})
    assert resp.status_code == 200
    assert resp.json()["member_id"] == member_id


@pytest.mark.asyncio
async def test_login_rejects_member_not_in_resolved_org():
    sub = f"globex{uuid.uuid4().hex[:8]}"
    await _make_org_member(sub, f"someone@{sub}.io")  # org exists, but a different email logs in
    stranger = f"stranger{uuid.uuid4().hex[:6]}@nope.io"
    _otp_store[stranger.lower()] = {"code": "123456", "expires_at": time.time() + 300, "attempts": 0}
    async with _client() as client:
        resp = await client.post("/auth/verify-otp", json={"email": stranger, "code": "123456"},
                                 headers={"X-Chronos-Org": sub})
    assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_auth_login.py -v`
Expected: `test_login_resolves_member_in_subdomain_org` FAILS (current `verify_otp` ignores the host and looks up only the `default` org → 403).

- [ ] **Step 3: Implement per-subdomain resolution**

In `apps/api/routers/auth.py`, add `Request` to the fastapi import if not present, add `from core.members import get_member_in_org` to the members import, and change `verify_otp`'s signature + member-resolution block:
```python
async def verify_otp(req: OtpVerify, request: Request, response: Response) -> dict[str, str]:
    if not _dev_otp_enabled():
        raise HTTPException(status_code=404, detail="Dev OTP auth is disabled")
    email = req.email.lower()
    _consume_otp(email, req.code)

    # Per-subdomain login: resolve the member in the request's tenant. Apex / no
    # tenant context falls back to the default org (dev convenience).
    resolved = getattr(request.state, "resolved_org_id", None)
    if resolved is not None:
        member = await get_member_in_org(resolved, email=email)
        if member is None:
            member = await accept_pending_invitation(email, org_id=resolved)
    else:
        member = await get_member_by_email(email)
        if member is None:
            member = await accept_pending_invitation(email, org_id=settings.org_id)
    if member is None:
        raise HTTPException(status_code=403, detail="Email is not a member of this organization")

    token = create_access_token(member.id, org_id=member.organization_id)
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(update(members).where(members.c.id == member.id).values(region=settings.region))
    await audit.log("otp_verified", member.id, "auth.verify_otp", organization_id=member.organization_id)
    set_session_cookie(response, token)
    return {"access_token": token, "token_type": "bearer", "member_id": member.id}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_auth_login.py tests/test_signup.py -v`
Expected: PASS (new login tests + signup endpoint tests, which post without `X-Chronos-Org` and still resolve via the default fallback or their just-created member).
NOTE: signup tests log in via `/auth/signup`, not `/auth/verify-otp`, so they're unaffected. But existing OTP tests that rely on the default-org path must still pass — run them too: `python3.11 -m pytest -q -k "otp or verify" 2>&1 | tail -3`.

- [ ] **Step 5: Commit**
```bash
git add apps/api/routers/auth.py apps/api/tests/test_auth_login.py
git commit -m "feat(w1-2b2): per-subdomain login resolves member in the request's org"
```

---

## Task 2: Free-email signup idempotency

**Files:** Modify `apps/api/core/members.py`, `apps/api/core/signup.py`; Test `apps/api/tests/test_signup.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_signup.py`:
```python
@pytest.mark.asyncio
async def test_free_email_repeat_signup_is_idempotent():
    email = f"person{uuid.uuid4().hex[:8]}@gmail.com"
    first = await signup_or_join(email)
    again = await signup_or_join(email)
    assert again["org_id"] == first["org_id"]
    assert again["member_id"] == first["member_id"]
    assert again["created"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py::test_free_email_repeat_signup_is_idempotent -v`
Expected: FAIL — the second signup creates a new personal org (`created is True`, different org_id).

- [ ] **Step 3: Add a global member lookup + use it**

In `apps/api/core/members.py`, add:
```python
async def get_member_by_email_global(email: str) -> Member | None:
    """Find a member by email across all orgs (first match). Used by free-email
    signup to detect a returning user's existing personal org."""
    members = await reflect_table("members")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(members).where(members.c.email == email.lower()).order_by(members.c.created_at.asc())
        )).mappings().first()
    return Member(**dict(row)) if row else None
```
In `apps/api/core/signup.py`, import it (`from core.members import get_member_by_email_global, get_member_in_org, provision_member`) and guard the free-email branch:
```python
    if is_free_email_domain(domain):
        existing = await get_member_by_email_global(email)
        if existing is not None:
            return {"org_id": existing.organization_id, "member_id": existing.id,
                    "role": existing.role, "created": False, "joined": False}
        local = email.split("@", 1)[0]
        sub = await unique_subdomain(local)
        prov = await provision_org(slug=sub, name=org_name or f"{local}'s workspace", owner_email=email)
        return {"org_id": prov["org_id"], "member_id": prov["owner_member_id"],
                "role": "owner", "created": True, "joined": False}
```
NOTE: this means a free-email address already a member of any org logs into that org rather than getting a second personal org — the intended idempotent behavior (documented edge case: a free-email user previously invited to a work org resolves to that membership).

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v`
Expected: PASS (the new idempotency test + all existing signup tests, incl. the original free-email personal-org test which uses a fresh unique email each run).

- [ ] **Step 5: Commit**
```bash
git add apps/api/core/members.py apps/api/core/signup.py apps/api/tests/test_signup.py
git commit -m "feat(w1-2b2): free-email signup is idempotent (logs into existing org)"
```

---

## Task 3: TOCTOU-safe unclaimed-domain signup

**Files:** Modify `apps/api/core/signup.py`; Test `apps/api/tests/test_signup.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_signup.py` (forces the race by making the claim appear between the check and the insert via monkeypatch):
```python
@pytest.mark.asyncio
async def test_unclaimed_domain_race_resolves_to_join(monkeypatch):
    """If the domain gets claimed between our check and our insert (concurrent
    signup), the loser re-resolves to an auto-join instead of 500ing/orphaning."""
    import core.signup as signup_mod
    domain = f"race{uuid.uuid4().hex[:8]}.com"
    # First signup creates the org + claim normally.
    winner = await signup_or_join(f"founder@{domain}")
    # Force _claim_for_domain to return None on the next call so signup takes the
    # unclaimed branch, then collides on the real unique claim index.
    real = signup_mod._claim_for_domain
    calls = {"n": 0}
    async def fake_claim(d):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # pretend unclaimed → take the create-org branch
        return await real(d)
    monkeypatch.setattr(signup_mod, "_claim_for_domain", fake_claim)
    res = await signup_or_join(f"loser@{domain}")
    assert res["org_id"] == winner["org_id"]      # joined the winner's org
    assert res["created"] is False and res["joined"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py::test_unclaimed_domain_race_resolves_to_join -v`
Expected: FAIL — currently the duplicate claim insert raises `IntegrityError` (uncaught → error), not a clean join.

- [ ] **Step 3: Guard the unclaimed-domain branch**

In `apps/api/core/signup.py`, add `from sqlalchemy import insert, select` (select already imported) and `from sqlalchemy.exc import IntegrityError`. Replace the unclaimed-domain branch (the `sub = await unique_subdomain(...)` → claim insert → return block) with:
```python
    # Unclaimed work domain: create the org, make the signer owner, soft-claim.
    sub = await unique_subdomain(domain.split(".", 1)[0])
    prov = await provision_org(slug=sub, name=org_name or domain.split(".", 1)[0].title(), owner_email=email)
    claims = await reflect_table("email_domain_claims")
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(claims).values(
                organization_id=prov["org_id"], region=settings.region, domain=domain,
                claim_type="soft_email", join_policy="auto",
            ))
    except IntegrityError:
        # Lost the race: a concurrent signup claimed this domain first. Roll back
        # the org we just created and join the winner's org instead.
        orgs = await reflect_table("organizations")
        members = await reflect_table("members")
        async with engine.begin() as conn:
            await conn.execute(members.delete().where(members.c.id == prov["owner_member_id"]))
            await conn.execute(orgs.delete().where(orgs.c.id == prov["org_id"]))
        winner = await _claim_for_domain(domain)
        member = await provision_member(winner["organization_id"], email, role="user")
        return {"org_id": winner["organization_id"], "member_id": member.id,
                "role": "user", "created": False, "joined": True}
    await audit.log("domain_soft_claimed", prov["owner_member_id"], "signup.claim_domain",
                    organization_id=prov["org_id"], payload={"domain": domain})
    return {"org_id": prov["org_id"], "member_id": prov["owner_member_id"],
            "role": "owner", "created": True, "joined": False}
```
(The leftover context dir + OpenFGA tuple for the rolled-back org are harmless — an empty dir and a no-op tuple; acceptable for this rare race.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v`
Expected: PASS (race test + all existing).

- [ ] **Step 5: Commit**
```bash
git add apps/api/core/signup.py apps/api/tests/test_signup.py
git commit -m "fix(w1-2b2): TOCTOU-safe unclaimed-domain signup re-resolves to join"
```

---

## Task 4: Route Cognito callback through signup_or_join (prod signup)

**Files:** Modify `apps/api/routers/auth.py`; Test `apps/api/tests/test_signup.py` or `test_cognito_auth.py`

- [ ] **Step 1: Write the failing test**

Cognito callback is normally credential-gated; test the resolution logic by calling the helper path. Append to `apps/api/tests/test_signup.py` a test of a new `_resolve_cognito_member` helper (extracted in Step 3):
```python
@pytest.mark.asyncio
async def test_cognito_resolution_creates_org_for_unclaimed_domain():
    from routers.auth import _resolve_cognito_member
    domain = f"corp{uuid.uuid4().hex[:8]}.com"
    member = await _resolve_cognito_member(f"ceo@{domain}", name="CEO", resolved_org_id=None)
    assert member.organization_id and member.role == "owner"
    # second person same domain auto-joins
    member2 = await _resolve_cognito_member(f"eng@{domain}", name="Eng", resolved_org_id=None)
    assert member2.organization_id == member.organization_id and member2.role == "user"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py::test_cognito_resolution_creates_org_for_unclaimed_domain -v`
Expected: FAIL — `_resolve_cognito_member` doesn't exist.

- [ ] **Step 3: Implement**

In `apps/api/routers/auth.py`, add a helper and use it in `cognito_callback`/`cognito_verify` (replacing the `get_or_create_member_for_email(email, name=name)` calls):
```python
from core.members import get_member_in_org
from core.signup import signup_or_join


async def _resolve_cognito_member(email: str, *, name: str | None, resolved_org_id: str | None):
    """Resolve a Cognito-verified email to a member. Per-subdomain: log into the
    resolved org if a member there; otherwise self-serve create/join (signup_or_join).
    This makes production signup tenant-correct instead of dumping into `default`."""
    email = email.lower()
    if resolved_org_id is not None:
        member = await get_member_in_org(resolved_org_id, email=email)
        if member is not None:
            return member
    result = await signup_or_join(email, org_name=name)
    if result.get("member_id") is None:
        raise HTTPException(status_code=403, detail="Membership pending approval")
    return await get_member_in_org(result["org_id"], email=email)
```
Then in `cognito_callback` add `request: Request` to the signature and replace `member = await get_or_create_member_for_email(email, name=name)` with:
```python
        member = await _resolve_cognito_member(
            email, name=name, resolved_org_id=getattr(request.state, "resolved_org_id", None)
        )
```
Do the same in `cognito_verify` (add `request: Request`, route through `_resolve_cognito_member`). Keep the existing `CognitoAuthError`/`PermissionError` handling.

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py tests/test_cognito_auth.py -v`
Expected: PASS. (If `test_cognito_auth.py` asserts the old default-org behavior, update those assertions to the new tenant-correct behavior and note it — the old behavior was the bug.)

- [ ] **Step 5: Commit**
```bash
git add apps/api/routers/auth.py apps/api/tests/test_signup.py
git commit -m "feat(w1-2b2): route Cognito signup through signup_or_join (tenant-correct prod signup)"
```

---

## Task 5: Regression gate (fresh DB)

- [ ] **Step 1:** Run the full suite on a fresh `chronos_2b2` DB (create, `alembic upgrade head`, `pytest -q`, drop) per the environment block. Expected: only the 14 known `test_doc_authoring` optional-import failures; zero new. If a cognito or otp test newly fails, inspect — Task 1/4 changed those paths.

---

## Self-Review

**Spec coverage:** self-serve login (per-subdomain) → Task 1; free-email idempotency → Task 2; TOCTOU race → Task 3; prod/Cognito signup → Task 4. Handoff + UI explicitly deferred to 2C.

**Placeholder scan:** none — full code in every step.

**Type consistency:** `get_member_in_org(org_id, *, email=)` and `provision_member(org_id, email, role=)` are existing `core/members.py` signatures. `get_member_by_email_global(email)` defined in Task 2, used in Task 2. `_resolve_cognito_member(email, *, name, resolved_org_id)` defined + used in Task 4. `_claim_for_domain` is the existing `core/signup.py` helper (monkeypatched in Task 3's test). `signup_or_join` returns `{org_id, member_id, role, created, joined[, status]}` consumed consistently. `request.state.resolved_org_id` set by Phase-1 middleware, read in Tasks 1 & 4.
