# Chronos — Enterprise SSO (OIDC) + SCIM 2.0

Status: **implemented**
Branch: `claude/enterprise-sso-scim`

Closes the top enterprise-procurement blocker from the maturity review: customers
can now log in with their own identity provider and provision/deprovision users
automatically.

---

## What's included

### SSO — generic OIDC (`core/sso.py`, `routers/sso.py`)
Per-org OpenID Connect against any standard IdP — **Okta, Microsoft Entra ID,
Google Workspace, Auth0, Ping, OneLogin** (all support OIDC).

- **Authorization Code flow**: `/auth/sso/start?email=` resolves the IdP by email
  domain and returns the IdP login URL; the IdP redirects to
  `/auth/sso/callback`, which exchanges the code, validates the `id_token`
  against the IdP's JWKS (RS256/ES256, issuer + audience checked), JIT-provisions
  the member in the connection's org, issues a Chronos session, and bounces to the
  web app (token in the URL fragment, never logged).
- **Discovery**: endpoint URLs auto-fill from the issuer's
  `/.well-known/openid-configuration` when left blank.
- **Stateless state**: a short-lived signed JWT (connection id + redirect + nonce)
  — no server-side session store.
- **Admin management** (`/auth/sso/connections`, admin-gated + audited): CRUD of
  per-org connections; client secret is never returned by the API.
- **Login UI**: "Continue with SSO" on the login page; the callback page consumes
  the `#access_token` fragment.

### SCIM 2.0 — provisioning (`core/scim.py`, `routers/scim.py`)
RFC 7643/7644, authenticated by a per-org bearer token (only its SHA-256 hash is
stored). `application/scim+json` responses with ListResponse/Error envelopes.

- **Users**: GET (list + `userName eq`/`externalId eq` filter + pagination),
  GET/{id}, POST (409 on duplicate), PUT, PATCH (handles the common
  `active` toggle from Okta/Entra), DELETE → **soft deactivate**.
- **Groups**: GET, GET/{id}, POST, PATCH (add/remove/replace members), DELETE.
- **Discovery**: ServiceProviderConfig, ResourceTypes, Schemas.
- **Lifecycle → access**: `active:false` (or DELETE) sets member status
  `deactivated`; `get_current_member` then refuses that member's existing tokens
  immediately. Soft deactivation preserves audit history and ownership.
- **Groups → roles**: each group maps to a Chronos role; a member's effective role
  is the **highest** role among their groups (recomputed on every membership
  change). "Add user to the Admins group" makes them an admin.
- **Token management** (`/scim/tokens`, admin-gated + audited): create (raw token
  shown once), list, revoke.

### Schema (`migration 0038_sso_scim`)
- `members`: + `external_id`, `status`, `sso_subject` (+ partial unique index on
  `(org, external_id)`).
- `sso_connections`, `scim_tokens`, `scim_groups`, `group_memberships` — all
  tenant-scoped (`organization_id` + `region`).

### Governance
SSO/SCIM admin actions (`manage_sso`, `manage_scim`) route through the permission
seam's always-on admin gate, so only org admins can manage them — and every change
is audited.

---

## Tests
`tests/test_sso_scim.py` — SSO state signing + tamper, claim extraction, login-URL
building; SCIM token hashing, User/Group resource mapping, filter parsing, and the
group→role precedence. DB-backed CRUD runs against Postgres in CI.

---

## Not included (and why)
- **SAML 2.0** — every modern IdP (Okta, Entra ID, Google, Auth0, Ping, OneLogin)
  supports OIDC, which this implements end-to-end. SAML adds a heavy XML-signature
  dependency and is only needed for legacy SAML-only IdPs. The connection model
  has a `protocol` column as the extension point if a customer mandates SAML.
- **SCIM bulk operations** — advertised as unsupported in ServiceProviderConfig
  (optional in the spec; IdPs fall back to per-resource calls).
