"""W4 — plan tiers & entitlements."""
from core.plans import Entitlements, get_entitlements, has_feature


def test_known_plans_have_expected_entitlements():
    trial = get_entitlements("trial")
    assert isinstance(trial, Entitlements)
    assert trial.max_seats == 3
    pro = get_entitlements("pro")
    assert pro.max_seats == 25 and "sso" in pro.features
    ent = get_entitlements("enterprise")
    assert ent.max_seats >= 1000


def test_unknown_plan_falls_back_to_trial():
    assert get_entitlements("bogus").plan == "trial"
    assert get_entitlements(None).plan == "trial"


def test_has_feature():
    assert has_feature("pro", "sso") is True
    assert has_feature("trial", "sso") is False
