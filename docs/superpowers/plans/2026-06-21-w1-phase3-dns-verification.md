# W1 Phase 3 — DNS-TXT Hard Domain Verification + SSO Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An org can prove it controls a domain (DNS-TXT), upgrading its soft email-claim to a **hard (`verified_dns`) claim**; and SSO connections can only be configured for a domain the org has hard-claimed — making `email_domain_claims` the canonical domain registry over `sso_connections`.

**Architecture:** A `domain_verifications` table holds a per-(org,domain) TXT token + status. `core/domains.py` starts a verification (generate token → return the `TXT` record to publish) and checks it (DNS lookup behind a monkeypatchable `_lookup_txt`; on match → mark verified + upgrade the `email_domain_claims.claim_type` to `verified_dns`). Admin-gated endpoints expose start/check. SSO connection create/update validates `email_domain` is hard-claimed by the caller's org.

**Tech Stack:** FastAPI, SQLAlchemy Core, Alembic, `dnspython` (already installed), pytest, Postgres.

**Spec:** `docs/superpowers/specs/2026-06-20-w1-tenant-onboarding-identity-design.md` (§3 `domain_verifications`, §4 hard claim, §2f domain registry single source of truth).

**Base:** builds on Phase 2A's `email_domain_claims` (`claim_type` in {`soft_email`, `verified_dns`}) and the Phase 2B/2C work.

**Environment** (export before any python/pytest; `python3.11`):
```bash
export DATABASE_URL="postgresql+asyncpg://chronos:chronos@localhost:5432/chronos" \
  REDIS_URL="redis://localhost:6379/0" \
  VAULT_ENCRYPTION_KEY="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" \
  OBJECT_STORAGE_BACKEND="s3" AWS_S3_BUCKET="chronos-ci-local-fallback" AWS_S3_REGION="us-east-1"
```
Authoritative runs use a FRESH DB (`chronos_p3`), `alembic upgrade head`, then drop. Current head: confirm with `python3.11 -m alembic heads` (should be `0040_email_domain_claims`).

---

## File Structure
- `apps/api/migrations/versions/0041_domain_verifications.py` — **Create.** `domain_verifications` table.
- `apps/api/core/domains.py` — **Create.** `start_domain_verification`, `check_domain_verification`, `is_domain_hard_claimed`, `_lookup_txt`.
- `apps/api/routers/domains.py` — **Create.** Admin-gated start/check endpoints.
- `apps/api/main.py` — **Modify.** Register the domains router.
- `apps/api/routers/sso.py` — **Modify.** Validate `email_domain` is hard-claimed in create/update connection.
- Tests: `apps/api/tests/test_domain_verification.py`, additions to an SSO test.

---

## Task 1: Migration — `domain_verifications`

**Files:** Create `apps/api/migrations/versions/0041_domain_verifications.py`

- [ ] **Step 1: Write the migration**

Mirror the id/server_default/index style of `0040_email_domain_claims.py`. Create `0041_domain_verifications.py`:
```python
"""domain_verifications: DNS-TXT proof of domain ownership (W1 Phase 3).

A verified row upgrades the org's email_domain_claims.claim_type to 'verified_dns'
(a hard claim), which is required to configure SSO for that domain.
"""
from alembic import op
import sqlalchemy as sa

revision = "0041_domain_verifications"
down_revision = "0040_email_domain_claims"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domain_verifications",
        sa.Column("id", sa.Text(), primary_key=True, server_default=sa.text("gen_random_uuid()::text")),
        sa.Column("organization_id", sa.Text(), nullable=False, server_default="default"),
        sa.Column("region", sa.Text(), nullable=False, server_default="us"),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("txt_token", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("uq_domain_verifications_org_domain", "domain_verifications",
                    ["organization_id", "domain"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_domain_verifications_org_domain", table_name="domain_verifications")
    op.drop_table("domain_verifications")
```

- [ ] **Step 2: Apply + round-trip**

Run: `cd apps/api && python3.11 -m alembic upgrade head` then `python3.11 -m alembic downgrade -1 && python3.11 -m alembic upgrade head`. Expected: clean both ways; head is `0041_domain_verifications`.

- [ ] **Step 3: Commit**
```bash
git add apps/api/migrations/versions/0041_domain_verifications.py
git commit -m "feat(w1-3): domain_verifications table"
```

---

## Task 2: `core/domains.py` — verification logic

**Files:** Create `apps/api/core/domains.py`; Test `apps/api/tests/test_domain_verification.py`

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/test_domain_verification.py`:
```python
"""W1 Phase 3 — DNS-TXT domain verification."""
from __future__ import annotations

import uuid
import pytest

from core import domains
from core.db import engine, reflect_table


async def _seed_claim(domain: str) -> str:
    """Create an org + a soft claim for `domain`. Returns org_id."""
    org_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T"))
        await conn.execute(claims.insert().values(
            organization_id=org_id, domain=domain, claim_type="soft_email", join_policy="auto",
        ))
    return org_id


@pytest.mark.asyncio
async def test_start_returns_txt_record_and_persists_pending():
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    rec = await domains.start_domain_verification(org_id, domain)
    assert rec["name"].endswith(domain) and rec["value"].startswith("chronos-verify=")
    verifs = await reflect_table("domain_verifications")
    async with engine.begin() as conn:
        row = (await conn.execute(verifs.select().where(
            verifs.c.organization_id == org_id, verifs.c.domain == domain))).mappings().one()
    assert row["status"] == "pending" and row["txt_token"] in rec["value"]


@pytest.mark.asyncio
async def test_check_verifies_and_upgrades_claim_when_txt_present(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    rec = await domains.start_domain_verification(org_id, domain)
    token = rec["value"].split("=", 1)[1]
    # Simulate DNS returning the published TXT record.
    monkeypatch.setattr(domains, "_lookup_txt", lambda d: [f"chronos-verify={token}", "v=spf1 -all"])
    ok = await domains.check_domain_verification(org_id, domain)
    assert ok is True
    assert await domains.is_domain_hard_claimed(org_id, domain) is True
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        ct = (await conn.execute(claims.select().where(
            claims.c.organization_id == org_id, claims.c.domain == domain))).mappings().one()["claim_type"]
    assert ct == "verified_dns"


@pytest.mark.asyncio
async def test_check_fails_when_txt_absent(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    await domains.start_domain_verification(org_id, domain)
    monkeypatch.setattr(domains, "_lookup_txt", lambda d: ["unrelated-record"])
    assert await domains.check_domain_verification(org_id, domain) is False
    assert await domains.is_domain_hard_claimed(org_id, domain) is False
```
Run `python3.11 -m pytest tests/test_domain_verification.py -v` → FAIL (no module core.domains).

- [ ] **Step 2: Implement**

Create `apps/api/core/domains.py`:
```python
"""DNS-TXT domain verification: prove an org controls a domain, upgrading its
soft email-claim to a hard (verified_dns) claim."""
from __future__ import annotations

import secrets

from sqlalchemy import select

from core import audit
from core.config import settings
from core.db import engine, reflect_table

_TXT_PREFIX = "chronos-verify="


def _lookup_txt(domain: str) -> list[str]:
    """Return the TXT records for ``domain``. Isolated for monkeypatching in tests."""
    import dns.resolver  # imported lazily so the module loads without network

    try:
        answers = dns.resolver.resolve(domain, "TXT")
    except Exception:
        return []
    out: list[str] = []
    for rdata in answers:
        out.append(b"".join(rdata.strings).decode("utf-8", "ignore")
                   if hasattr(rdata, "strings") else str(rdata).strip('"'))
    return out


async def start_domain_verification(org_id: str, domain: str) -> dict:
    """Generate (or refresh) a TXT token for (org, domain). Returns the DNS record."""
    domain = domain.lower().strip()
    token = secrets.token_urlsafe(24)
    verifs = await reflect_table("domain_verifications")
    async with engine.begin() as conn:
        existing = (await conn.execute(select(verifs.c.id).where(
            verifs.c.organization_id == org_id, verifs.c.domain == domain))).first()
        if existing is not None:
            await conn.execute(verifs.update().where(
                verifs.c.organization_id == org_id, verifs.c.domain == domain
            ).values(txt_token=token, status="pending", verified_at=None))
        else:
            await conn.execute(verifs.insert().values(
                organization_id=org_id, region=settings.region, domain=domain,
                txt_token=token, status="pending",
            ))
    await audit.log("domain_verification_started", org_id, "domains.verify_start",
                    organization_id=org_id, resource_type="domain", resource_id=domain)
    return {"name": f"_chronos.{domain}", "type": "TXT", "value": f"{_TXT_PREFIX}{token}"}


async def check_domain_verification(org_id: str, domain: str) -> bool:
    """Look up the domain's TXT records; if our token is present, mark verified and
    upgrade the email_domain_claims claim_type to 'verified_dns'."""
    domain = domain.lower().strip()
    verifs = await reflect_table("domain_verifications")
    async with engine.begin() as conn:
        row = (await conn.execute(select(verifs).where(
            verifs.c.organization_id == org_id, verifs.c.domain == domain))).mappings().first()
    if row is None:
        return False
    expected = f"{_TXT_PREFIX}{row['txt_token']}"
    records = _lookup_txt(f"_chronos.{domain}") + _lookup_txt(domain)
    if expected not in records:
        return False
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        await conn.execute(verifs.update().where(
            verifs.c.organization_id == org_id, verifs.c.domain == domain
        ).values(status="verified", verified_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc)))
        await conn.execute(claims.update().where(
            claims.c.organization_id == org_id, claims.c.domain == domain
        ).values(claim_type="verified_dns"))
    await audit.log("domain_verified", org_id, "domains.verify_check",
                    organization_id=org_id, resource_type="domain", resource_id=domain)
    return True


async def is_domain_hard_claimed(org_id: str, domain: str) -> bool:
    """True if ``org_id`` holds a DNS-verified claim on ``domain``."""
    domain = domain.lower().strip()
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        row = (await conn.execute(select(claims.c.claim_type).where(
            claims.c.organization_id == org_id, claims.c.domain == domain))).first()
    return bool(row) and row[0] == "verified_dns"
```
(The inline `__import__("datetime")` is ugly — prefer a top-level `from datetime import datetime, timezone` and use `datetime.now(timezone.utc)`. Implement it cleanly with the top-level import.)

Run `python3.11 -m pytest tests/test_domain_verification.py -v` → PASS.

- [ ] **Step 3: Commit**
```bash
git add apps/api/core/domains.py apps/api/tests/test_domain_verification.py
git commit -m "feat(w1-3): DNS-TXT domain verification core (start/check/hard-claim)"
```

---

## Task 3: Admin-gated verification endpoints

**Files:** Create `apps/api/routers/domains.py`; Modify `apps/api/main.py`; Test (HTTP) in `test_domain_verification.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_domain_verification.py`:
```python
import httpx
import main
from core.auth import create_access_token


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _admin_in_org(org_id: str) -> str:
    members = await reflect_table("members")
    mid = str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(members.insert().values(
            id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="admin"))
    return create_access_token(mid, org_id=org_id)


@pytest.mark.asyncio
async def test_verify_start_endpoint_admin_only(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    token = await _admin_in_org(org_id)
    async with _client() as client:
        resp = await client.post("/domains/verify/start", json={"domain": domain},
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": (
                                     await reflect_table("organizations"))  # placeholder; see note
                                 and None})
    # See implementation note: X-Chronos-Org must equal the org's subdomain.
```
NOTE: simplify the test — fetch the org subdomain and pass it as `X-Chronos-Org`. Final test:
```python
@pytest.mark.asyncio
async def test_verify_start_then_check_via_http(monkeypatch):
    domain = f"verify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)
    token = await _admin_in_org(org_id)
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        sub = (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()["subdomain"]
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": sub}
    async with _client() as client:
        start = await client.post("/domains/verify/start", json={"domain": domain}, headers=headers)
        assert start.status_code == 200
        rec = start.json()
        tok = rec["value"].split("=", 1)[1]
        monkeypatch.setattr("core.domains._lookup_txt", lambda d: [f"chronos-verify={tok}"])
        check = await client.post("/domains/verify/check", json={"domain": domain}, headers=headers)
        assert check.status_code == 200 and check.json()["verified"] is True
```
Run `python3.11 -m pytest tests/test_domain_verification.py -v -k "http"` → FAIL (router missing / 404).

- [ ] **Step 2: Implement the router**

Create `apps/api/routers/domains.py`:
```python
"""Admin endpoints to verify domain ownership via DNS-TXT (W1 Phase 3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import domains, permissions
from core.auth import get_current_member
from core.models import Member

router = APIRouter(prefix="/domains", tags=["domains"])


class DomainRequest(BaseModel):
    domain: str


@router.post("/verify/start")
async def verify_start(req: DomainRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    record = await domains.start_domain_verification(member.organization_id, req.domain)
    return {"record": record}


@router.post("/verify/check")
async def verify_check(req: DomainRequest, member: Member = Depends(get_current_member)) -> dict:
    await permissions.check(member, "manage_sso", member.organization_id)
    verified = await domains.check_domain_verification(member.organization_id, req.domain)
    if not verified:
        raise HTTPException(status_code=400, detail="TXT record not found or does not match")
    return {"verified": True}
```
In `apps/api/main.py`, import and register the router: add `domains` to the `from routers import (...)` list and add `app.include_router(domains.router)` next to the other includes.

Run `python3.11 -m pytest tests/test_domain_verification.py -v` → all pass.

- [ ] **Step 3: Commit**
```bash
git add apps/api/routers/domains.py apps/api/main.py apps/api/tests/test_domain_verification.py
git commit -m "feat(w1-3): admin DNS-TXT verify endpoints (start/check)"
```

---

## Task 4: SSO reconciliation — connections require a hard-claimed domain

**Files:** Modify `apps/api/routers/sso.py`; Test (additions)

- [ ] **Step 1: Write the failing test**

Add a test (in `test_domain_verification.py` or a new `test_sso_reconciliation.py`) that creating an SSO connection for a domain the org has NOT hard-claimed is rejected, and succeeds once verified:
```python
@pytest.mark.asyncio
async def test_sso_connection_requires_hard_claimed_domain(monkeypatch):
    domain = f"ssoverify{uuid.uuid4().hex[:8]}.com"
    org_id = await _seed_claim(domain)  # soft claim only
    token = await _admin_in_org(org_id)
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        sub = (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()["subdomain"]
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": sub}
    body = {"issuer": "https://idp.example.com", "client_id": "abc", "client_secret": "s",
            "email_domain": domain}
    async with _client() as client:
        # Soft claim → rejected.
        r1 = await client.post("/sso/connections", json=body, headers=headers)
        assert r1.status_code == 403
        # Hard-claim the domain, then it succeeds.
        rec = await domains.start_domain_verification(org_id, domain)
        tok = rec["value"].split("=", 1)[1]
        monkeypatch.setattr("core.domains._lookup_txt", lambda d: [f"chronos-verify={tok}"])
        await domains.check_domain_verification(org_id, domain)
        r2 = await client.post("/sso/connections", json=body, headers=headers)
        assert r2.status_code == 200
```
NOTE: match the real `SSOConnectionInput` field names in `routers/sso.py` (issuer/client_id/client_secret/email_domain) — read the model and adjust the body. If creating a connection has other required fields, include them.

Run → FAIL (soft-claim connection currently returns 200, not 403).

- [ ] **Step 2: Implement the gate**

In `apps/api/routers/sso.py` `create_connection` (and `update_connection`), when `req.email_domain` is provided, validate it is hard-claimed by the caller's org before inserting. Add `from core.domains import is_domain_hard_claimed` and, where `email_domain` is set into `values`:
```python
    if req.email_domain:
        domain = req.email_domain.lower().strip()
        if not await is_domain_hard_claimed(member.organization_id, domain):
            raise HTTPException(status_code=403,
                                detail="Domain must be DNS-verified by this org before configuring SSO")
        values["email_domain"] = domain
```
Apply the same check in `update_connection`. (`HTTPException` is already imported in sso.py.)

Run `python3.11 -m pytest tests/test_domain_verification.py tests/test_sso_scim.py -v` → all pass (the existing SSO tests: if any create a connection with an `email_domain` for an unverified domain, they'll now 403 — update those tests to hard-claim the domain first, OR to omit `email_domain`. Read `test_sso_scim.py` and adjust only the connection-creation setup, noting the change; do NOT weaken assertions about SSO crypto/routing).

- [ ] **Step 3: Commit**
```bash
git add apps/api/routers/sso.py apps/api/tests/  # include any adjusted sso test
git commit -m "feat(w1-3): SSO connections require a DNS-hard-claimed domain (registry reconciliation)"
```

---

## Task 5: Regression gate (fresh DB)

- [ ] **Step 1:** Full suite on a fresh `chronos_p3` DB (create, `alembic upgrade head`, `pytest -q`, drop). Expected: only the 14 known `test_doc_authoring` optional-import failures; zero new. If an SSO test fails, it likely creates a connection for an unverified domain — fix per Task 4 Step 2's note.

---

## Self-Review

**Spec coverage:** `domain_verifications` table → Task 1; DNS-TXT start/check + claim upgrade (§4 hard claim) → Task 2; admin endpoints → Task 3; `email_domain_claims` canonical over `sso_connections` (§2f) → Task 4 (SSO requires a hard claim). 

**Placeholder scan:** Task 2's `__import__("datetime")` is flagged to be replaced with a clean top-level import. Task 3 Step 1's first sketch is superseded by the "Final test" below it. Otherwise full code.

**Type consistency:** `start_domain_verification(org_id, domain) -> {name,type,value}`, `check_domain_verification(org_id, domain) -> bool`, `is_domain_hard_claimed(org_id, domain) -> bool`, `_lookup_txt(domain) -> list[str]` defined in Task 2 and used in Tasks 3-4 + tests. `claim_type` values `soft_email`/`verified_dns` match the 0040 schema. The SSO `SSOConnectionInput` field names must be confirmed against `routers/sso.py` (Task 4 Step 1 note).
