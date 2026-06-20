"""Unit tests for enterprise SSO (OIDC) + SCIM 2.0 logic.

Covers the pure, DB-independent surface: SSO state signing, claim extraction and
login-URL building; SCIM token hashing, resource mapping, filter parsing, and the
group→role precedence. DB-backed CRUD is exercised by the API in CI.
"""
from __future__ import annotations

import asyncio

import pytest

from core import scim, sso


# ── SSO ───────────────────────────────────────────────────────────────────────

def test_state_roundtrip_and_tamper():
    token = sso.sign_state("conn-1", "/chat", "nonce-abc")
    claims = sso.verify_state(token)
    assert claims["cid"] == "conn-1"
    assert claims["redirect"] == "/chat"
    with pytest.raises(sso.SSOError):
        sso.verify_state(token + "tamper")


def test_email_from_claims():
    assert sso.email_from_claims({"email": "Alice@Acme.com"}) == "alice@acme.com"
    assert sso.email_from_claims({"preferred_username": "bob@acme.com"}) == "bob@acme.com"
    with pytest.raises(sso.SSOError):
        sso.email_from_claims({"sub": "no-email"})


def test_build_login_url_uses_configured_endpoints():
    conn = sso.SSOConnection(
        id="c1", organization_id="default", issuer="https://idp.example.com",
        client_id="client-123", client_secret="s",
        authorize_url="https://idp.example.com/authorize",
        token_url="https://idp.example.com/token",
        jwks_url="https://idp.example.com/jwks",
        userinfo_url="", scopes="openid email", email_domain="acme.com",
        default_role="user", enabled=True,
    )
    url = asyncio.get_event_loop().run_until_complete(
        sso.build_login_url(conn, redirect="/chat", nonce="n1")
    )
    assert url.startswith("https://idp.example.com/authorize?")
    assert "client_id=client-123" in url
    assert "response_type=code" in url
    assert "state=" in url and "nonce=n1" in url


# ── SCIM token auth ───────────────────────────────────────────────────────────

def test_token_hash_and_format():
    raw = scim.generate_token()
    assert raw.startswith("scim_")
    h = scim.hash_token(raw)
    assert h == scim.hash_token(raw) and len(h) == 64
    assert scim.hash_token("a") != scim.hash_token("b")


# ── SCIM User mapping ─────────────────────────────────────────────────────────

def test_member_to_scim_active_and_inactive():
    base = "https://api.example.com/scim/v2"
    active = scim.member_to_scim(
        {"id": "m1", "email": "a@x.com", "name": "Alice", "external_id": "ext-1", "status": "active"},
        base_url=base,
    )
    assert active["schemas"] == [scim.USER_SCHEMA]
    assert active["userName"] == "a@x.com"
    assert active["active"] is True
    assert active["externalId"] == "ext-1"
    assert active["emails"][0]["value"] == "a@x.com"
    assert active["meta"]["location"] == f"{base}/Users/m1"

    inactive = scim.member_to_scim({"id": "m2", "email": "b@x.com", "status": "deactivated"}, base_url=base)
    assert inactive["active"] is False


def test_extract_email_and_name():
    assert scim._extract_email({"userName": "U@X.com"}) == "u@x.com"
    assert scim._extract_email({"emails": [{"value": "c@x.com", "primary": True}]}) == "c@x.com"
    assert scim._extract_email({"foo": "bar"}) is None
    assert scim._extract_name({"displayName": "Jane Doe"}) == "Jane Doe"
    assert scim._extract_name({"name": {"givenName": "Jane", "familyName": "Doe"}}) == "Jane Doe"
    assert scim._extract_name({"name": {"formatted": "J. Doe"}}) == "J. Doe"


def test_parse_filter():
    assert scim._parse_filter('userName eq "a@x.com"') == ("userName", "a@x.com")
    assert scim._parse_filter('externalId eq "ext-9"') == ("externalId", "ext-9")
    assert scim._parse_filter(None) == (None, None)
    assert scim._parse_filter("displayName co Smith") == (None, None)


# ── SCIM Group mapping + role precedence ──────────────────────────────────────

def test_group_to_scim():
    g = scim.group_to_scim(
        {"id": "g1", "display_name": "Admins", "external_id": "okta-1"},
        [{"member_id": "m1", "email": "a@x.com"}],
        base_url="https://api/scim/v2",
    )
    assert g["schemas"] == [scim.GROUP_SCHEMA]
    assert g["displayName"] == "Admins"
    assert g["members"][0]["value"] == "m1"


def test_role_precedence():
    assert scim._max_role(["user", "admin", "viewer"]) == "admin"
    assert scim._max_role(["user", "manager"]) == "manager"
    assert scim._max_role(["user"]) == "user"
    assert scim._max_role([]) == "user"
    assert scim._rank("owner") > scim._rank("admin") > scim._rank("user")
