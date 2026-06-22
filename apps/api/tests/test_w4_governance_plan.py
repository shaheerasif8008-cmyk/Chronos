"""W4 — plan entitlements supply the governance budget default."""
from __future__ import annotations

import uuid
import pytest

from core import governance
from core.db import engine, reflect_table


async def _org(plan: str) -> str:
    org_id = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", plan=plan))
    return org_id


@pytest.mark.asyncio
async def test_plan_supplies_budget_default():
    # An org on 'trial' with no explicit override should resolve trial's budget.
    org_id = await _org("trial")
    cfg = await governance.governance_config(org_id)
    # trial: daily_token_limit=200_000, daily_cost_limit_usd=5.0 (from core/plans.py)
    assert cfg.daily_token_limit == 200_000
    assert cfg.daily_cost_limit_usd == 5.0


@pytest.mark.asyncio
async def test_pro_plan_supplies_larger_budget_default():
    # An org on 'pro' should resolve pro's budget.
    org_id = await _org("pro")
    cfg = await governance.governance_config(org_id)
    # pro: daily_token_limit=5_000_000, daily_cost_limit_usd=100.0 (from core/plans.py)
    assert cfg.daily_token_limit == 5_000_000
    assert cfg.daily_cost_limit_usd == 100.0


@pytest.mark.asyncio
async def test_enterprise_plan_supplies_unlimited_defaults():
    # enterprise: 0 == unlimited (matches governance's "0 == unlimited" convention).
    org_id = await _org("enterprise")
    cfg = await governance.governance_config(org_id)
    assert cfg.daily_token_limit == 0
    assert cfg.daily_cost_limit_usd == 0.0
