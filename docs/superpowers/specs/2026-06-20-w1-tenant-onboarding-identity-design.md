# W1 · Tenant Onboarding & Identity — Design

_Date: 2026-06-20 · Status: approved design, pre-implementation · Author: Claude (Opus 4.8) via brainstorming_

## Context: where this sits

Launch target chosen: **self-serve enterprise GA** (any org can sign up and run
unattended). That bar is too large for one spec, so GA was decomposed into
workstreams (W0–W8). This spec covers **W1 only**; the rest remain a map:

- **W0** Deploy & proof baseline (mostly done — clean migrate + ~621 green tests; the audit's migration + unauthorized-approve blockers are already closed)
- **W1** Tenant onboarding & identity ← _this spec_
- **W2** Access-control depth (OpenFGA relationship/group/scope; groups, retention)
- **W3** Self-serve safety & cost governance (per-tenant rate/cost budgets, secrets isolation, prompt-injection defense, abuse controls)
- **W4** Billing & plan controls
- **W5** Admin & compliance (admin console depth, audit/compliance export, notifications)
- **W6** Collaboration (shared conversations, comments/mentions, global search + command palette)
- **W7** Live integrations (honest-degraded → live)
- **W8** Multi-tenant operational readiness (single-instance runtime decision, observability/SLOs)

Recommended sequence: finish W0 → build the spine (W1 → W2 → W3) → W4 → W5/W6/W7 in parallel → W8 before real load.

### Why W1 first

The identity *machinery* already exists — Cognito/OIDC SSO (`core/sso.py`,
`routers/sso.py`), SCIM 2.0 (`core/scim.py`, `routers/scim.py`, migration 0038),
member invitations (`core/invitations.py`), and an always-on admin permission gate
(`core/permissions.py:check`). What does **not** exist is **tenant genesis**:

- **No self-serve org creation.** Orgs are seeded — `apps/api/seed.py` inserts a
  single `default` org. No path for a new company to create its own org and become
  its first admin.
- **No tenant resolution from the request.** Nothing reads the Host/subdomain to
  bind a request to an org; `org_id` is derived purely from the authenticated
  member (`RequesterContext.from_member`). CLAUDE.md promises
  `novatech.cognisiatech.com` subdomains, but nothing resolves them.

W1 is the spine no other workstream can stand without: you cannot safely admit a
second real org until signup, provisioning, and tenant resolution exist.

## Decisions made during brainstorming

1. **Signup model: open self-serve + domain claiming.** Anyone verifies a work
   email and creates an org. The first signup from a domain claims it and becomes
   owner; later same-domain users auto-join (or request to join). Prevents
   duplicate orgs per company.
2. **Tenant resolution: real subdomains, infra-real.** `novatech.cognisiatech.com`
   resolves the org via Host header in middleware; wildcard DNS + wildcard TLS.
   Matches CLAUDE.md.
3. **Domain-claim trust: two-tier (soft email claim + hard DNS-TXT claim).**
   Recommended and approved (see §4).

## 1. Scope & boundaries

**In scope:** self-serve org signup, domain claiming, tenant provisioning, real
subdomain→org resolution, first-run onboarding.

**Reused, not rebuilt:** Cognito/OIDC SSO, SCIM 2.0, member invitations, the
always-on admin permission gate.

**Out of scope (other workstreams):** deep RBAC/OpenFGA (W2), per-tenant cost/abuse
budgets (W3), billing (W4). W1 stops at "a new company can sign up, get an isolated
branded tenant at its own subdomain, and invite its team."

## 2. Architecture / data flow

### 2a. Tenant resolution (every request)

New ASGI middleware in `apps/api/main.py`:

- Read `Host` header → extract subdomain label → look up `organizations.slug`
  (or `subdomain`) → attach `org_id` to request state.
- `RequesterContext` is built from **(resolved tenant ∩ authenticated member)**: a
  member token is valid only on *their* org's subdomain. A token minted for org A
  presented on org B's subdomain is **rejected (fail-closed)**. This is the hard
  multi-tenant isolation guarantee.
- The marketing/apex host (`app.` / `www.` / apex) resolves to a special
  "no-tenant" context that serves only signup/login.

### 2b. Org genesis (signup)

New `POST /auth/signup`:

1. Verify work email (reuse Cognito email verification / OTP).
2. Look up the email domain in `email_domain_claims`:
   - **Unclaimed work domain:** create org (slug derived from domain,
     collision-suffixed), provision it (§2c), make the signer **owner**, soft-claim
     the domain.
   - **Already claimed:** auto-join the existing org at the lowest role — or
     request-to-join if the owner set `join_policy = approval`.
   - **Free-email domain** (gmail/outlook/… blocklist): create a *personal* org;
     no domain claim.
3. Redirect to the new org's subdomain, logged in (handoff in §2e).

**Reserved slugs.** Slug-from-domain (collision-suffixed) must refuse a
reserved-label blocklist — `app`, `www`, `api`, `admin`, `static`, `assets`, and
the apex itself — or a signup could hijack platform routing.

### 2e. Cross-subdomain auth handoff + dev/CI tenant resolution

Login happens at the apex/no-tenant host, but tokens are bound to a tenant
subdomain (§2a), so the session must cross an origin boundary. Mechanism: after
auth at apex, mint a **short-lived single-use handoff token** (signed JWT: org_id +
member_id + nonce, ~60s TTL) and redirect to
`https://{subdomain}.cognisiatech.com/login/handoff#token=…`; that page exchanges
the handoff token at the tenant origin for the real subdomain-bound session
cookie/token. Reuses the existing fragment-token pattern (`login/callback`); the
handoff token is never logged.

**Dev/CI tenant resolution (required for §6 proof).** There is no wildcard DNS in
local dev or CI, so the middleware (§2a) must also accept tenant resolution via:
(a) `*.lvh.me` / `acme.localhost` Host labels (both resolve to 127.0.0.1), and
(b) an `X-Chronos-Org` header override that is honored **only** when
`settings.is_production` is false. The web base-URL logic in `apps/web/lib/api.ts`
gains a matching dev path so the E2E can drive two subdomains on localhost. Without
this the §6 isolation E2E cannot run, so it is load-bearing, not polish.

### 2f. Domain registry: single source of truth

Two domain→org maps now exist: the new `email_domain_claims` and the existing
`sso_connections.email_domain` (unique-indexed, used by
`core/sso.py:get_connection_by_domain`). **`email_domain_claims` is canonical.**
SSO connection creation must validate that `email_domain` is a domain the org has
**hard-claimed** (DNS-verified, §4) — you cannot route SSO for a domain you don't
own. Login domain routing reads `email_domain_claims` first; `sso_connections`
only answers "which IdP for this already-claimed domain." This keeps the two
registries from diverging.

### 2c. Provisioning (`core/provisioning.py`)

One idempotent function that, for a new org, creates: the org row (with `region`),
the owner member, the `context/{org_id}/org.md` starter folder in object storage, a
default persona, and seeds default skills. This is the per-tenant parameterization
of what `seed.py` does for `default`.

### 2d. Onboarding (first-run)

Owner wizard on the new subdomain: set org name/branding, invite teammates (reuse
invitations), optionally start SSO/SCIM setup (reuse), optionally verify domain via
DNS-TXT to unlock enterprise features.

## 3. Data model (migration `00xx_tenant_genesis`)

- `email_domain_claims` — `domain`, `organization_id`, `claim_type`
  (`soft_email` | `verified_dns`), `join_policy` (`auto` | `approval`),
  `organization_id` + `region` (tenant-scoped per Rules 4/5).
- `domain_verifications` — `domain`, `organization_id`, `txt_token`, `status`
  (DNS-TXT proof of domain ownership).
- `organizations` — already has `slug`; add `subdomain` (defaults to slug),
  `onboarding_state`, `owner_member_id`.
- `members.status` — already exists from SSO/SCIM; reuse for `pending_approval`.

All new tables carry `organization_id UUID NOT NULL` and `region TEXT NOT NULL`
(Rules 4 & 5). Audit on every claim/provision/verify event (append-only).

**Back-compat for `default`.** The existing `default` org (load-bearing in
`seed.py` and across the test suite) is migrated to reserved subdomain
`default.cognisiatech.com` (and resolvable via the dev paths in §2e). Its existing
sessions remain valid: tokens minted before tenant-binding are grandfathered to
`default` so the suite and any live `default` sessions don't break on deploy. New
tokens are subdomain-bound.

## 4. Security / abuse model

"First signup claims the domain" lets an arbitrary/junior/malicious person own a
company's tenant. Two-tier trust closes this:

- **Soft claim (email-verified):** lets you create the org and auto-join same-domain
  users. Enough for frictionless self-serve.
- **Hard claim (DNS-TXT verified):** required to unlock SSO/SCIM, a custom
  subdomain, and to win an ownership dispute. The Slack/Vercel/Google-Workspace
  pattern.
- **Free-email blocklist** → personal orgs only; never claim a shared domain.
- **Auto-join exposure:** new auto-joined members get the lowest role and no access
  to `restricted`-scope memory; owners can flip the org to `join_policy = approval`.
  The tenant-token binding in §2a is the hard isolation guarantee.

## 5. Infra

Real subdomains on Render (current deploy target — `render.yaml` runs `chronos-api`
+ `chronos-web` web services, redis, postgres):

- Add wildcard custom domain `*.cognisiatech.com` to the web service + an `api.`
  (or `*.api.`) host, with Render-provisioned wildcard TLS; wildcard CNAME in DNS.
- Session cookies scoped to `.cognisiatech.com` do not cross-leak because of the
  tenant-token binding in §2a; CORS allows only the matched tenant subdomain.
- The web app derives its API base + current org from `window.location.hostname`,
  replacing the current env-only logic in `apps/web/lib/api.ts` (and the duplicated
  copies in `login/callback/page.tsx`, `chat/page.tsx`, etc.).

## 6. Proof (completion bar)

- Backend tests: signup creates + provisions + claims; second same-domain signup
  auto-joins; free-email → personal org; **cross-tenant token rejected on the wrong
  subdomain** (load-bearing isolation test); DNS-TXT verify unlocks SSO.
- Migration runs clean on a fresh DB (chain guard).
- Behavioral E2E: sign up → land on `acme.<domain>` → invite a teammate → teammate
  accepts → both see the same org, isolated from a second org created in parallel.

## 7. Explicit non-goals

No SAML, no billing, no OpenFGA depth, no per-tenant cost budgets, no custom vanity
domains (only `*.cognisiatech.com`). Those are W2–W4.
