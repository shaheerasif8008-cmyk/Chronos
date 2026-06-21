# W1 Phase 2A — Provisioning + Self-Serve Signup + Domain Claiming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new company can self-serve create an isolated, provisioned org by verifying a work email — first signup from a domain claims it and becomes owner; same-domain signups auto-join — all backend + API + tests, reusing the Phase 1 tenant columns and the existing OTP/member machinery.

**Architecture:** A `core/provisioning.py` parameterizes seed.py's org-genesis shape (org row + owner member + `context/{org}/org.md` + OpenFGA owner grant). A `core/signup.py` decides, from the email domain, whether to create a new org (+ soft domain claim), auto-join a claimed org, or create a personal org for a free-email address. A `POST /auth/signup` endpoint wraps that core logic with OTP email-verification (reusing the dev OTP store). Subdomains are stored lowercase (closes the Phase 1 case-sensitivity carry-item for newly-created orgs).

**Tech Stack:** FastAPI, SQLAlchemy Core + `reflect_table`, Alembic, pytest + httpx ASGITransport, Postgres.

**Spec:** `docs/superpowers/specs/2026-06-20-w1-tenant-onboarding-identity-design.md` (§2b signup, §2c provisioning, §3 `email_domain_claims`, §4 free-email/soft-claim).

**Phase boundary (read first):** 2A issues a **legacy org-less session token** on signup success (grandfathered, same as Phase 1) — tenancy is created but not yet *bound*. The org-bound-token flip across all mint sites, the cross-subdomain handoff endpoint, and the C1/C2 enforcement land in **Phase 2B**. DNS-TXT hard domain verification is **Phase 3**. The signup/onboarding UI is **Phase 2C**. This keeps 2A backend-only and the suite green.

**Environment for all tasks** (export before any python/pytest; use `python3.11` — system python3 is 3.9 and breaks):
```bash
export DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:5432/chronos" \
  REDIS_URL="redis://localhost:6379/0" \
  VAULT_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" \
  OBJECT_STORAGE_BACKEND="s3" AWS_S3_BUCKET="chronos-ci-local-fallback" AWS_S3_REGION="us-east-1"
```
Current alembic head: `0039_org_subdomain`. Postgres+Redis are running.

---

## File Structure

- `apps/api/migrations/versions/0040_email_domain_claims.py` — **Create.** `email_domain_claims` table (domain → org, claim_type, join_policy), unique on `domain`.
- `apps/api/core/provisioning.py` — **Create.** `provision_org(...)`: parameterized org genesis. One responsibility: create + provision an org.
- `apps/api/core/signup.py` — **Create.** Domain blocklist, slug derivation, and `signup_or_join(email, org_name)` decision logic. One responsibility: turn a verified email into an org membership.
- `apps/api/routers/auth.py` — **Modify.** Refactor OTP code-check into `_consume_otp`; add `POST /auth/signup`.
- `apps/api/tests/test_provisioning.py` — **Create.**
- `apps/api/tests/test_signup.py` — **Create.** (core logic + HTTP)

---

## Task 1: Migration — `email_domain_claims`

**Files:** Create `apps/api/migrations/versions/0040_email_domain_claims.py`

- [ ] **Step 1: Write the migration**

Open `apps/api/migrations/versions/0039_org_subdomain.py` and `0038_sso_scim.py` for the exact `id`/`server_default`/index conventions this repo uses (ids are Postgres `text` defaulting to `gen_random_uuid()::text`). Create `0040_email_domain_claims.py`:

```python
"""email_domain_claims: domain -> org mapping for self-serve signup (W1 Phase 2A).

The first signup from a work-email domain creates an org and soft-claims the
domain here; later same-domain signups resolve their org through this table.
``email_domain_claims`` is the canonical domain registry (sso_connections will
be reconciled against it in Phase 3).
"""
from alembic import op
import sqlalchemy as sa

revision = "0040_email_domain_claims"
down_revision = "0039_org_subdomain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_domain_claims",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("claim_type", sa.Text(), nullable=False, server_default="soft_email"),
        sa.Column("join_policy", sa.Text(), nullable=False, server_default="auto"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("uq_email_domain_claims_domain", "email_domain_claims", ["domain"], unique=True)
    op.create_index("ix_email_domain_claims_org", "email_domain_claims", ["organization_id"])


def downgrade() -> None:
    op.drop_index("ix_email_domain_claims_org", table_name="email_domain_claims")
    op.drop_index("uq_email_domain_claims_domain", table_name="email_domain_claims")
    op.drop_table("email_domain_claims")
```
Revision id `0040_email_domain_claims` is 26 chars (≤ 31, safe).

- [ ] **Step 2: Apply + verify**

Run: `cd apps/api && python3.11 -m alembic upgrade head`
Expected: applies `0040_email_domain_claims`; `python3.11 -m alembic heads` shows it as head.
Then round-trip: `python3.11 -m alembic downgrade -1 && python3.11 -m alembic upgrade head` → clean.

- [ ] **Step 3: Commit**

```bash
git add apps/api/migrations/versions/0040_email_domain_claims.py
git commit -m "feat(w1-2a): email_domain_claims table (domain->org registry)"
```

---

## Task 2: `core/provisioning.py` — `provision_org`

**Files:** Create `apps/api/core/provisioning.py`; Test `apps/api/tests/test_provisioning.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_provisioning.py`:

```python
"""W1 Phase 2A — parameterized org provisioning."""
from __future__ import annotations

import uuid
import pytest
from sqlalchemy import select

from core.db import engine, reflect_table
from core.provisioning import ROOT, provision_org


@pytest.mark.asyncio
async def test_provision_org_creates_org_owner_and_context():
    slug = f"acme{uuid.uuid4().hex[:8]}"
    result = await provision_org(slug=slug, name="Acme Inc", owner_email="Founder@Acme.com")
    org_id, owner_id = result["org_id"], result["owner_member_id"]

    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        org = (await conn.execute(select(orgs).where(orgs.c.id == org_id))).mappings().one()
        owner = (await conn.execute(select(members).where(members.c.id == owner_id))).mappings().one()

    assert org["slug"] == slug
    assert org["subdomain"] == slug           # stored lowercase, == slug
    assert org["organization_id"] == org_id   # self-referential tenant id
    assert org["owner_member_id"] == owner_id
    assert owner["organization_id"] == org_id
    assert owner["role"] == "owner"
    assert owner["email"] == "founder@acme.com"  # lowercased
    # Context folder is written where context.load_org_context reads it.
    assert (ROOT / "context" / org_id / "org.md").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_provisioning.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.provisioning'`.

- [ ] **Step 3: Write the module**

Create `apps/api/core/provisioning.py`:

```python
"""Parameterized org genesis: create + provision a new tenant.

Mirrors the shape of ``seed.py`` (org row + owner member + ``context/{org}/org.md``
+ OpenFGA owner grant), but for any org rather than the seeded ``default`` one.
``ROOT`` matches ``core.context``'s loader root so the context folder is read at
runtime.
"""
from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import insert

from core import audit, permissions
from core.config import settings
from core.db import engine, reflect_table

# core/provisioning.py -> core -> apps/api (same dir seed.py uses and context loads).
ROOT = Path(__file__).resolve().parent.parent

_ORG_MD_TEMPLATE = "# {name}\n\nWelcome to Chronos. This is your organization's context folder.\n"


async def provision_org(
    *, slug: str, name: str, owner_email: str, owner_name: str | None = None, region: str | None = None
) -> dict:
    """Create a new org with ``slug`` as its (lowercase) subdomain and an owner member.

    Assumes ``slug`` is already validated unique and non-reserved (see core.signup).
    Returns ``{"org_id", "owner_member_id"}``.
    """
    region = region or settings.region
    slug = slug.lower()
    owner_email = owner_email.lower()
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())

    organizations = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(insert(organizations).values(
            id=org_id, organization_id=org_id, region=region,
            slug=slug, subdomain=slug, name=name,
            onboarding_state="new", owner_member_id=member_id,
        ))
        await conn.execute(insert(members).values(
            id=member_id, organization_id=org_id, region=region,
            email=owner_email, role="owner",
            name=owner_name or owner_email.split("@", 1)[0],
        ))

    # OpenFGA owner grant — no-op unless an OpenFGA server is configured.
    await permissions.grant_org_membership(member_id, org_id, admin=True)

    # Org context folder (local FS; read by core.context.load_org_context).
    ctx = ROOT / "context" / org_id
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "org.md").write_text(_ORG_MD_TEMPLATE.format(name=name))

    await audit.log(
        "org_provisioned", member_id, "provisioning.create_org",
        organization_id=org_id, resource_type="organization", resource_id=org_id,
        payload={"slug": slug},
    )
    return {"org_id": org_id, "owner_member_id": member_id}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_provisioning.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/provisioning.py apps/api/tests/test_provisioning.py
git commit -m "feat(w1-2a): provision_org parameterized org genesis"
```

---

## Task 3: `core/signup.py` helpers — free-email blocklist + slug derivation

**Files:** Create `apps/api/core/signup.py` (helpers only this task); Test `apps/api/tests/test_signup.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_signup.py`:

```python
"""W1 Phase 2A — signup helpers + decision logic."""
from __future__ import annotations

import uuid
import pytest

from core.signup import RESERVED_SLUGS, derive_slug, is_free_email_domain, unique_subdomain
from core.db import engine, reflect_table


@pytest.mark.parametrize("domain,expected", [
    ("gmail.com", True), ("outlook.com", True), ("yahoo.com", True), ("icloud.com", True),
    ("novatech.com", False), ("acme.io", False),
])
def test_is_free_email_domain(domain, expected):
    assert is_free_email_domain(domain) is expected


@pytest.mark.parametrize("raw,expected", [
    ("NovaTech", "novatech"),
    ("nova tech!!", "nova-tech"),
    ("a..b__c", "a-b-c"),
    ("---x---", "x"),
    ("", "org"),
])
def test_derive_slug(raw, expected):
    assert derive_slug(raw) == expected


def test_reserved_slugs_include_resolution_blocklist():
    for s in ("app", "www", "api", "admin", "default"):
        assert s in RESERVED_SLUGS


@pytest.mark.asyncio
async def test_unique_subdomain_suffixes_on_collision_and_reserved():
    base = f"taken{uuid.uuid4().hex[:8]}"
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=str(uuid.uuid4()), slug=base, subdomain=base, name="T",
        ))
    # Collision → a different, available label.
    out = await unique_subdomain(base)
    assert out != base and out.startswith(base)
    # Reserved label → never returned as-is.
    reserved_out = await unique_subdomain("api")
    assert reserved_out not in RESERVED_SLUGS
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'core.signup'`.

- [ ] **Step 3: Write the helpers**

Create `apps/api/core/signup.py`:

```python
"""Self-serve signup: domain classification, slug derivation, and the
create-org / join-org decision for a verified email."""
from __future__ import annotations

import re
import secrets

from sqlalchemy import select

from core.db import engine, reflect_table
from core.tenancy import RESERVED_LABELS

# Consumer email providers cannot claim a shared domain — they get a personal org.
FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "ymail.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
    "yandex.com", "zoho.com", "qq.com", "163.com",
})

# Subdomains that must never be assigned to a tenant (resolution blocklist +
# product-reserved hostnames).
RESERVED_SLUGS = RESERVED_LABELS | frozenset({
    "default", "signup", "login", "onboarding", "help", "support", "status",
    "docs", "blog", "mail", "dashboard", "account", "billing",
})


def is_free_email_domain(domain: str) -> bool:
    return domain.lower().strip() in FREE_EMAIL_DOMAINS


def derive_slug(base: str) -> str:
    """Lowercase, collapse non [a-z0-9-] to single hyphens, trim. Empty -> 'org'."""
    s = re.sub(r"[^a-z0-9-]+", "-", base.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "org"


async def unique_subdomain(candidate: str) -> str:
    """Return an available, non-reserved subdomain derived from ``candidate``."""
    label = derive_slug(candidate)
    if label in RESERVED_SLUGS:
        label = f"{label}-org"
    organizations = await reflect_table("organizations")

    async def _taken(value: str) -> bool:
        async with engine.begin() as conn:
            return (await conn.execute(
                select(organizations.c.id).where(organizations.c.subdomain == value)
            )).first() is not None

    if label not in RESERVED_SLUGS and not await _taken(label):
        return label
    while True:
        suffixed = f"{label}-{secrets.token_hex(2)}"
        if suffixed not in RESERVED_SLUGS and not await _taken(suffixed):
            return suffixed
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v`
Expected: PASS (the helper tests; `signup_or_join` tests come in Task 4).

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/signup.py apps/api/tests/test_signup.py
git commit -m "feat(w1-2a): signup helpers (free-email blocklist, slug derivation, unique subdomain)"
```

---

## Task 4: `signup_or_join` decision logic

**Files:** Modify `apps/api/core/signup.py`; Modify `apps/api/tests/test_signup.py`

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_signup.py`:

```python
from core.signup import signup_or_join


def _domain() -> str:
    return f"co{uuid.uuid4().hex[:8]}.com"


@pytest.mark.asyncio
async def test_unclaimed_work_domain_creates_org_and_claims():
    domain = _domain()
    email = f"founder@{domain}"
    res = await signup_or_join(email, org_name="Acme")
    assert res["created"] is True and res["role"] == "owner" and res["member_id"]
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        claim = (await conn.execute(claims.select().where(claims.c.domain == domain))).mappings().one()
    assert claim["organization_id"] == res["org_id"]
    assert claim["claim_type"] == "soft_email" and claim["join_policy"] == "auto"


@pytest.mark.asyncio
async def test_second_same_domain_signup_auto_joins_as_user():
    domain = _domain()
    first = await signup_or_join(f"founder@{domain}")
    second = await signup_or_join(f"teammate@{domain}")
    assert second["created"] is False and second["joined"] is True
    assert second["org_id"] == first["org_id"] and second["role"] == "user"
    assert second["member_id"] != first["member_id"]


@pytest.mark.asyncio
async def test_existing_member_signup_is_login_not_duplicate():
    domain = _domain()
    first = await signup_or_join(f"founder@{domain}")
    again = await signup_or_join(f"founder@{domain}")
    assert again["org_id"] == first["org_id"]
    assert again["member_id"] == first["member_id"]
    assert again["created"] is False and again["joined"] is False


@pytest.mark.asyncio
async def test_free_email_creates_personal_org_without_claim():
    email = f"person{uuid.uuid4().hex[:8]}@gmail.com"
    res = await signup_or_join(email)
    assert res["created"] is True and res["role"] == "owner"
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        rows = (await conn.execute(claims.select().where(claims.c.domain == "gmail.com"))).all()
    assert rows == []  # free-email domains are never claimed
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v -k "domain_creates or auto_joins or existing_member or free_email"`
Expected: FAIL — `ImportError: cannot import name 'signup_or_join'`.

- [ ] **Step 3: Implement `signup_or_join`**

Append to `apps/api/core/signup.py` (add imports `from sqlalchemy import insert` and `from core import audit`, `from core.config import settings`, `from core.members import get_member_in_org, provision_member`, `from core.provisioning import provision_org` at the top with the existing imports):

```python
async def _claim_for_domain(domain: str) -> dict | None:
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        row = (await conn.execute(
            select(claims).where(claims.c.domain == domain)
        )).mappings().first()
    return dict(row) if row else None


async def signup_or_join(email: str, org_name: str | None = None) -> dict:
    """Turn a verified ``email`` into an org membership.

    Returns a dict: ``org_id``, ``member_id`` (None if pending approval), ``role``,
    ``created`` (new org), ``joined`` (joined an existing org), and optionally
    ``status`` == "pending_approval".
    """
    email = email.lower().strip()
    domain = email.split("@", 1)[1]

    # Free-email addresses get a personal org and never claim the shared domain.
    if is_free_email_domain(domain):
        local = email.split("@", 1)[0]
        sub = await unique_subdomain(local)
        prov = await provision_org(slug=sub, name=org_name or f"{local}'s workspace", owner_email=email)
        return {"org_id": prov["org_id"], "member_id": prov["owner_member_id"],
                "role": "owner", "created": True, "joined": False}

    claim = await _claim_for_domain(domain)
    if claim is not None:
        org_id = claim["organization_id"]
        existing = await get_member_in_org(org_id, email=email)
        if existing is not None:
            return {"org_id": org_id, "member_id": existing.id, "role": existing.role,
                    "created": False, "joined": False}
        if claim["join_policy"] == "approval":
            members = await reflect_table("members")
            import uuid as _uuid
            member_id = str(_uuid.uuid4())
            async with engine.begin() as conn:
                await conn.execute(insert(members).values(
                    id=member_id, organization_id=org_id, region=settings.region,
                    email=email, role="user", name=email.split("@", 1)[0],
                    status="pending_approval",
                ))
            await audit.log("signup_pending_approval", member_id, "signup.join",
                            organization_id=org_id, payload={"domain": domain})
            return {"org_id": org_id, "member_id": None, "role": "user",
                    "created": False, "joined": False, "status": "pending_approval"}
        # auto-join
        member = await provision_member(org_id, email, role="user")
        return {"org_id": org_id, "member_id": member.id, "role": "user",
                "created": False, "joined": True}

    # Unclaimed work domain: create the org, make the signer owner, soft-claim.
    sub = await unique_subdomain(domain.split(".", 1)[0])
    prov = await provision_org(slug=sub, name=org_name or domain.split(".", 1)[0].title(), owner_email=email)
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        await conn.execute(insert(claims).values(
            organization_id=prov["org_id"], region=settings.region, domain=domain,
            claim_type="soft_email", join_policy="auto",
        ))
    await audit.log("domain_soft_claimed", prov["owner_member_id"], "signup.claim_domain",
                    organization_id=prov["org_id"], payload={"domain": domain})
    return {"org_id": prov["org_id"], "member_id": prov["owner_member_id"],
            "role": "owner", "created": True, "joined": False}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v`
Expected: PASS (helpers + all four decision tests).

- [ ] **Step 5: Commit**

```bash
git add apps/api/core/signup.py apps/api/tests/test_signup.py
git commit -m "feat(w1-2a): signup_or_join (create-org / auto-join / personal-org)"
```

---

## Task 5: `POST /auth/signup` endpoint + OTP-consume refactor

**Files:** Modify `apps/api/routers/auth.py`; Modify `apps/api/tests/test_signup.py`

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/test_signup.py`:

```python
import httpx
import main
from routers.auth import _otp_store


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


def _seed_otp(email: str, code: str = "123456") -> None:
    import time
    _otp_store[email.lower()] = {"code": code, "expires_at": time.time() + 300, "attempts": 0}


@pytest.mark.asyncio
async def test_signup_endpoint_creates_org_for_unclaimed_domain():
    domain = _domain()
    email = f"founder@{domain}"
    _seed_otp(email)
    async with _client() as client:
        resp = await client.post("/auth/signup", json={"email": email, "code": "123456", "org_name": "Acme"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True and body["org_id"] and body["member_id"]
    assert body["access_token"]


@pytest.mark.asyncio
async def test_signup_endpoint_rejects_bad_otp():
    email = f"founder@{_domain()}"
    _seed_otp(email, code="111111")
    async with _client() as client:
        resp = await client.post("/auth/signup", json={"email": email, "code": "999999"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_signup_endpoint_second_same_domain_auto_joins():
    domain = _domain()
    _seed_otp(f"founder@{domain}")
    async with _client() as client:
        await client.post("/auth/signup", json={"email": f"founder@{domain}", "code": "123456"})
        _seed_otp(f"teammate@{domain}")
        resp = await client.post("/auth/signup", json={"email": f"teammate@{domain}", "code": "123456"})
    assert resp.status_code == 200
    assert resp.json()["created"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v -k "signup_endpoint"`
Expected: FAIL — 404 (route missing) or 405.

- [ ] **Step 3: Refactor OTP check + add the endpoint**

In `apps/api/routers/auth.py`:

First, extract the code-verification used by `verify_otp` into a reusable helper. Add this function near the top (after `_dev_otp_enabled`):

```python
def _consume_otp(email: str, code: str) -> None:
    """Validate and consume a dev OTP for ``email``. Raises HTTPException on failure."""
    email = email.lower()
    entry = _otp_store.get(email)
    if entry is None or entry["expires_at"] < time.time():
        _otp_store.pop(email, None)
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    if entry["attempts"] >= _OTP_MAX_ATTEMPTS:
        _otp_store.pop(email, None)
        raise HTTPException(status_code=429, detail="Too many attempts; request a new code")
    entry["attempts"] += 1
    if not secrets.compare_digest(code, entry["code"]):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    _otp_store.pop(email, None)
```

Then refactor `verify_otp` to use it: replace the inline block (from `entry = _otp_store.get(email)` through `_otp_store.pop(email, None)` that consumes on success) with a single call `_consume_otp(email, req.code)`. Keep the rest of `verify_otp` (member lookup, token, cookie) unchanged.

Add the signup request model near the other models (top of file):
```python
class SignupRequest(BaseModel):
    email: EmailStr
    code: str
    org_name: str | None = None
```

Add imports at the top: `from core.signup import signup_or_join`.

Add the endpoint (place it after `verify_otp`):
```python
@router.post("/signup")
async def signup(req: SignupRequest, response: Response) -> dict:
    """Self-serve signup: verify the work email (dev OTP), then create or join an org."""
    if not _dev_otp_enabled():
        raise HTTPException(status_code=404, detail="Self-serve signup requires dev OTP in this environment")
    email = req.email.lower()
    _consume_otp(email, req.code)
    result = await signup_or_join(email, org_name=req.org_name)
    if result.get("status") == "pending_approval":
        await audit.log("signup_pending", email, "auth.signup", organization_id=result["org_id"])
        return {"status": "pending_approval", "org_id": result["org_id"]}
    # Phase 2A issues a legacy org-less token (grandfathered); Phase 2B flips this
    # to create_access_token(member_id, org_id=result["org_id"]).
    token = create_access_token(result["member_id"])
    set_session_cookie(response, token)
    await audit.log("signup_completed", result["member_id"], "auth.signup",
                    organization_id=result["org_id"], payload={"created": result["created"]})
    return {"access_token": token, "token_type": "bearer", "member_id": result["member_id"],
            "org_id": result["org_id"], "created": result["created"]}
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v`
Expected: PASS (all signup tests incl. the HTTP ones). Also run `cd apps/api && python3.11 -m pytest tests/test_auth_otp.py -v 2>/dev/null || python3.11 -m pytest -q -k otp` to confirm the `verify_otp` refactor didn't regress existing OTP tests.

- [ ] **Step 5: Commit**

```bash
git add apps/api/routers/auth.py apps/api/tests/test_signup.py
git commit -m "feat(w1-2a): POST /auth/signup (OTP-verified self-serve org creation)"
```

---

## Task 6: Regression gate

**Files:** none (verification only).

- [ ] **Step 1: Full suite**

Run: `cd apps/api && python3.11 -m pytest -q 2>&1 | tail -4`
Expected: prior baseline (775 passed before this plan) + the new provisioning/signup tests; the ~17 pre-existing optional-import failures unchanged; ZERO new failures. If `verify_otp` or auth tests fail, the OTP refactor regressed — STOP and report.

- [ ] **Step 2: Clean migration on a throwaway DB**

Run:
```bash
cd apps/api && export PGPASSWORD=chronos
psql -h localhost -U chronos -d chronos -c "DROP DATABASE IF EXISTS chronos_2a_verify;" -c "CREATE DATABASE chronos_2a_verify;"
DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:5432/chronos_2a_verify" REDIS_URL="redis://localhost:6379/0" VAULT_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" OBJECT_STORAGE_BACKEND="s3" AWS_S3_BUCKET="chronos-ci-local-fallback" AWS_S3_REGION="us-east-1" python3.11 -m alembic upgrade head
psql -h localhost -U chronos -d chronos -c "DROP DATABASE chronos_2a_verify;"
```
Expected: `upgrade head` reaches `0040_email_domain_claims` on the fresh DB with no error.

- [ ] **Step 3: Commit (if any test-only fixups were needed)**

```bash
git add -A && git commit -m "test(w1-2a): green suite with signup + provisioning" || echo "nothing to commit"
```

---

## Subsequent Phase 2 sub-plans (roadmap — each its own plan)

- **Phase 2B — Org-bound token flip + handoff + C1/C2.** Flip all mint sites (`/auth/signup`, `verify_otp`, `cognito/callback`, `cognito/verify`, `sso.py`) to `create_access_token(member_id, org_id=...)`. Add `POST /auth/handoff` issuing/consuming the short-lived cross-subdomain handoff token (spec §2e). Enforce **C1**: reject org-bound tokens when `resolved_org_id is None` *except* the handoff endpoint. Add `settings.enforce_org_bound_tokens`; when on, `get_current_member` rejects org-less tokens (**C2** — closes grandfathering). Migrate the test harness to subdomain base URLs / `X-Chronos-Org`. This is the phase that makes tenancy *live*.
- **Phase 2C — Minimal signup + onboarding UI.** Next.js signup page (email → request-OTP → enter code + org name → `POST /auth/signup`), landing on the new org's subdomain; first-run onboarding (org name/branding, invite teammates via existing invitations). Replace env-only base-URL logic in `apps/web/lib/api.ts` (+ duplicated copies) with `window.location.hostname` derivation.
- **Phase 3 — DNS-TXT hard domain verification + SSO registry reconciliation** (`domain_verifications` table; validate `sso_connections.email_domain` against a hard claim).

---

## Self-Review

**Spec coverage (2A scope):** §2b signup decision (unclaimed→create+claim, claimed→auto-join, free-email→personal) → Task 4 + Task 5; §2c provisioning (org + owner + context + OpenFGA) → Task 2; §3 `email_domain_claims` → Task 1; §4 free-email blocklist + soft-claim + auto-join exposure (lowest role `user`) → Tasks 3–4; Phase-1 case-sensitivity carry-item (lowercase subdomain) → Task 2 (`slug.lower()`) + Task 3 (`derive_slug` lowercases). Deferred-and-documented: org-bound token flip/handoff/C1/C2 (2B), UI (2C), DNS-TXT + SSO reconciliation (Phase 3), `join_policy="approval"` full approval inbox (only the pending-member write is in 2A).

**Placeholder scan:** none — every code step is complete; every run step has an exact command + expected result. The roadmap section is forward context, not steps.

**Type consistency:** `provision_org(*, slug, name, owner_email, owner_name=None, region=None) -> {"org_id","owner_member_id"}` defined in Task 2 and called in Task 4 with those keys. `signup_or_join(email, org_name=None) -> {"org_id","member_id","role","created","joined"[,"status"]}` defined Task 4, consumed Task 5. `derive_slug`/`is_free_email_domain`/`unique_subdomain`/`RESERVED_SLUGS` defined Task 3, used Task 4. `_consume_otp(email, code)` defined Task 5 and reused by `verify_otp`. `_otp_store` (module global in `routers/auth.py`) seeded directly in Task 5 tests. `provision_member(org_id, email, role=...)` and `get_member_in_org(org_id, email=...)` are the existing `core/members.py` signatures. `create_access_token(member_id)` org-less in 2A (flip noted for 2B).
