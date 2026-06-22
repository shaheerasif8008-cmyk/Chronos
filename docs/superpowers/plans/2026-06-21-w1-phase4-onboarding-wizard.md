# W1 Phase 4 — Onboarding Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After self-serve signup, a new org owner lands in a first-run wizard — welcome, invite teammates, finish — that marks onboarding complete and drops them into the app. Returning users skip it.

**Architecture:** Wire the existing `organizations.onboarding_state` column (added in Phase 1's migration 0039) with two admin endpoints: `GET /settings/onboarding` (current state) and `POST /settings/onboarding/complete` (set `complete`). The wizard reuses the existing `POST /settings/invitations` for invites. The `/signup` page redirects a *newly created* org (`created: true` in the signup response) to `/onboarding` instead of `/chat`.

**Tech Stack:** FastAPI, SQLAlchemy Core, Next.js 14 + TypeScript, pytest, `npm run build`.

**Spec:** `docs/superpowers/specs/2026-06-20-w1-tenant-onboarding-identity-design.md` (§2d first-run onboarding).

**Scope note:** branding/logo upload is intentionally out of scope (the settings page already edits org branding; the org name is set at signup). The wizard's value here is a guided invite-teammates first run + the completion flag. Richer branding-in-wizard is a follow-up.

**Environment** (backend): export the standard block; `python3.11`. Web: `cd apps/web && npm run build`.

---

## File Structure
- `apps/api/routers/settings.py` — **Modify.** Add `GET /settings/onboarding` + `POST /settings/onboarding/complete`.
- `apps/api/tests/test_onboarding.py` — **Create.**
- `apps/web/app/onboarding/page.tsx` — **Create.** The wizard.
- `apps/web/app/signup/page.tsx` — **Modify.** New-org signups go to `/onboarding`.

---

## Task 1: Onboarding state endpoints

**Files:** Modify `apps/api/routers/settings.py`; Test `apps/api/tests/test_onboarding.py`

Read `apps/api/routers/settings.py` first for: the router prefix, `require_admin(member)` / `ADMIN_ROLES`, and the imports (`reflect_table`, `engine`, `select`, `update`, `audit`, `get_current_member`, `Member`). Match them.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_onboarding.py`:
```python
"""W1 Phase 4 — onboarding state endpoints."""
from __future__ import annotations

import uuid
import httpx
import pytest

import main
from core.auth import create_access_token
from core.db import engine, reflect_table


def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


async def _org_and_admin(state: str = "new"):
    org_id = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(
            id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T", onboarding_state=state))
        await conn.execute(members.insert().values(
            id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="admin"))
    return org_id, mid, create_access_token(mid, org_id=org_id), f"o{org_id[:8]}"


@pytest.mark.asyncio
async def test_get_onboarding_state():
    org_id, _, token, sub = await _org_and_admin(state="new")
    async with _client() as client:
        resp = await client.get("/settings/onboarding",
                                headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": sub})
    assert resp.status_code == 200
    assert resp.json()["state"] == "new"


@pytest.mark.asyncio
async def test_complete_onboarding_sets_state_and_persists():
    org_id, _, token, sub = await _org_and_admin(state="new")
    headers = {"Authorization": f"Bearer {token}", "X-Chronos-Org": sub}
    async with _client() as client:
        resp = await client.post("/settings/onboarding/complete", headers=headers)
        assert resp.status_code == 200 and resp.json()["state"] == "complete"
        again = await client.get("/settings/onboarding", headers=headers)
    assert again.json()["state"] == "complete"
    orgs = await reflect_table("organizations")
    async with engine.begin() as conn:
        row = (await conn.execute(orgs.select().where(orgs.c.id == org_id))).mappings().one()
    assert row["onboarding_state"] == "complete"


@pytest.mark.asyncio
async def test_complete_onboarding_rejects_non_admin():
    org_id = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    orgs = await reflect_table("organizations")
    members = await reflect_table("members")
    async with engine.begin() as conn:
        await conn.execute(orgs.insert().values(id=org_id, slug=f"o{org_id[:8]}", subdomain=f"o{org_id[:8]}", name="T"))
        await conn.execute(members.insert().values(id=mid, organization_id=org_id, email=f"{mid[:8]}@t.io", role="user"))
    token = create_access_token(mid, org_id=org_id)
    async with _client() as client:
        resp = await client.post("/settings/onboarding/complete",
                                 headers={"Authorization": f"Bearer {token}", "X-Chronos-Org": f"o{org_id[:8]}"})
    assert resp.status_code == 403
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd apps/api && python3.11 -m pytest tests/test_onboarding.py -v`
Expected: FAIL (404 — routes missing).

- [ ] **Step 3: Implement the endpoints**

In `apps/api/routers/settings.py`, add (matching the file's existing helpers/imports — `require_admin`, `reflect_table`, `engine`, `select`, `update`, `audit`, `get_current_member`, `Member`):
```python
@router.get("/onboarding")
async def get_onboarding(member: Member = Depends(get_current_member)) -> dict:
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        state = (await conn.execute(
            select(organizations.c.onboarding_state).where(organizations.c.id == member.organization_id)
        )).scalar_one_or_none()
    return {"state": state or "new"}


@router.post("/onboarding/complete")
async def complete_onboarding(member: Member = Depends(get_current_member)) -> dict:
    require_admin(member)
    organizations = await reflect_table("organizations")
    async with engine.begin() as conn:
        await conn.execute(update(organizations).where(
            organizations.c.id == member.organization_id).values(onboarding_state="complete"))
    await audit.log("onboarding_completed", member.id, "settings.onboarding_complete",
                    organization_id=member.organization_id, resource_type="organization",
                    resource_id=member.organization_id)
    return {"state": "complete"}
```
(If `require_admin`/`Depends`/`audit`/`update`/`select` aren't already imported in settings.py, they are used by the existing invitation/members endpoints — reuse the same imports. Confirm the `@router` prefix is `/settings` so the paths resolve to `/settings/onboarding`.)

- [ ] **Step 4: Run to verify it passes**

Run: `cd apps/api && python3.11 -m pytest tests/test_onboarding.py -v`
Expected: PASS (get state, complete + persist, non-admin 403).

- [ ] **Step 5: Commit**
```bash
git add apps/api/routers/settings.py apps/api/tests/test_onboarding.py
git commit -m "feat(w1-4): onboarding state endpoints (get + complete)"
```

---

## Task 2: Onboarding wizard page

**Files:** Create `apps/web/app/onboarding/page.tsx`

- [ ] **Step 1: Create the wizard**

Create `apps/web/app/onboarding/page.tsx`. It uses the same `apiBase()` helper as the other pages and credentialed fetches. Steps: welcome → invite teammates (calls `POST /settings/invitations`) → finish (calls `POST /settings/onboarding/complete`, then routes to `/chat`).
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

export default function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState<1 | 2>(1);
  const [invite, setInvite] = useState("");
  const [invited, setInvited] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function sendInvite(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/settings/invitations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: invite, role: "user" }),
        credentials: "include",
      });
      if (!res.ok) throw new Error(await res.text());
      setInvited((prev) => [...prev, invite]);
      setInvite("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invite failed");
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setBusy(true);
    try {
      await fetch(`${apiBase()}/settings/onboarding/complete`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Non-fatal: proceed into the app regardless.
    } finally {
      router.push("/chat");
    }
  }

  return (
    <div style={{ maxWidth: 440, margin: "10vh auto", display: "flex", flexDirection: "column", gap: 16 }}>
      {step === 1 ? (
        <>
          <h1>Welcome to Chronos</h1>
          <p>Your organization is ready. Let&apos;s invite your team.</p>
          <button onClick={() => setStep(2)}>Invite teammates</button>
          <button onClick={finish} disabled={busy} style={{ background: "transparent" }}>
            Skip for now
          </button>
        </>
      ) : (
        <>
          <h1>Invite your team</h1>
          {error && <p role="alert" style={{ color: "crimson" }}>{error}</p>}
          <form onSubmit={sendInvite} style={{ display: "flex", gap: 8 }}>
            <input
              type="email"
              required
              placeholder="teammate@yourcompany.com"
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
              style={{ flex: 1 }}
            />
            <button type="submit" disabled={busy}>Send</button>
          </form>
          {invited.length > 0 && (
            <ul>{invited.map((e) => <li key={e}>Invited {e}</li>)}</ul>
          )}
          <button onClick={finish} disabled={busy}>Finish setup</button>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 2: Send new-org signups into the wizard**

In `apps/web/app/signup/page.tsx`, the `submitSignup` success path currently does `router.push("/chat")` (and the prod subdomain redirect goes to `/chat`). Change the *local/dev* path so a newly created org lands in onboarding: when `body.created` is true and we're not doing the subdomain redirect, `router.push("/onboarding")` instead of `/chat`. For the prod subdomain redirect, point it at `/onboarding` when `body.created`:
```tsx
      const dest = body.created ? "/onboarding" : "/chat";
      if (typeof window !== "undefined") {
        const host = window.location.host;
        const isLocal = host.includes("localhost") || host.startsWith("127.");
        if (!isLocal && body.subdomain) {
          const baseDomain = host.split(".").slice(-2).join(".");
          window.location.href = `${window.location.protocol}//${body.subdomain}.${baseDomain}${dest}`;
          return;
        }
      }
      router.push(dest);
```

- [ ] **Step 3: Build**

Run: `cd apps/web && npm run build 2>&1 | tail -20`
Expected: build succeeds; `/onboarding` is compiled as a route; no type errors.

- [ ] **Step 4: Commit**
```bash
git add apps/web/app/onboarding/page.tsx apps/web/app/signup/page.tsx
git commit -m "feat(w1-4): first-run onboarding wizard (invite teammates) + signup redirect"
```

---

## Task 3: Verification gate

- [ ] **Step 1:** Backend fresh-DB suite (standard env + throwaway DB) → only the 14 known `test_doc_authoring` failures; zero new.
- [ ] **Step 2:** `cd apps/web && npm run build` → succeeds with `/onboarding`.

---

## Self-Review

**Spec coverage:** first-run onboarding (§2d) → Tasks 1-2 (state endpoints + wizard + invite reuse + completion flag). Branding-in-wizard intentionally deferred (settings page already does it; org name set at signup).

**Placeholder scan:** none — full code for endpoints and the page.

**Type consistency:** `GET /settings/onboarding` → `{state}`; `POST /settings/onboarding/complete` → `{state:"complete"}`; both read by the wizard. `POST /settings/invitations` (existing) takes `{email, role}`. `onboarding_state` column (organizations, migration 0039) is the single source of truth. Signup response `created` (bool) drives the redirect target.
