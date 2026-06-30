# Chronos — Launch-Readiness Audit

**Auditor role:** principal engineer / QA lead / security reviewer / launch operator
**Date:** 2026-06-29
**Branch audited:** `claude/chronos-launch-readiness-audit-y8mix6` (tip of `main` @ `8678c63`)
**Scope:** full repository (`apps/api`, `apps/web`, `infra`, `migrations`, `skills`, CI/CD)

> **Method & honesty note.** This is a static + CI-evidence audit. I attempted to
> stand up the real stack locally to run the suite, but **test execution was
> environmentally blocked**: the Docker registry is denied by the egress policy
> (`pgvector/pgvector:pg15` → 403), the host Postgres is v16 **without pgvector**,
> and a transitive Python dep (`pysher`) fails to build. I therefore did **not**
> reproduce `pytest` locally. Where I rely on test results, the evidence is the
> repo's **GitHub Actions CI history**, which is green on `main` across the last
> ~15 runs (backend migrate+pytest against real pgvector/redis/openfga, web
> typecheck+build, static e2e). Anything I could not verify is labelled
> **UNVERIFIED**.

---

## A. Executive Verdict

**Launchable for tightly controlled design partners — NOT for public beta or GA — after clearing the launch blockers in §B.**

Blunt rationale: Chronos is **much further along than a prototype**. The three
critical seams (permission / memory / broker) are real and centrally enforced,
tenant isolation is consistently applied at the query layer with dedicated
passing isolation tests, the credential vault is correct AES-256-GCM, and the
audit log is genuinely immutable (REVOKE **plus** a `BEFORE UPDATE/DELETE`
trigger that fires even for the table owner). CI is green against the real
infrastructure.

However, the gap between "passes CI" and "an enterprise can rely on it" is real:

1. **The shipped production manifest (`render.yaml`) cannot run the product as
   written** — free Postgres has no pgvector (migrations with `VECTOR` columns
   fail), no background **worker** service is deployed (the connector framework
   queue is never drained), and the default model IDs are free-tier/likely
   non-existent.
2. **Intra-org authorization is off by default in production** — the OpenFGA
   relationship layer that governs project/workspace/task access is not deployed
   in `render.yaml`, so least-privilege inside a tenant is not enforced (only
   org-isolation + admin/approval role gates are always-on).
3. **Single-process runtime assumptions** — APScheduler schedulers and the agent
   task runner run *inside the web dyno*; horizontal scaling duplicates scheduled
   work, and `recover_incomplete_workflows` only recovers `tenant_id="default"`.
4. **Several advertised capabilities are demo/fixture or disabled** out of the
   box (Gmail send permanently blocked; Gmail/browser default to demo/fixture
   tiers returning placeholder data).

None of these are deep architectural defects — they are deployment, operational,
and configuration gaps. They are, however, blocking for anyone beyond a
hand-held design partner.

---

## B. Launch Blockers

| # | Blocker | Why it blocks |
|---|---------|---------------|
| B1 | `render.yaml` Postgres `plan: free` has **no pgvector**; schema uses `VECTOR(1536)` + ivfflat. `preDeployCommand: alembic upgrade head` will fail. | Product cannot deploy at all on the shipped manifest. |
| B2 | **No worker service** deployed. `connectors/worker_main.py` drains the Redis connector-execution queue; nothing runs it in prod. | Connector-framework workflows/plans enqueue and never execute. |
| B3 | **OpenFGA not deployed** (`OPENFGA_API_URL` unset) → project/workspace/task relationship checks disabled in prod. | Intra-tenant least-privilege (the enterprise selling point) is not enforced; any org member can act on any project/workspace/task in their org. |
| B4 | In-process **APScheduler + task runner in the web process**, no leader election. | Scaling web `>1` instance runs every schedule N times; long tasks die on deploy/restart. |
| B5 | `recover_incomplete_workflows(tenant_id="default")` hardcodes the default tenant. | Non-default tenants' interrupted workflows are never recovered after restart — silent data/work loss. |
| B6 | Default model IDs (`deepseek-v4-flash:free`, `nemotron-3-super-120b`, `minimax-m2.5`, `gemini-embedding-2`) are free-tier/unverified; embeddings + agents hard-depend on the provider. **UNVERIFIED** these models exist/are stable. | Memory, chat, and agents fail or rate-limit under real load on free models. |
| B7 | No reproducible **backup/restore** or **DR** artifact; Redis is `plan: free` with `allkeys-lru` (no persistence). | No recovery proof; Redis eviction silently drops rate-limit/loop-detection/idempotency/vault-cache state. |

---

## Architecture Summary

**Backend** (`apps/api`, FastAPI async, SQLAlchemy Core + reflected tables,
Alembic 48 migrations). 33 routers mounted in `main.py`. All risky paths route
through three frozen seams in `core/`:

- `permissions.check(actor, action, resource)` — two enforcement layers:
  always-on deterministic role gates (approvals + admin governance) and
  OpenFGA relationship checks (project/workspace/task) that **fail closed** when
  enforcement is enabled but the server is unreachable. Unmapped non-generic
  actions fail closed (`denied_unmapped`).
- `memory.retrieve(query, ctx)` — pgvector cosine search scoped by
  `organization_id` **and** authorized `(scope, scope_id)` pairs; graceful
  degradation to recent-memory on embedding/dimension failure; task scratchpad
  layered first.
- `tool_broker.execute(agent, tool, args)` — single connector gateway:
  permission check → rate limit → token budget → loop detection → safety limits
  → always-approval hard floor → tool-policy → graduated-autonomy gate →
  idempotency replay → audit → tiered connector routing → untrusted-content
  scan → trust ledger.

**Runtime** (`runtime/`): planner/executor/agent_loop, sub-agent manager (depth
≤3), Redis-backed `leases` for multi-worker task coordination, durable task
runner with recovery on startup.

**Frontend** (`apps/web`, Next.js 14 App Router). 26 route groups, real
`lib/api.ts` `apiFetch` wiring (no mock data found in components). Cross-host
cookie/session design (`SameSite=None; Secure` in prod).

**Data**: every table carries `organization_id` + `region`; `audit_log` is
append-only (REVOKE + trigger); `trust_events` similarly immutable.

**Tenancy**: host/header → `resolve_org_id` middleware → `request.state`;
JWT carries `org`; data isolation enforced by `member.organization_id` query
scoping (verified across routers), with token tenant-binding as secondary
defense.

---

## Feature Inventory

| Product area | Intended outcome | Key files | Status | Evidence | Risk |
|---|---|---|---|---|---|
| Permission seam | Govern every action | `core/permissions.py` | **Complete (config-gated)** | Role gates always-on; FGA layer present, fails closed; but disabled w/o `OPENFGA_API_URL` | high (B3) |
| Memory seam | Scoped recall | `core/memory.py`, `embeddings.py` | **Complete** | Org+scope SQL filter; graceful degrade; CI `test_memory_parity` | medium |
| Tool broker | Single governed gateway | `core/tool_broker.py` | **Complete** | Full gate chain; idempotency; loop/rate/safety | medium |
| Tenant isolation | No cross-org access | routers/* | **Complete** | Every query filters `organization_id`; `test_tenant_isolation_http` green | low |
| Audit log | Immutable trail | `core/audit.py`, `0005_*` | **Complete** | REVOKE + reject trigger | low |
| Credential vault | Encrypted secrets | `connectors/vault.py` | **Complete** | AES-256-GCM, tenant-scoped read, vault_ref-only logs | low |
| Auth/session | Login, sessions | `core/auth.py`, `cognito.py`, `signup.py` | **Working w/ limits** | JWT HS256; prod secret guard; `enforce_org_bound_tokens=False` default | medium |
| Gmail connector | Read/draft/send | `connectors/gmail.py` | **Partial** | Live OAuth+REST + demo fallback; **send permanently blocked**; demo file not tenant-scoped | high |
| Browser operator | Web automation | `connectors/browser*.py` | **Partial** | Live Playwright OR fixtures; defaults to fixture w/o Tavily/Chromium | medium |
| Other connectors | fs/code/repo/data/doc/image/voice/computer/desktop/canva/mcp | `connectors/*` | **Mixed** | Many `live`; external ones tier to demo/fixture; degraded-annotation present | medium |
| Connector framework workflows | Async connector plans | `connectors/framework/*`, `worker_main.py` | **Broken in prod** | Queue needs worker; none deployed | blocker (B2) |
| Agent tasks | Plan/execute/resume | `runtime/*`, `routers/tasks.py` | **Working w/ limits** | Leases + recovery; but runs in web dyno | high (B4) |
| Schedules/monitors | Recurring work | `jobs/scheduled_tasks.py`, `routers/schedules.py` | **Working w/ limits** | APScheduler in-process, no leader election | high (B4) |
| Approvals | Human-in-loop | `routers/approvals.py` | **Working** | `test_approval_flow_http` green; role-gated decisions | low |
| Artifacts + versions/share | Create/edit/version/publish | `core/artifacts*`, `routers/artifact*` | **Working** | Org-scoped; `token_urlsafe(24)` shares w/ revoke | low |
| Billing/usage | Plan enforcement | `core/billing*`, `routers/billing.py` | **UNVERIFIED depth** | `test_billing*` green (happy path) | medium |
| SSO/SCIM | Enterprise auth | `core/sso.py`, `scim.py` | **UNVERIFIED depth** | Tests green; needs live IdP proof | medium |
| Research/deep-research | Multi-step research | `runtime/research_executor.py` | **Working w/ limits** | Recovery on startup; provider-dependent | medium |
| Observability | Langfuse/Sentry | `main.py` | **Optional/Complete** | Wired if keys present | low |
| Deployment | Prod infra | `render.yaml`, `infra/`, `deploy-aws.yml` | **Broken as written** | B1/B2/B7 | blocker |

---

## Phase 3 — Functional Walkthrough Ratings

| Flow | Rating | Notes |
|---|---|---|
| Signup / login / session expiry | **Working w/ limits** | JWT + cookie; prod secret guard fails closed; `enforce_org_bound_tokens` off by default (legacy org-less tokens accepted). |
| Organization isolation (IDOR) | **Verified working** | By-ID fetches all filter `organization_id` (artifacts L37, tasks L149-262). CI isolation tests green. |
| Chat: stream/persist/refresh/models | **Working w/ limits** | SSE streaming + persistence; model availability provider-dependent. |
| Agent tasks: plan/execute/resume/cancel | **Working w/ limits** | Lease-coordinated recovery on startup; runs in web process (B4). |
| Approvals: pause/decide/audit | **Verified working** | Role-gated, hard-floor tools always require approval record. |
| Artifacts: edit/version/diff/restore/share | **Working** | Durable in object storage; share tokens revocable. Restore/diff CI-covered. |
| Memory: scope/privacy/embedding failure | **Working** | Privacy gate, scoped retrieval, graceful degrade. |
| Connectors: OAuth/refresh/execute | **Partial** | Gmail live path real (token refresh + retry); defaults to demo. Framework async jobs broken in prod (B2). |
| Governance: RBAC/immutable audit/redaction | **Working w/ caveat** | Audit immutable; creds never logged; **project ACLs off w/o OpenFGA (B3)**. |
| Reliability: restart/worker recovery | **Partial** | Task/research recovery present; **workflow recovery default-tenant-only (B5)**; Redis eviction risk (B7). |
| Admin/internal ops | **Working w/ limits** | Admin console, audit export, suspend-org; impersonation not reviewed. |

---

## Phase 4 — Security & Governance Findings

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| S1 | **Critical** | Intra-org authorization disabled by default in prod (OpenFGA not deployed). Any member can view/edit/manage any project/workspace/task in their org. | `core/permissions.py:351` `enforce = authz.is_enabled() and ...`; `render.yaml` has no `OPENFGA_API_URL`; `config.py:208` default empty. |
| S2 | **High** | `enforce_org_bound_tokens=False` default → legacy org-less JWTs accepted; tenant-binding only secondary. | `core/auth.py:99-116`, `config.py:75`. |
| S3 | **High** | `/health` runs a **real billed model completion** on every call, unauthenticated. Cost-amplification / DoS vector; couples readiness to provider. | `main.py:312-323`. |
| S4 | **Medium** | Demo Gmail drafts written to shared `/tmp/chronos_demo_drafts.jsonl` — **not tenant-scoped** (cross-tenant read in demo mode; ephemeral). | `connectors/gmail.py:50,331-355`. |
| S5 | **Medium** | `per_org_daily_token_limit=0` by default → no cost ceiling; broker token-budget guard is a no-op unless set. | `tool_broker.py:390`, `config.py:227`. |
| S6 | **Low/Positive** | Audit log immutable (REVOKE + trigger); vault AES-256-GCM tenant-scoped; untrusted-content scanning + write-block on untrusted-triggered writes; SSRF guard module present; skill path-traversal guard. | `0005_*`, `vault.py`, `tool_broker.py:125-180,250-289`, `core/ssrf.py`. |
| S7 | **Medium** | Public share links are unauthenticated by token; verify expiry + rate-limiting on `artifact_share`. **UNVERIFIED** (no expiry field seen in `get_active_share_by_token`). | `routers/artifact_share.py`, `core/artifact_shares.py`. |
| S8 | **Low** | Default OpenRouter `HTTP-Referer: http://127.0.0.1:3000` hardcoded in embeddings. Cosmetic/leak-minor. | `embeddings.py:40`. |

Cross-tenant data access (the highest-impact class) is **well-defended** at the
query layer and CI-proven. The S1 gap is *intra*-tenant, not cross-tenant.

---

## Phase 5 — Test / Build / Deployment Validation

| Command | Result | Notes |
|---|---|---|
| `docker compose up postgres redis` | **FAIL (env)** | Registry egress denied (403) — not a repo defect. |
| `pip install -r requirements.txt` | **FAIL (env)** | `pysher` wheel build error under this image's setuptools. |
| `pytest` (local) | **NOT RUN** | Blocked by the two above. |
| CI: backend migrate + pytest | **PASS (CI)** | Green on `main` last ~15 runs; real pgvector/redis/openfga + Playwright chromium. |
| CI: web typecheck + build | **PASS (CI)** | `npm ci && npm run build`. |
| CI: static e2e route guards | **PASS (CI)** | `*-static.spec.ts`. |
| CI: behavioral e2e | **GATED** | Only runs if `OPENROUTER_API_KEY` secret present; otherwise skipped → behavioral flows are **not continuously proven**. |
| `render.yaml` deploy | **WILL FAIL** | B1 (pgvector), B2 (no worker), B7 (free Redis). |
| Backup / restore | **ABSENT** | No documented/tested DR path. |

**Test-coverage character:** the 105 backend test files are predominantly
*happy-path + invariant proofs* (isolation, governance, append-only). Thin areas:
provider outage / partial failure, Redis eviction mid-flight, worker crash
mid-job, OAuth token-expiry recovery against a live IdP, scheduler duplication
under multi-instance, billing metering accuracy under concurrency.

---

## C. Prioritized Remediation Roadmap

### 1. Immediate stabilization (24–72h) — all P0
- **B1/B7 infra**: Move Postgres to a pgvector-capable plan; provision a
  persistent Redis (AOF/RDB). *AC:* `alembic upgrade head` succeeds on the prod
  DB; `CREATE EXTENSION vector` present. *Owner:* platform. *Effort:* S.
- **B2 worker**: Add a `worker` service running `python -m connectors.worker_main`
  (+ confirm whether the agent task runner should also move out of web). *AC:*
  enqueued connector jobs complete in prod; a smoke workflow runs end-to-end.
  *Owner:* platform/backend. *Effort:* M. *Test:* integration job round-trip.
- **B3 authz**: Deploy OpenFGA and set `OPENFGA_API_URL`; run
  `reconcile_org_tuples` on every existing org. *AC:* a non-member is 403'd from
  another member's project; reconcile backfills without lockout. *Owner:*
  security/backend. *Effort:* M. *Test:* negative authz HTTP tests in prod config.
- **S3 health**: Drop the model call from `/health` (or move to an
  authed/`/health/deep` with caching). *AC:* `/health` makes zero billed calls.
  *Owner:* backend. *Effort:* XS. *Test:* assert no litellm call in `/health`.
- **B6 models**: Pin verified, paid, stable model IDs; document required keys.
  *AC:* chat + embeddings succeed against the pinned models under load. *Owner:*
  product/backend. *Effort:* S.

### 2. Core launch readiness (1–2 weeks) — P1
- **B4 scheduler/runtime**: Add leader election (or a dedicated scheduler dyno)
  so schedules fire once; pin web to 1 instance until done. *AC:* a schedule with
  2 instances fires once. *Test:* multi-instance scheduler test. *Effort:* M.
- **B5 workflow recovery**: Iterate all tenants in
  `recover_incomplete_workflows`. *AC:* a non-default tenant's interrupted run
  resumes after restart. *Test:* multi-tenant recovery test. *Effort:* S.
- **S2 tokens**: Default `enforce_org_bound_tokens=True` (with a migration/grace
  window). *Effort:* S. *Test:* org-less token rejected.
- **S5 cost**: Set a sane `per_org_daily_token_limit`; alert on breach. *Effort:* S.
- **S4 demo Gmail**: Tenant-scope or disable demo draft storage in prod.
  *Effort:* XS.
- **S7 share links**: Add/verify expiry + revoke + access logging + rate limit.
  *Effort:* S. *Test:* expired/revoked token 404.
- **DR**: Document + rehearse backup/restore for Postgres and vault. *Effort:* M.

### 3. Beta hardening (2–4 weeks) — P2
- Failure-mode test coverage: provider outage, Redis eviction, worker crash
  mid-job, OAuth refresh against live IdP, scheduler duplication.
- Un-gate (or add a nightly) behavioral e2e so chat/research/agent flows are
  continuously proven.
- Gmail `send` decision: implement post-approval send or remove it from the
  advertised surface.
- Load/limit testing for rate limits, timeouts, retries, dead-letter, idempotency.

### 4. Post-launch scale / enterprise (P3)
- Extract task runner to dedicated workers; queue-based agent execution.
- SSO/SCIM live-IdP certification; data-residency/region enforcement proof.
- Per-connector reliability SLOs; secret rotation; key-management (KMS) for vault.

---

## D. Critical Path (smallest sequence to a reliable design-partner launch)

1. pgvector-capable Postgres + persistent Redis (B1, B7).
2. Deploy worker service (B2).
3. Deploy OpenFGA + reconcile tuples (B3).
4. Pin real models + keys (B6).
5. Remove model call from `/health` (S3).
6. Pin web to a single instance and fix multi-tenant workflow recovery (B4 interim, B5).

Everything else is parallelizable and not on the critical path for a hand-held
design-partner launch.

---

## E. Launch Gates (objective go/no-go)

- **Tests:** backend + web + static-e2e green **and** behavioral-e2e green in
  the launch config (not skipped). 100% of the flows in §F below pass.
- **Security (zero tolerance):** OpenFGA enforcing (negative authz test passes);
  org-bound tokens enforced; no billed calls on unauthenticated endpoints;
  cross-tenant IDOR suite green; audit immutability proven on the prod DB.
- **Required flows:** signup→login→chat(stream+persist+refresh)→create task→
  watch activity→approval→Gmail **draft** appears in a live connected account.
- **Monitoring:** Sentry + Langfuse keys set; `/health` (cheap) wired to the
  platform health check; alerting on error rate and token-budget breach.
- **Backup/restore:** one rehearsed restore of Postgres + vault from backup.
- **Tenant isolation proof:** automated cross-tenant + intra-tenant (FGA) denial.
- **Approval/audit proof:** hard-floor tool blocked without approval; audit
  UPDATE/DELETE rejected by trigger.
- **Integration reliability:** OAuth connect→refresh→execute→disconnect on a
  live Google account; worker drains a real connector job.
- **Recovery:** kill the worker/web mid-task and confirm resume for a
  non-default tenant.

---

## F. Recommended Release Strategy

**Private design-partner beta (invite-only), 2–5 friendly orgs, hands-on.**

- **First cohort:** internal + 2–3 trusted design partners who accept a
  white-glove setup and known limits.
- **Product limits:** OpenFGA enforcing; web pinned to 1 instance until B4
  fixed; per-org token cap on; **disable** anything still demo/fixture in the
  target tenant (browser without Tavily/Chromium, Gmail without OAuth).
- **Manually approve / disable risky features:** Gmail `send` (drafts only),
  external publish (twitter/linkedin/website — already hard-floor), local/cloud
  computer exec (already hard-floor).
- **Promotion metrics to invite-only/public beta:** zero cross-tenant or authz
  escapes; behavioral-e2e green for 2 weeks; task/workflow recovery proven under
  induced restarts; connector OAuth refresh stable; p95 chat latency and error
  rate within target on the pinned paid models; one successful DR rehearsal.

---

## G. Final Issue Register (ranked)

| ID | Sev | Title | Area | Effort | Test to add |
|---|---|---|---|---|---|
| B1 | Blocker | Prod Postgres lacks pgvector; migrations fail | infra | S | migrate-on-clean-prod gate |
| B2 | Blocker | No worker deployed; connector queue never drained | platform | M | queue round-trip integration |
| B3 | Blocker→S1 | OpenFGA not deployed; intra-org authz off | security | M | negative authz HTTP (prod cfg) |
| B4 | Blocker | In-process scheduler/runner; duplicates on scale | platform | M | multi-instance single-fire |
| B5 | Critical | Workflow recovery default-tenant only | backend | S | multi-tenant recovery |
| B6 | Critical | Default model IDs free/unverified | product | S | model-reachability smoke |
| B7 | Critical | No DR; free Redis evicts critical state | platform | M | restore rehearsal |
| S2 | High | Org-bound tokens not enforced by default | security | S | org-less token rejected |
| S3 | High | `/health` makes billed unauth model call | backend | XS | no-LLM-in-health assertion |
| S4 | Medium | Demo Gmail drafts not tenant-scoped | backend | XS | tenant-scoped demo store |
| S5 | Medium | No default token/cost cap | backend | S | budget-exceeded path |
| S7 | Medium | Share-link expiry/rate-limit unverified | backend | S | expired/revoked → 404 |
| F1 | Medium | Gmail `send` permanently disabled despite approvals | product | M | post-approval send or remove |
| F2 | Medium | Behavioral e2e gated/skipped by default | QA | S | nightly behavioral run |
| F3 | Medium | Connectors default to demo/fixture silently | product | S | prod "live-only" guard |
| S8 | Low | Hardcoded referer in embeddings | backend | XS | — |

---

### Appendix — What I verified vs. could not

**Verified (code-read + CI):** three seams, tenant query-scoping across routers,
audit immutability migration, vault crypto, broker gate ordering, memory scope
SQL, auth/JWT flow, config production guards, CI green history, frontend→API
wiring, render/CI manifests.

**UNVERIFIED (label honestly):** local `pytest` (env-blocked), runtime behavior
of agents/research/billing/SSO/SCIM beyond happy-path tests, existence/stability
of the default model IDs, share-link expiry semantics, behavioral e2e outcomes
(gated), real OAuth refresh against live Google.
