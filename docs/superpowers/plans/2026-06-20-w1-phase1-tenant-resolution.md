# W1 Phase 1 — Tenant Resolution & Token Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every API request resolve to a tenant from its subdomain, and make org-bound session tokens fail closed when presented on the wrong tenant — without breaking the existing single-tenant test suite.

**Architecture:** A pure resolver (`core/tenancy.py`) maps a Host header (or a non-prod `X-Chronos-Org` override) to an `organization_id` via the new `organizations.subdomain` column. An ASGI middleware attaches the resolved org to `request.state`. `create_access_token` gains an optional `org` claim; `get_current_member` rejects a token whose `org` claim ≠ the resolved tenant. Legacy tokens (no `org` claim) are grandfathered to preserve the current suite — the login paths are flipped to mint org-bound tokens in Phase 2, alongside the subdomain-aware test harness.

**Tech Stack:** FastAPI (ASGI middleware), SQLAlchemy Core + `reflect_table`, Alembic, PyJWT (HS256), pytest + httpx ASGITransport, Postgres.

**Spec:** `docs/superpowers/specs/2026-06-20-w1-tenant-onboarding-identity-design.md` (§2a, §2e, §3 back-compat).

**Phase boundary (read first):** This phase ships the *mechanism* and proves it with dedicated tests. It does **not** flip the login/SSO/Cognito endpoints to mint org-bound tokens — that flip happens in Phase 2 where the test harness moves to subdomain base URLs. Keeping the flip out of Phase 1 is what keeps the existing ~621-test suite green. After this phase: requests resolve to tenants, org-bound tokens are enforced fail-closed, legacy tokens still work.

---

## File Structure

- `apps/api/migrations/versions/0039_org_subdomain.py` — **Create.** Adds `subdomain`, `onboarding_state`, `owner_member_id` to `organizations`; backfills `subdomain = slug`; unique index on `(subdomain)`; sets the seeded `default` org's subdomain to `default`.
- `apps/api/core/tenancy.py` — **Create.** Host/header → `organization_id` resolution. One responsibility: tenant resolution. Pure parsing + one DB lookup.
- `apps/api/core/config.py` — **Modify.** Add `base_domain` setting.
- `apps/api/main.py` — **Modify.** Register the tenant-resolution middleware.
- `apps/api/core/auth.py` — **Modify.** Optional `org` claim in `create_access_token`; fail-closed binding check in `get_current_member`.
- `apps/api/tests/test_tenancy.py` — **Create.** Unit tests for the resolver.
- `apps/api/tests/test_tenant_binding_http.py` — **Create.** HTTP integration: cross-tenant token rejected, same-tenant accepted, legacy token grandfathered.

---

## Task 1: Migration — `organizations.subdomain` + backfill

**Files:**
- Create: `apps/api/migrations/versions/0039_org_subdomain.py`
- Test: `apps/api/tests/test_tenant_binding_http.py` (added in Task 7; migration is verified by the chain guard + Task 2 lookups)

- [ ] **Step 1: Write the migration**

Look at `apps/api/migrations/versions/0038_sso_scim.py` for the exact `revision`/`down_revision` style and the `op`/`sa` imports. Use a short revision id (≤ 31 chars) — the audit's clean-deploy lesson.

Create `apps/api/migrations/versions/0039_org_subdomain.py`:

```python
"""Add subdomain + onboarding columns to organizations (W1 Phase 1).

Tenant resolution maps a request's subdomain label to an org via
``organizations.subdomain``. Backfills existing rows from ``slug`` so current
tenants (incl. the seeded ``default`` org) resolve immediately.
"""
from alembic import op
import sqlalchemy as sa

revision = "0039_org_subdomain"
down_revision = "0038_sso_scim"  # confirmed current head via `alembic heads`
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("subdomain", sa.Text(), nullable=True))
    op.add_column("organizations", sa.Column("onboarding_state", sa.Text(), nullable=False, server_default="new"))
    op.add_column("organizations", sa.Column("owner_member_id", sa.dialects.postgresql.UUID(as_uuid=False), nullable=True))
    # Backfill subdomain from slug for all existing tenants.
    op.execute("UPDATE organizations SET subdomain = slug WHERE subdomain IS NULL")
    op.create_index(
        "uq_organizations_subdomain", "organizations", ["subdomain"],
        unique=True, postgresql_where=sa.text("subdomain IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_organizations_subdomain", table_name="organizations")
    op.drop_column("organizations", "owner_member_id")
    op.drop_column("organizations", "onboarding_state")
    op.drop_column("organizations", "subdomain")
```

- [ ] **Step 2: Confirm the head revision id**

Run: `cd apps/api && python3.11 -m alembic heads`
Expected: prints `0038_sso_scim (head)` (a single head). If it differs, set `down_revision` to whatever it prints. (Multiple heads → run `alembic merge` first; out of scope if a single head.)

- [ ] **Step 3: Apply the migration on a fresh/dev DB**

Run: `cd apps/api && alembic upgrade head`
Expected: completes with no error; `0039_org_subdomain` is the new head.

- [ ] **Step 4: Verify the column + backfill**

Run: `cd apps/api && python -c "import asyncio; from core.db import engine; from sqlalchemy import text; asyncio.run(engine.dispose()) or None"`
Then run a quick check:
```bash
cd apps/api && python - <<'PY'
import asyncio
from sqlalchemy import text
from core.db import engine
async def main():
    async with engine.begin() as c:
        rows = (await c.execute(text("SELECT slug, subdomain FROM organizations"))).all()
        print(rows)
    await engine.dispose()
asyncio.run(main())
PY
```
Expected: every row has `subdomain == slug` (the `default` org shows `('default', 'default')`).

- [ ] **Step 5: Commit**

```bash
git add apps/api/migrations/versions/0039_org_subdomain.py
git commit -m "feat(w1): add organizations.subdomain + onboarding columns"
```

---

## Task 2: Config — `base_domain` setting

**Files:**
- Modify: `apps/api/core/config.py` (in the `Settings` class, near `org_id`/`region` ~line 16)

- [ ] **Step 1: Add the setting**

In `apps/api/core/config.py`, inside `class Settings(BaseSettings)`, add below `region`:

```python
    # Apex domain for per-tenant subdomains: novatech.<base_domain>.
    base_domain: str = "cognisiatech.com"
```

- [ ] **Step 2: Verify it loads**

Run: `cd apps/api && python -c "from core.config import settings; print(settings.base_domain)"`
Expected: prints `cognisiatech.com`.

- [ ] **Step 3: Commit**

```bash
git add apps/api/core/config.py
git commit -m "feat(w1): add base_domain setting for subdomain tenancy"
```

---

## Task 3: Resolver — `core/tenancy.py`

**Files:**
- Create: `apps/api/core/tenancy.py`
- Test: `apps/api/tests/test_tenancy.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/test_tenancy.py`:

```python
"""W1 Phase 1 — tenant label extraction from a Host header."""
from __future__ import annotations

import pytest

from core.tenancy import RESERVED_LABELS, extract_tenant_label


@pytest.mark.parametrize(
    "host,expected",
    [
        ("novatech.cognisiatech.com", "novatech"),
        ("novatech.cognisiatech.com:443", "novatech"),
        ("acme.localhost", "acme"),
        ("acme.localhost:8000", "acme"),
        ("acme.lvh.me", "acme"),
        ("cognisiatech.com", None),          # apex → no tenant
        ("www.cognisiatech.com", None),      # reserved label
        ("app.cognisiatech.com", None),      # reserved label
        ("api.cognisiatech.com", None),      # reserved label
        ("localhost", None),                 # bare dev host → no tenant
        ("test", None),                      # httpx ASGITransport default Host
        ("", None),
    ],
)
def test_extract_tenant_label(host, expected):
    assert extract_tenant_label(host, base_domain="cognisiatech.com") == expected


def test_reserved_labels_are_blocked():
    for label in ("app", "www", "api", "admin"):
        assert label in RESERVED_LABELS
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenancy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.tenancy'`.
(Note: use `python3.11` — system `python3` is 3.9 here and breaks imports.)

- [ ] **Step 3: Write the resolver**

Create `apps/api/core/tenancy.py`:

```python
"""Resolve an incoming request to its tenant (organization).

Resolution order: an explicit ``X-Chronos-Org`` header (honored only outside
production), otherwise the subdomain label of the Host header. The label is
looked up against ``organizations.subdomain``. Returns ``None`` for the apex
host, reserved labels, or an unknown subdomain (the "no-tenant" context that
serves only signup/login).
"""
from __future__ import annotations

from sqlalchemy import select

from core.config import settings
from core.db import engine, reflect_table

# Labels that must never be a tenant subdomain (they route the platform itself).
RESERVED_LABELS = frozenset({"app", "www", "api", "admin", "static", "assets"})

# Dev hosts that carry a tenant label as their first segment without DNS.
_DEV_SUFFIXES = (".localhost", ".lvh.me")


def extract_tenant_label(host: str, *, base_domain: str | None = None) -> str | None:
    """Return the tenant subdomain label from ``host``, or ``None``.

    Strips a port, handles the production ``<label>.<base_domain>`` shape and the
    dev ``<label>.localhost`` / ``<label>.lvh.me`` shapes, and rejects reserved
    labels and the bare apex.
    """
    if not host:
        return None
    host = host.split(":", 1)[0].strip().lower().rstrip(".")
    base = (base_domain or settings.base_domain).lower()

    label: str | None = None
    if host.endswith("." + base):
        label = host[: -(len(base) + 1)].split(".")[0]
    else:
        for suffix in _DEV_SUFFIXES:
            if host.endswith(suffix):
                label = host[: -len(suffix)].split(".")[0]
                break
    if not label or label in RESERVED_LABELS:
        return None
    return label


async def resolve_org_id(host: str, org_header: str | None) -> str | None:
    """Resolve a request to an ``organization_id`` (or ``None`` for no-tenant).

    The header override is honored only outside production so tests/dev can drive
    multiple tenants on ``localhost`` without wildcard DNS.
    """
    label: str | None = None
    if org_header and not settings.is_production:
        label = org_header.strip().lower()
        if label in RESERVED_LABELS:
            label = None
    if label is None:
        label = extract_tenant_label(host)
    if label is None:
        return None

    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        org_id = (
            await conn.execute(
                select(organizations.c.id).where(organizations.c.subdomain == label)
            )
        ).scalar_one_or_none()
    return str(org_id) if org_id is not None else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenancy.py -v`
Expected: PASS (all parametrized cases + reserved-labels test).

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/tenancy.py apps/api/tests/test_tenancy.py
git commit -m "feat(w1): tenant resolver from Host header + dev org override"
```

---

## Task 4: Middleware — attach resolved org to request state

**Files:**
- Modify: `apps/api/main.py` (after the `app.add_middleware(CORSMiddleware, ...)` block, ~line 46-53)

- [ ] **Step 1: Add the middleware**

In `apps/api/main.py`, add an import near the other `core` imports at the top:

```python
from core.tenancy import resolve_org_id
```

Then, immediately **after** the existing `app.add_middleware(CORSMiddleware, ...)` call, add:

```python
@app.middleware("http")
async def _resolve_tenant(request: Request, call_next):
    """Bind each request to its tenant. Stored on request.state for the auth
    dependency; ``None`` means the no-tenant (apex/signup) context."""
    host = request.headers.get("host", "")
    org_header = request.headers.get("x-chronos-org")
    request.state.resolved_org_id = await resolve_org_id(host, org_header)
    return await call_next(request)
```

- [ ] **Step 2: Verify the app still imports and boots**

Run: `cd apps/api && python3.11 -c "import main; print('ok', any(getattr(m, '__name__', '')=='_resolve_tenant' or True for m in [main.app]))"`
Expected: prints `ok True` with no import error.

- [ ] **Step 3: Run the full suite to confirm no regression**

Run: `cd apps/api && python3.11 -m pytest -q`
Expected: same pass count as before this task (middleware sets state but nothing reads it yet; existing tests use Host `test` → `resolved_org_id = None`, unused).

- [ ] **Step 4: Commit**

```bash
git add apps/api/main.py
git commit -m "feat(w1): tenant-resolution middleware sets request.state.resolved_org_id"
```

---

## Task 5: Org-bound tokens — optional `org` claim

**Files:**
- Modify: `apps/api/core/auth.py` (`create_access_token`, ~line 28)
- Test: `apps/api/tests/test_tenant_binding_http.py` (full coverage in Task 7; a focused unit assertion here)

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_tenant_binding_http.py` with just this test for now:

```python
"""W1 Phase 1 — org-bound session tokens carry and enforce an `org` claim."""
from __future__ import annotations

import jwt

from core.auth import create_access_token
from core.config import settings


def test_token_includes_org_claim_when_provided():
    token = create_access_token("member-123", org_id="org-abc")
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert payload["org"] == "org-abc"


def test_token_omits_org_claim_when_not_provided():
    token = create_access_token("member-123")
    payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert "org" not in payload  # legacy tokens stay org-less and grandfathered
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v`
Expected: FAIL — `create_access_token() got an unexpected keyword argument 'org_id'`.

- [ ] **Step 3: Add the optional claim**

In `apps/api/core/auth.py`, change `create_access_token`:

```python
def create_access_token(member_id: str, *, org_id: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": member_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_expire_minutes)).timestamp()),
    }
    if org_id is not None:
        payload["org"] = org_id
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/auth.py apps/api/tests/test_tenant_binding_http.py
git commit -m "feat(w1): optional org claim in session tokens (back-compat preserved)"
```

---

## Task 6: Enforcement — fail closed on the wrong tenant

**Files:**
- Modify: `apps/api/core/auth.py` (`get_current_member`, ~line 39)

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_tenant_binding_http.py`:

```python
import uuid
import pytest
import httpx
import main
from core.db import engine, reflect_table


async def _make_org_and_member(subdomain: str):
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=subdomain, subdomain=subdomain, name=subdomain.title(),
        ))
        await conn.execute(members.insert().values(
            id=member_id, organization_id=org_id, email=f"{member_id[:8]}@{subdomain}.io", role="owner",
        ))
    return org_id, member_id


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


@pytest.mark.asyncio
async def test_org_bound_token_rejected_on_wrong_tenant():
    org_a, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    org_b, _ = await _make_org_and_member(f"globex{uuid.uuid4().hex[:6]}")
    from core.auth import create_access_token
    token = create_access_token(member_a, org_id=org_a)

    # The org-B subdomain is resolved via the dev X-Chronos-Org override.
    b_subdomain = (await reflect_table("organizations"))
    async with _client() as client:
        orgs = await reflect_table("organizations")
        async with engine.begin() as conn:
            b_label = (await conn.execute(
                orgs.select().where(orgs.c.id == org_b)
            )).mappings().one()["subdomain"]
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": b_label},
        )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_org_bound_token_accepted_on_its_own_tenant():
    org_a, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    from core.auth import create_access_token
    token = create_access_token(member_a, org_id=org_a)
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        a_label = (await conn.execute(
            orgs.select().where(orgs.c.id == org_a)
        )).mappings().one()["subdomain"]
    async with _client() as client:
        resp = await client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": a_label},
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_legacy_token_without_org_claim_is_grandfathered():
    _, member_a = await _make_org_and_member(f"acme{uuid.uuid4().hex[:6]}")
    from core.auth import create_access_token
    token = create_access_token(member_a)  # no org claim
    async with _client() as client:
        resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
```

Note: this assumes a `GET /auth/me` returning the current member (Step 3 adds it if missing).

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v -k "wrong_tenant or own_tenant or grandfathered"`
Expected: FAIL — either `/auth/me` is 404, or the wrong-tenant case returns 200 (no enforcement yet).

- [ ] **Step 3: Add the binding check (and `/auth/me` if absent)**

In `apps/api/core/auth.py`, add `Request` to the imports and the dependency signature, and enforce the claim. Replace `get_current_member`'s signature + add the check right after the member is loaded:

```python
from fastapi import Cookie, Depends, HTTPException, Request
```

```python
async def get_current_member(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    chronos_session: str | None = Cookie(default=None),
) -> Member:
    # ... unchanged token decode + member load ...

    if getattr(member, "status", "active") != "active":
        raise HTTPException(status_code=403, detail="Member account is deactivated")

    # Tenant binding: an org-bound token is valid only on its own tenant. Legacy
    # tokens (no `org` claim) are grandfathered. Fail closed when the resolved
    # tenant is known and does not match.
    token_org = payload.get("org")
    if token_org is not None:
        resolved = getattr(request.state, "resolved_org_id", None)
        if resolved is not None and resolved != token_org:
            raise HTTPException(status_code=403, detail="Token not valid for this tenant")
    return member
```

If `GET /auth/me` does not already exist, check `apps/api/routers/auth.py` first:
Run: `grep -n "auth/me\|/me" apps/api/routers/auth.py`
If absent, add to `apps/api/routers/auth.py`:

```python
from fastapi import Depends
from core.auth import get_current_member
from core.models import Member


@router.get("/me")
async def me(member: Member = Depends(get_current_member)) -> dict:
    return {"id": member.id, "email": member.email, "role": member.role,
            "organization_id": member.organization_id}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_tenant_binding_http.py -v`
Expected: PASS — wrong-tenant → 403, own-tenant → 200, legacy → 200.

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/auth.py apps/api/routers/auth.py apps/api/tests/test_tenant_binding_http.py
git commit -m "feat(w1): fail-closed tenant binding for org-scoped tokens"
```

---

## Task 7: Full-suite regression gate

**Files:** none (verification only).

- [ ] **Step 1: Run the whole backend suite**

Run: `cd apps/api && python3.11 -m pytest -q`
Expected: all previously-passing tests still pass, plus the new `test_tenancy.py` and `test_tenant_binding_http.py`. No new failures. (If the environment is missing optional libs like `pypdf`/`docx`, those pre-existing env-only failures are unrelated — confirm the count matches the pre-change baseline plus the new tests.)

- [ ] **Step 2: Confirm migration chain is clean on a fresh DB**

Run: `cd apps/api && alembic downgrade base && alembic upgrade head`
Expected: both complete with no error; head is `0039_org_subdomain`.

- [ ] **Step 3: Commit (if any test-only fixups were needed)**

```bash
git add -A && git commit -m "test(w1): green suite with tenant resolution + binding" || echo "nothing to commit"
```

---

## Subsequent W1 phases (roadmap — each gets its own plan)

These are **not** detailed here; they become their own plans once Phase 1 lands.

- **Phase 2 — Provisioning + self-serve signup + domain claiming.** `core/provisioning.py` (parameterized `seed.py` shape), `email_domain_claims`/`domain_verifications` migration, `POST /auth/signup`, reserved-slug enforcement, free-email blocklist, the cross-subdomain handoff token, and the **flip of login/SSO/Cognito to mint org-bound tokens** with the test harness moved to subdomain base URLs.
  - **Carried from Phase 1 review (must resolve before flipping minting):**
    - **C1 — no-tenant-host policy.** Today an org-bound token is *accepted* when `request.state.resolved_org_id` is `None` (apex / unknown subdomain / resolver failure). Harmless in Phase 1 (nothing mints org-bound tokens). Before the flip, decide explicitly: reject org-bound tokens on no-tenant hosts *except* a designated handoff endpoint, and pin it with a test. (Pinned in a code comment on the binding check in `core/auth.py`.) This interacts with the `.cognisiatech.com` parent-domain cookie — a token auto-sent to apex/sibling subdomains must not pass binding.
    - **C2 — close grandfathering.** Org-less (legacy) tokens currently bypass the binding check. Once minting flips, add `settings.enforce_org_bound_tokens` (or equivalent) that makes `get_current_member` reject tokens with no `org` claim (401), so grandfathering can't silently persist. All four mint sites are enumerated in a comment above `create_access_token`: `routers/auth.py` (OTP verify, Cognito callback, Cognito verify) and `routers/sso.py`.
    - **Case-sensitivity (from final review).** The resolver lowercases the incoming label but `organizations.subdomain` is plain case-sensitive `TEXT` backfilled verbatim from `slug` (which has no lowercase constraint). An org with an uppercase slug becomes silently *unresolvable* (a correctness/availability bug, not an isolation breach — a mismatch resolves to one row or None, never the wrong tenant). Inert today (only the lowercase `default` org exists). Phase 2 provisioning must lowercase the derived subdomain AND the lookup must be case-insensitive (`citext` or a `lower(subdomain)` expression index).
    - **Test debt to add alongside:** cookie-path coverage exists; extend with the handoff-endpoint accept case once C1's policy lands.
- **Phase 3 — DNS-TXT domain verification + SSO registry reconciliation.** `domain_verifications` flow; make `email_domain_claims` canonical over `sso_connections.email_domain`; validate SSO connection creation against a hard-claimed domain.
- **Phase 4 — Frontend onboarding wizard + subdomain-aware web base URL.** Replace env-only base logic in `apps/web/lib/api.ts` (+ duplicated copies) with `window.location.hostname` derivation; owner first-run wizard (name/branding, invite teammates, optional SSO/SCIM, optional domain verify).
- **Infra (ops, not a code plan).** Wildcard custom domain `*.cognisiatech.com` + wildcard TLS on Render; wildcard CNAME; cookie scope `.cognisiatech.com`; per-tenant CORS.

---

## Self-Review

**Spec coverage (Phase 1 scope only):** §2a tenant resolution → Tasks 3–4; §2a fail-closed token binding → Tasks 5–6; §2e dev/CI `X-Chronos-Org` override (non-prod gated) → Task 3 `resolve_org_id`; §3 `organizations.subdomain` + `onboarding_state` + `owner_member_id` and `default` back-compat → Task 1; §3 legacy-token grandfathering → Task 6. Signup, domain claims, DNS verify, handoff token, web base URL, and the login-token flip are explicitly deferred to Phases 2–4 (documented in the phase boundary + roadmap).

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to" — every code step has full code and every run step has an exact command + expected result. The roadmap section is forward-looking context, not steps in this plan.

**Type consistency:** `extract_tenant_label(host, *, base_domain)` and `resolve_org_id(host, org_header)` are used identically in `core/tenancy.py`, the middleware (Task 4), and the tests (Tasks 3, 6). `create_access_token(member_id, *, org_id=None)` is called with `org_id=` in Tasks 5–6 and remains positional-compatible for all existing callers. `request.state.resolved_org_id` is written in Task 4 and read in Task 6 under the same name. Token claim key `"org"` is written in Task 5 and read in Task 6.
