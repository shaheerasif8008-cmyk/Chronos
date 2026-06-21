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

RESERVED_LABELS = frozenset({"app", "www", "api", "admin", "static", "assets"})

_DEV_SUFFIXES = (".localhost", ".lvh.me")


def extract_tenant_label(host: str, *, base_domain: str | None = None) -> str | None:
    """Return the tenant subdomain label from ``host``, or ``None``."""
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
