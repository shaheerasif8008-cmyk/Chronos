"""DNS-TXT domain verification: prove an org controls a domain, upgrading its
soft email-claim to a hard (verified_dns) claim."""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import select

from core import audit
from core.config import settings
from core.db import engine, reflect_table

_TXT_PREFIX = "chronos-verify="


def _lookup_txt(domain: str) -> list[str]:
    """Return the TXT records for ``domain``. Isolated for monkeypatching in tests."""
    import dns.resolver

    try:
        answers = dns.resolver.resolve(domain, "TXT")
    except Exception:
        return []
    out: list[str] = []
    for rdata in answers:
        if hasattr(rdata, "strings"):
            out.append(b"".join(rdata.strings).decode("utf-8", "ignore"))
        else:
            out.append(str(rdata).strip('"'))
    return out


async def start_domain_verification(org_id: str, domain: str) -> dict:
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
                txt_token=token, status="pending"))
    await audit.log("domain_verification_started", org_id, "domains.verify_start",
                    organization_id=org_id, resource_type="domain", resource_id=domain)
    return {"name": f"_chronos.{domain}", "type": "TXT", "value": f"{_TXT_PREFIX}{token}"}


async def check_domain_verification(org_id: str, domain: str) -> bool:
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
        ).values(status="verified", verified_at=datetime.now(timezone.utc)))
        await conn.execute(claims.update().where(
            claims.c.organization_id == org_id, claims.c.domain == domain
        ).values(claim_type="verified_dns"))
    await audit.log("domain_verified", org_id, "domains.verify_check",
                    organization_id=org_id, resource_type="domain", resource_id=domain)
    return True


async def is_domain_hard_claimed(org_id: str, domain: str) -> bool:
    domain = domain.lower().strip()
    claims = await reflect_table("email_domain_claims")
    async with engine.begin() as conn:
        row = (await conn.execute(select(claims.c.claim_type).where(
            claims.c.organization_id == org_id, claims.c.domain == domain))).first()
    return bool(row) and row[0] == "verified_dns"
