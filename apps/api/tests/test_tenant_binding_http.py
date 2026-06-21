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
