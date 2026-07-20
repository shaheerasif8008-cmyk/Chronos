"""Session cookies stay on the central API host in every environment."""
from __future__ import annotations

from starlette.responses import Response

from core import auth as auth_mod


def test_cookie_is_host_only_secure_and_lax_in_production(monkeypatch):
    monkeypatch.setattr("core.auth.settings.environment", "production", raising=False)
    monkeypatch.setattr("core.auth.settings.base_domain", "cognisiatech.com", raising=False)
    resp = Response()
    auth_mod.set_session_cookie(resp, "tok")
    cookie = resp.headers.get("set-cookie", "")
    assert "Domain=" not in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie


def test_cookie_host_only_in_non_production(monkeypatch):
    monkeypatch.setattr("core.auth.settings.environment", "development", raising=False)
    resp = Response()
    auth_mod.set_session_cookie(resp, "tok")
    assert "Domain=" not in resp.headers.get("set-cookie", "")
