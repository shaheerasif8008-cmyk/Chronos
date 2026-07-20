from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_cookie_authenticated_mutation_rejects_untrusted_origin():
    import main

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://api.localhost",
        cookies={"chronos_session": "not-a-real-session"},
    ) as client:
        response = await client.post(
            "/auth/logout",
            headers={"Origin": "https://attacker.example"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "CSRF validation failed"}


@pytest.mark.asyncio
async def test_cookie_authenticated_mutation_accepts_configured_app_origin():
    import main

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://api.localhost",
        cookies={"chronos_session": "not-a-real-session"},
    ) as client:
        response = await client.post(
            "/auth/logout",
            headers={"Origin": "http://localhost:3000"},
        )

    # The request crossed the CSRF boundary and then failed normal JWT auth.
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bearer token"


@pytest.mark.asyncio
async def test_bearer_client_is_not_subject_to_browser_cookie_csrf_check():
    import main

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://api.localhost",
        cookies={"chronos_session": "not-a-real-session"},
    ) as client:
        response = await client.post(
            "/auth/logout",
            headers={
                "Authorization": "Bearer also-not-a-real-session",
                "Origin": "https://attacker.example",
            },
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bearer token"


def test_production_session_cookie_is_secure_lax_and_host_only(monkeypatch):
    from fastapi import Response

    from core import auth

    monkeypatch.setattr(auth.settings, "environment", "production")
    monkeypatch.setattr(auth.settings, "base_domain", "cognisiatech.com")
    response = Response()
    auth.set_session_cookie(response, "signed-session")

    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Domain=" not in cookie
