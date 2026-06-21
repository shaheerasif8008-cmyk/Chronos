"""W1 Phase 1 — tenant label extraction from a Host header."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from core.db import engine, reflect_table
from core.tenancy import RESERVED_LABELS, extract_tenant_label, resolve_org_id


@pytest.mark.parametrize(
    "host,expected",
    [
        ("novatech.cognisiatech.com", "novatech"),
        ("novatech.cognisiatech.com:443", "novatech"),
        ("acme.localhost", "acme"),
        ("acme.localhost:8000", "acme"),
        ("acme.lvh.me", "acme"),
        ("cognisiatech.com", None),
        ("www.cognisiatech.com", None),
        ("app.cognisiatech.com", None),
        ("api.cognisiatech.com", None),
        ("localhost", None),
        ("test", None),
        ("", None),
        ("a.b.cognisiatech.com", None),   # multi-level subdomain — must reject
        ("a.b.localhost", None),          # multi-level dev host — must reject
    ],
)
def test_extract_tenant_label(host, expected):
    assert extract_tenant_label(host, base_domain="cognisiatech.com") == expected


def test_reserved_labels_are_blocked():
    for label in ("app", "www", "api", "admin"):
        assert label in RESERVED_LABELS


async def _make_org(subdomain: str) -> str:
    org_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=subdomain, subdomain=subdomain, name="T",
        ))
    return org_id


@pytest.mark.asyncio
async def test_resolve_org_id_by_subdomain_host():
    sub = f"acme{uuid.uuid4().hex[:8]}"
    org_id = await _make_org(sub)
    assert await resolve_org_id(f"{sub}.cognisiatech.com", None) == org_id


@pytest.mark.asyncio
async def test_resolve_org_id_dev_header_override_in_non_production():
    sub = f"acme{uuid.uuid4().hex[:8]}"
    org_id = await _make_org(sub)
    # Host carries no tenant; the dev X-Chronos-Org header supplies it.
    assert await resolve_org_id("localhost", sub) == org_id


@pytest.mark.asyncio
async def test_resolve_org_id_header_inert_in_production():
    # In production the X-Chronos-Org override must be ignored. Plain localhost
    # host then resolves to no tenant. Mock settings so is_production is True.
    fake = MagicMock()
    fake.is_production = True
    fake.base_domain = "cognisiatech.com"
    with patch("core.tenancy.settings", fake):
        assert await resolve_org_id("localhost", "novatech") is None


@pytest.mark.asyncio
async def test_resolve_org_id_unknown_subdomain_returns_none():
    assert await resolve_org_id(f"nope{uuid.uuid4().hex[:8]}.cognisiatech.com", None) is None
