"""Direct per-provider OAuth2 connect flow (claude.ai-style connector directory).

The flow: build a signed authorize URL → user signs in at the provider →
provider redirects to our callback with code+state → we verify state, exchange
the code for tokens, store them in the vault, and activate a connectors row.

httpx, Redis, and the DB/vault are mocked at the function boundary (the
established pattern in this suite) — no live providers or infra required.
"""
import time

import pytest


# ── Signed state (CSRF + binds the public callback back to a member) ────────────

def test_sign_and_verify_state_roundtrip():
    from connectors import oauth

    state = oauth.sign_state({"member_id": "m1", "org_id": "default", "provider": "google", "ts": int(time.time())})
    payload = oauth.verify_state(state)
    assert payload["member_id"] == "m1"
    assert payload["provider"] == "google"


def test_verify_state_rejects_tampering():
    from connectors import oauth

    state = oauth.sign_state({"member_id": "m1", "provider": "google", "ts": int(time.time())})
    body, sig = state.split(".", 1)
    forged = body[:-2] + ("aa" if not body.endswith("aa") else "bb") + "." + sig
    with pytest.raises(ValueError):
        oauth.verify_state(forged)


def test_verify_state_rejects_expired():
    from connectors import oauth

    stale = oauth.sign_state({"member_id": "m1", "provider": "google", "ts": int(time.time()) - 10_000})
    with pytest.raises(ValueError):
        oauth.verify_state(stale, max_age_seconds=600)


# ── Authorize URL ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_authorize_url_includes_required_params(monkeypatch):
    from connectors import oauth

    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "google-client-123")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "secret")

    stored: dict = {}

    class FakeRedis:
        async def set(self, key, value, ex=None):
            stored[key] = (value, ex)

    monkeypatch.setattr(oauth, "redis_client", FakeRedis())

    url = await oauth.build_authorize_url("google", member_id="m1", org_id="default")

    assert url.startswith(oauth.PROVIDER_CATALOG["google"].authorize_url)
    assert "client_id=google-client-123" in url
    assert "response_type=code" in url
    assert "redirect_uri=" in url
    assert "state=" in url
    # PKCE: a code_challenge is sent and the verifier is held server-side in Redis.
    assert "code_challenge=" in url and "code_challenge_method=S256" in url
    assert any(k.startswith("oauth:pkce:") for k in stored)


@pytest.mark.asyncio
async def test_build_authorize_url_unconfigured_provider_raises(monkeypatch):
    from connectors import oauth

    monkeypatch.delenv("OAUTH_GOOGLE_CLIENT_ID", raising=False)
    with pytest.raises(ValueError):
        await oauth.build_authorize_url("google", member_id="m1", org_id="default")


# ── Code exchange ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_exchange_code_posts_and_returns_tokens(monkeypatch):
    from connectors import oauth

    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "csecret")

    posted: dict = {}

    async def fake_post(url, data):
        posted["url"] = url
        posted["data"] = data
        return {"access_token": "AT", "refresh_token": "RT", "scope": "a b", "token_type": "Bearer"}

    class FakeRedis:
        async def get(self, key):
            return "the-verifier"

        async def delete(self, key):
            return None

    monkeypatch.setattr(oauth, "_post_token", fake_post)
    monkeypatch.setattr(oauth, "redis_client", FakeRedis())

    state = oauth.sign_state({"member_id": "m1", "org_id": "default", "provider": "google", "ts": int(time.time())})
    tokens = await oauth.exchange_code("google", code="the-code", state=state)

    assert tokens["access_token"] == "AT"
    assert posted["url"] == oauth.PROVIDER_CATALOG["google"].token_url
    assert posted["data"]["code"] == "the-code"
    assert posted["data"]["grant_type"] == "authorization_code"
    assert posted["data"]["code_verifier"] == "the-verifier"  # PKCE verifier replayed


@pytest.mark.asyncio
async def test_exchange_code_rejects_bad_state(monkeypatch):
    from connectors import oauth

    with pytest.raises(ValueError):
        await oauth.exchange_code("google", code="x", state="tampered.sig")


# ── Completing the connection: vault + connectors row ───────────────────────────

@pytest.mark.asyncio
async def test_complete_connection_stores_token_and_activates_connector(monkeypatch):
    from connectors import oauth

    async def fake_exchange(provider, code, state):
        return {
            "access_token": "AT",
            "refresh_token": "RT",
            "scope": "https://www.googleapis.com/auth/gmail.send",
            "__member_id": "m1",
            "__org_id": "default",
        }

    stored_creds: dict = {}

    async def fake_vault_store(connector_id, credentials, org_id="default"):
        stored_creds["credentials"] = credentials
        stored_creds["org_id"] = org_id
        return "vlt_abc"

    inserted: dict = {}

    async def fake_insert_connector(values):
        inserted.update(values)
        return "conn-1"

    monkeypatch.setattr(oauth, "exchange_code", fake_exchange)
    monkeypatch.setattr(oauth.vault, "store", fake_vault_store)
    monkeypatch.setattr(oauth, "_insert_connector_row", fake_insert_connector)

    result = await oauth.complete_connection("google", code="c", state="s")

    # Token is vaulted (never returned/logged); the row references only vault_ref.
    assert stored_creds["credentials"]["access_token"] == "AT"
    assert inserted["provider"] == "google"
    assert inserted["vault_ref"] == "vlt_abc"
    assert inserted["status"] == "active"
    assert inserted["organization_id"] == "default"
    assert result["provider"] == "google"
    assert "access_token" not in result  # secrets never leave the vault boundary


# ── Directory listing ───────────────────────────────────────────────────────────

def test_list_providers_reports_configured_flag(monkeypatch):
    from connectors import oauth

    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("OAUTH_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("OAUTH_NOTION_CLIENT_ID", raising=False)

    providers = {p["provider"]: p for p in oauth.list_providers()}
    assert providers["google"]["configured"] is True
    assert providers["notion"]["configured"] is False
    # Catalog entries expose what the directory UI needs.
    assert {"name", "description", "provider", "configured"} <= set(providers["google"].keys())
