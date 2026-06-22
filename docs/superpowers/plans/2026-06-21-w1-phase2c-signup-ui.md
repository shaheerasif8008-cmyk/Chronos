# W1 Phase 2C — Signup/Onboarding UI + Cross-Subdomain Cookie Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person can self-serve sign up through the real web UI — verify a work email, name their org — and land logged-in on their org's subdomain. Completes the "demoable end-to-end" bar.

**Architecture:** Backend sets the session cookie on the **parent domain** (`.{base_domain}`) in production so an apex-signup cookie is valid on the tenant subdomain after redirect (no separate handoff token — consistent with the existing `SameSite=None` cross-host cookie). A new `/signup` Next.js page mirrors the login page: email → request-OTP → code + org name → `POST /auth/signup` → redirect to the org's subdomain (prod) or `/chat` (dev single-host). Backend signup response already returns `org_id`; we add the `subdomain` so the client knows where to redirect.

**Tech Stack:** FastAPI (cookie), Next.js 14 App Router + TypeScript, pytest, `npm run build`/typecheck for web verification.

**Spec:** `docs/superpowers/specs/2026-06-20-w1-tenant-onboarding-identity-design.md` (§2d onboarding, §2e — note the parent-domain-cookie variant chosen over a handoff token; §5 infra).

**Design note (handoff vs parent-cookie):** the spec §2e described a short-lived handoff token exchanged on the subdomain. We instead scope the session cookie to the parent domain so it's shared across subdomains — simpler and consistent with `main`'s current `SameSite=None` cross-host cookie. Trade-off: host-only cookies + a handoff token would isolate sessions per subdomain more strictly; recorded as a future hardening, not needed for the functional slice.

**Environment** (backend tests): export the standard block; `python3.11`. Web: `cd apps/web && npm install` (if needed) then `npm run build`.

---

## File Structure
- `apps/api/core/auth.py` — **Modify.** `set_session_cookie` scopes to the parent domain in production.
- `apps/api/core/config.py` — already has `base_domain`.
- `apps/api/routers/auth.py` — **Modify.** `signup` response includes `subdomain`.
- `apps/api/tests/test_signup.py` — **Modify.** Assert signup returns `subdomain`.
- `apps/web/app/signup/page.tsx` — **Create.** Signup UI.
- `apps/web/app/login/page.tsx` — **Modify (small).** Add a "Create an organization" link to `/signup`.

---

## Task 1: Parent-domain session cookie in production

**Files:** Modify `apps/api/core/auth.py`; Test `apps/api/tests/test_auth_cookie.py` (new)

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_auth_cookie.py`:
```python
"""W1 Phase 2C — session cookie is scoped to the parent domain in production so an
apex-signup cookie is valid on the tenant subdomain."""
from __future__ import annotations

import pytest
from starlette.responses import Response

from core import auth as auth_mod


def test_cookie_scoped_to_parent_domain_in_production(monkeypatch):
    monkeypatch.setattr("core.auth._is_production", lambda: True)
    monkeypatch.setattr("core.auth.settings.base_domain", "cognisiatech.com", raising=False)
    resp = Response()
    auth_mod.set_session_cookie(resp, "tok")
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Domain=.cognisiatech.com" in set_cookie


def test_cookie_host_only_in_non_production(monkeypatch):
    monkeypatch.setattr("core.auth._is_production", lambda: False)
    resp = Response()
    auth_mod.set_session_cookie(resp, "tok")
    set_cookie = resp.headers.get("set-cookie", "")
    assert "Domain=" not in set_cookie  # host-only in dev
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_auth_cookie.py -v`
Expected: the production test FAILS (no `Domain=` is set today).

- [ ] **Step 3: Implement**

In `apps/api/core/auth.py`, update `set_session_cookie` to pass `domain` in production. The current function calls `response.set_cookie("chronos_session", token, httponly=True, samesite=..., secure=..., max_age=...)`. Add a `domain` kwarg:
```python
def set_session_cookie(response, token: str) -> None:
    """Set the session JWT as an httpOnly cookie. In production it is scoped to the
    parent domain (.<base_domain>) so an apex-signup cookie is valid on the tenant
    subdomain; in dev it is host-only."""
    domain = f".{settings.base_domain}" if _is_production() else None
    response.set_cookie(
        "chronos_session",
        token,
        domain=domain,
        httponly=True,
        samesite="none" if settings.is_production else "lax",
        secure=settings.is_production,
        max_age=settings.access_token_expire_minutes * 60,
    )
```
(`_is_production()` already exists from Phase 2B-1. Keep the existing `samesite`/`secure` logic unchanged.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_auth_cookie.py -v`
Expected: PASS (both). Also run the existing auth tests: `python3.11 -m pytest -q -k "otp or signup or cognito or tenant_binding" 2>&1 | tail -3` → all pass.

- [ ] **Step 5: Commit**
```bash
git add apps/api/core/auth.py apps/api/tests/test_auth_cookie.py
git commit -m "feat(w1-2c): scope session cookie to parent domain in production"
```

---

## Task 2: Signup response includes the org subdomain

**Files:** Modify `apps/api/routers/auth.py`, `apps/api/tests/test_signup.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_signup.py`:
```python
@pytest.mark.asyncio
async def test_signup_response_includes_subdomain():
    domain = f"sub{uuid.uuid4().hex[:8]}.com"
    email = f"founder@{domain}"
    _seed_otp(email)
    async with _client() as client:
        resp = await client.post("/auth/signup", json={"email": email, "code": "123456"})
    assert resp.status_code == 200
    body = resp.json()
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        sub = (await conn.execute(orgs.select().where(orgs.c.id == body["org_id"]))).mappings().one()["subdomain"]
    assert body["subdomain"] == sub
```
(`_seed_otp`, `_client`, `reflect_table`, `engine` are already in the test file.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py::test_signup_response_includes_subdomain -v`
Expected: FAIL — `KeyError: 'subdomain'`.

- [ ] **Step 3: Implement**

In `apps/api/routers/auth.py` `signup`, after `signup_or_join`, look up the org's subdomain and include it in the response. The success-path return currently is `{"access_token":..., "token_type":..., "member_id":..., "org_id":..., "created":...}`. Add the subdomain — fetch it from the org:
```python
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        subdomain = (await conn.execute(
            select(organizations.c.subdomain).where(organizations.c.id == result["org_id"])
        )).scalar_one()
    return {"access_token": token, "token_type": "bearer", "member_id": result["member_id"],
            "org_id": result["org_id"], "subdomain": subdomain, "created": result["created"]}
```
(`reflect_table`, `engine`, `select` are already imported in auth.py.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_signup.py -v`
Expected: PASS (new test + all existing signup tests; the existing endpoint tests don't assert the absence of `subdomain`, so they're unaffected).

- [ ] **Step 5: Commit**
```bash
git add apps/api/routers/auth.py apps/api/tests/test_signup.py
git commit -m "feat(w1-2c): signup response returns the org subdomain for redirect"
```

---

## Task 3: Signup page (Next.js)

**Files:** Create `apps/web/app/signup/page.tsx`; Modify `apps/web/app/login/page.tsx`

- [ ] **Step 1: Create the signup page**

Create `apps/web/app/signup/page.tsx` mirroring the login page's OTP flow but calling `/auth/signup` and collecting an org name. Use the same `apiBase()` helper pattern as `login/page.tsx`:
```tsx
"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;
function apiBase() {
  if (CONFIGURED_API_BASE) return CONFIGURED_API_BASE;
  if (typeof window !== "undefined") {
    const webPort = Number(window.location.port || "3000");
    if (Number.isFinite(webPort) && webPort >= 3000 && webPort < 3100) {
      return `http://${window.location.hostname}:${8000 + (webPort - 3000)}`;
    }
  }
  return "http://localhost:8000";
}

export default function SignupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [orgName, setOrgName] = useState("");
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function requestOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/auth/request-otp`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
        credentials: "include",
      });
      if (!res.ok) throw new Error("Could not send a verification code. Is signup enabled?");
      setRequested(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function submitSignup(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, code, org_name: orgName || undefined }),
        credentials: "include",
      });
      if (!res.ok) throw new Error(await res.text());
      const body = await res.json();
      // Prod: redirect to the org's subdomain. Dev single-host: go straight to the app.
      if (typeof window !== "undefined") {
        const host = window.location.host;
        const isLocal = host.includes("localhost") || host.startsWith("127.");
        if (!isLocal && body.subdomain) {
          const baseDomain = host.split(".").slice(-2).join(".");
          window.location.href = `${window.location.protocol}//${body.subdomain}.${baseDomain}/chat`;
          return;
        }
      }
      router.push("/chat");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Signup failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 380, margin: "10vh auto", display: "flex", flexDirection: "column", gap: 12 }}>
      <h1>Create your organization</h1>
      {error && <p role="alert" style={{ color: "crimson" }}>{error}</p>}
      {!requested ? (
        <form onSubmit={requestOtp} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <input type="email" required placeholder="Work email" value={email}
                 onChange={(e) => setEmail(e.target.value)} />
          <button type="submit" disabled={busy}>Send verification code</button>
        </form>
      ) : (
        <form onSubmit={submitSignup} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <input required placeholder="Verification code" value={code}
                 onChange={(e) => setCode(e.target.value)} />
          <input placeholder="Organization name (optional)" value={orgName}
                 onChange={(e) => setOrgName(e.target.value)} />
          <button type="submit" disabled={busy}>Create organization</button>
        </form>
      )}
      <a href="/login">Already have an account? Sign in</a>
    </div>
  );
}
```

- [ ] **Step 2: Add a link from the login page**

In `apps/web/app/login/page.tsx`, add a link to `/signup` near the form (e.g. after the existing form/error UI). Find a sensible spot in the returned JSX and add:
```tsx
      <a href="/signup">Create an organization</a>
```
Keep it minimal; match the surrounding style. Don't restructure the login page.

- [ ] **Step 3: Verify the web build**

Run: `cd apps/web && npm run build 2>&1 | tail -20`
Expected: build succeeds, `/signup` is compiled as a route, no type errors. (If `npm install` is needed first, run it.)

- [ ] **Step 4: Commit**
```bash
git add apps/web/app/signup/page.tsx apps/web/app/login/page.tsx
git commit -m "feat(w1-2c): self-serve signup page with subdomain redirect"
```

---

## Task 4: Verification gate

- [ ] **Step 1:** Backend fresh-DB suite (standard env + throwaway DB) → only the 14 known `test_doc_authoring` failures; zero new.
- [ ] **Step 2:** `cd apps/web && npm run build` → succeeds with the new `/signup` route.

---

## Self-Review

**Spec coverage:** signup UI (§2d) → Task 3; cross-subdomain session continuity (§2e, parent-cookie variant) → Task 1; redirect target → Task 2 (subdomain in response) + Task 3 (redirect). Onboarding wizard depth (branding, invite teammates) is intentionally minimal here — the functional signup + landing is the bar; richer onboarding can be a follow-up.

**Placeholder scan:** none — full code for the page and the backend changes.

**Type consistency:** `set_session_cookie(response, token)` signature unchanged (internal `domain` derivation). Signup response gains `subdomain` (string); the page reads `body.subdomain`. `_is_production()` reused from 2B-1. `apiBase()` mirrors the existing login page helper.

**Handoff trade-off** is documented (parent-domain cookie vs host-only + token) so a reviewer sees it was a deliberate choice, not an omission.
