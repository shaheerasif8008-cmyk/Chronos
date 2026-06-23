"""Plan tiers and entitlements (W4). Static map; the org's tier is organizations.plan."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entitlements:
    plan: str
    max_seats: int
    daily_cost_limit_usd: float
    daily_token_limit: int
    features: frozenset[str] = field(default_factory=frozenset)


_PLANS: dict[str, Entitlements] = {
    "trial": Entitlements("trial", max_seats=3, daily_cost_limit_usd=5.0, daily_token_limit=200_000,
                          features=frozenset({"chat", "projects"})),
    "pro": Entitlements("pro", max_seats=25, daily_cost_limit_usd=100.0, daily_token_limit=5_000_000,
                        features=frozenset({"chat", "projects", "connectors", "sso"})),
    "enterprise": Entitlements("enterprise", max_seats=10_000, daily_cost_limit_usd=0.0, daily_token_limit=0,
                               features=frozenset({"chat", "projects", "connectors", "sso", "scim", "audit_export"})),
}
# enterprise: 0 budget == unlimited (matches governance's "0 == unlimited" convention).


def get_entitlements(plan: str | None) -> Entitlements:
    return _PLANS.get((plan or "trial").lower(), _PLANS["trial"])


def has_feature(plan: str | None, feature: str) -> bool:
    return feature in get_entitlements(plan).features
