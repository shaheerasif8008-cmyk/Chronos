"""W1 Phase 2C — session cookie scoped to the parent domain in production so an
apex-signup cookie is valid on the tenant subdomain."""
from __future__ import annotations

from starlette.responses import Response

from core import auth as auth_mod


def test_cookie_scoped_to_parent_domain_in_production(monkeypatch):
    monkeypatch.setattr("core.auth._is_production", lambda: True)
    monkeypatch.setattr("core.auth.settings.base_domain", "cognisiatech.com", raising=False)
    resp = Response()
    auth_mod.set_session_cookie(resp, "tok")
    assert "Domain=.cognisiatech.com" in resp.headers.get("set-cookie", "")


def test_cookie_host_only_in_non_production(monkeypatch):
    monkeypatch.setattr("core.auth._is_production", lambda: False)
    resp = Response()
    auth_mod.set_session_cookie(resp, "tok")
    assert "Domain=" not in resp.headers.get("set-cookie", "")
