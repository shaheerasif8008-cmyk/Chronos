# Chronos — Full Launch-Readiness Plan

> **Provenance note.** The requested file `Chronos master codex directive v2.md`
> does not exist in this checkout (searched `main` and
> `claude/chronos-codex-v2-launch-r4qbc1`, root and `docs/`). This plan is
> therefore derived from the current controlling authorities:
> `CHRONOS_TOTAL_PARITY_GOAL.md`, `docs/chronos_total_parity_matrix.md`,
> `docs/LAUNCH_READINESS_AUDIT.md`, `docs/PRODUCTION_CONFIGURATION.md`,
> `docs/PRODUCTION_OPERATIONS.md`, `docs/TERRAFORM_STATE_ADOPTION.md`, and
> `docs/DISASTER_RECOVERY.md`. If the directive text exists elsewhere, hand it
> over and this plan will be reconciled against it verbatim.

## CEO/CTO Executive Assessment

**Chronos is no longer a build problem. It is a proof-and-operate problem.**

The repository is deep and largely feature-complete against the parity matrix:
38 API routers, 73 Alembic migrations (head chain reaching
`0069_artifact_share_expiry`), 142 backend test suites, 40 Playwright E2E specs,
a complete Terraform AWS stack (`infra/`), and three CI workflows. The three
critical seams (permission / memory / broker) are real and centrally enforced,
tenant isolation is proven at the query and HTTP boundary, and the audit log is
genuinely immutable.

What is missing is **not more features**. Every row in the parity matrix that
says "implemented" is qualified with "live provider proof / deployed-SHA
evidence outstanding." The launch-readiness audit confirms the target AWS
account has **no running Chronos stack**, no verified provider accounts, no
hardened identity, no protected release, and no single immutable SHA that has
passed the full gate set end-to-end.

**Blunt verdict:** the critical path to launch is roughly 70% operations /
external-configuration / live-proof and 30% code. Most of the remaining code
is closing honest degraded-path gaps and fixing whatever the *first real
deployment* surfaces — you cannot know those until the stack is actually up.

### The single most important strategic decision

Do not aim for full public GA as the first milestone. Aim for a **controlled
design-partner launch** (2–5 hand-held orgs) on one immutable, fully-gated
release, then earn GA with two weeks of clean operating evidence. This is the
recommendation of the existing audit and I concur strongly. It converts an
open-ended "prove everything at once" problem into a bounded, sequenced one.

### What blocks us that I (the agent) cannot do alone

These require a human with account/credential/finance/legal authority. I can
prepare every script, config, and runbook, but I cannot create the accounts or
spend the money:

- AWS account provisioning, `terraform apply`, GitHub OIDC deploy role, DNS,
  ACM certificates, Cognito MFA hardening.
- Funding and verifying provider accounts: OpenRouter (paid models) + a
  **separately-keyed direct-provider fallback** (Anthropic/OpenAI), Browserbase
  (paid tier), E2B (desktop + repo templates), Composio (the current credential
  returns invalid — must be replaced), SendGrid (domain verification, SES
  de-sandbox), Slack/Teams app installs, Stripe products/prices/webhooks,
  Langfuse/Sentry projects.
- Apple Developer ID signing + notarization for the macOS desktop app.
- Legal/commercial: privacy terms, DPA, retention/deletion policy, SLA/SLO,
  incident + on-call ownership.

Everything else below, I can drive.

---

## Workstreams

Each workstream lists **Agent-executable** items (I can do these in the repo now)
and **Human-required** items (need credentials/accounts/authority). Gate = the
objective go/no-go evidence that closes the item.

### WS1 — Release Integrity (one immutable SHA)
*Owner: CTO / platform. The whole launch hangs on this.*

- **Agent:** Get the full backend suite, web typecheck/lint/build, static +
  behavioral E2E, and the complete Alembic chain green locally / in CI on a
  single commit. Fix whatever breaks. Pin every third-party GitHub Action to an
  immutable commit SHA (audit says done — I will re-verify).
- **Agent:** Add/repair a nightly behavioral-E2E run so chat/research/agent
  flows are continuously proven, not gated-and-skipped.
- **Human:** Enable branch protection / required reviews / ruleset / environment
  on `main`; enforce reviewed protected releases before any deploy.
- **Gate:** CI green on one SHA across migrate + backend pytest + web build +
  static-e2e + behavioral-e2e + dependency scan + container scan + Terraform
  fmt/validate/test/plan + desktop build. That SHA's digest is what deploys.

### WS2 — AWS Infrastructure Bring-Up
*Owner: platform. Terraform already exists; it has never been applied to prod.*

- **Agent:** Dry-run `terraform fmt/validate/plan`; reconcile
  `TERRAFORM_STATE_ADOPTION.md`; prepare `terraform.tfvars` from
  `terraform.tfvars.example` with placeholders; sanity-check ECS service
  definitions (API + worker ≥2 tasks, OpenFGA app+migration, scheduler leader).
- **Human:** Create AWS account/state backend, run the guarded `apply`, adopt
  state per the adoption doc, wire the GitHub OIDC deploy role.
- **Human:** Run the one-off production migration task (base → `0069`), retain
  head/constraint/FK/schema evidence from the *applied* environment.
- **Gate:** Live ECS/RDS(pgvector)/ElastiCache/ALB/ECR/Secrets/WAF/OpenFGA
  stack healthy; migration evidence retained; `app`/`api` DNS resolve to the
  live ALB with valid ACM certs.

### WS3 — Provider Configuration & Live Proof
*Owner: product + platform. This is the long pole.*

Per capability, the pattern is identical: fund account → set secret in Secrets
Manager → run a controlled live exercise → capture evidence → confirm degraded
behavior is honest. A non-empty secret is **not** readiness.

- **Models:** paid OpenRouter primary + separately-keyed direct fallback; force
  a primary outage, prove fallback, no duplicate partial stream/tool action,
  embeddings at 1536 dims, quota/latency/spend. **(Human funds; Agent writes
  the exercise harness.)**
- **Browserbase:** paid tier; navigate/login/takeover/clean+malicious
  download/upload/cross-replica restart/expiry/revoke.
- **E2B:** desktop + repo templates; real pixels/input/resume, allowed-domain
  success + unlisted-IP denial, quota, destruction, cost.
- **Gmail/Composio:** replace the invalid Composio credential; member-scoped
  search/draft/approve/send/crash-retry/revoke.
- **Stripe / SendGrid / Slack / Teams / GitHub publication:** live install,
  signature, delivery, retry/dead-letter, revoke evidence.
- **Gate:** every enabled capability has a captured live success **and** a
  captured honest-degraded/outage path; anything unproven is disabled in the
  launch tenant rather than shipped as demo.

### WS4 — Security & Governance Live Exercises
*Owner: security. Static contracts exist; live proof does not.*

- OpenFGA enforcing + fail-closed on outage; negative intra-tenant authz.
- Org-bound tokens enforced by default; org-less token rejected.
- No billed model call on any unauthenticated endpoint (verify `/health`).
- Per-org token/cost ceiling set and alerting on breach.
- Live prompt-injection block, sandbox-egress denial, ClamAV fail-closed,
  WAF/rate-limit, duplicate-delivery prevention, secret redaction, cross-tenant
  isolation — all on the deployed image digest.
- **Human:** Cognito MFA on operators/pools; confirmed paging route.
- **Gate:** the full security exercise set passes on the deployed SHA;
  cross-tenant and intra-tenant denial automated and green.

### WS5 — Reliability, Capacity & Disaster Recovery
*Owner: platform.*

- Multi-replica exactly-once schedules/monitors; task restart + cancellation +
  failure injection under the deployed topology; non-default-tenant workflow
  recovery.
- Measured load/capacity, SLO, p95 latency on the pinned paid models.
- Full DR rehearsal per `DISASTER_RECOVERY.md`: app/OpenFGA/S3/Redis
  reconciliation, tenant/authz validation, measured RTO/RPO, cross-Region
  promotion/failback, ECR tag-overwrite rejection + backup-Region replication.
- **Gate:** one rehearsed restore + one promotion/failback with retained
  evidence and measured RTO/RPO within target.

### WS6 — Product QA (Computer Use / behavioral)
*Owner: QA. This is where "implemented in repo" becomes "works for a client."*

- Exhaustive Computer Use walkthrough of every desktop **and** mobile route,
  modal, and state on the release candidate.
- Accessibility conformance: automated axe + VoiceOver/keyboard/zoom/contrast
  on every route and modal (currently only source-contract "static" specs).
- Execute the six **Final Parity Proof Scenarios** from the matrix end-to-end on
  the deployed SHA: ChatGPT-style, Claude-style, Manus-style, enterprise
  governance, connector ecosystem, reliability-and-safety.
- **Gate:** all six combined scenarios pass on one deployed release; a11y
  evidence captured; no P0/P1 defects open.

### WS7 — Desktop App Release
*Owner: platform + Apple account holder.*

- **Human:** configure Apple Developer ID secrets; run `desktop-release.yml`;
  notarize/staple; install on a clean Mac; pair/execute/revoke/notify.
- **Gate:** signed, notarized, stapled GitHub release; live pairing smoke.

### WS8 — Commercial & Operational Readiness
*Owner: CEO / ops / legal.*

- On-call + incident + support process with named owners; alert routing proven.
- Privacy/DPA terms, retention/deletion policy, SLA/SLO published.
- Client onboarding + rollback ownership; billing support path.
- **Gate:** signed-off operations contract per `PRODUCTION_OPERATIONS.md`.

---

## Sequenced Timeline (critical path)

Ordering matters: you cannot prove providers without a stack, cannot certify a
release without CI green, cannot certify DR without data services live.

1. **WS1 release integrity green on one SHA** *(prerequisite for everything)*.
2. **WS2 AWS bring-up + production migration** *(unblocks all live proof)*.
3. **WS3 providers + WS4 security**, in parallel, on the live stack.
4. **WS5 reliability/DR + WS6 product QA**, in parallel, once providers are live.
5. **WS7 desktop + WS8 commercial**, in parallel throughout (not on the model's
   critical path but required for the launch bar).
6. **Go/No-Go** against the gate checklist → **design-partner launch**.
7. Two weeks clean operating evidence → **invite-only → GA**.

## Objective Go/No-Go Gate (design-partner launch)

- [ ] One immutable SHA passed the full CI + behavioral-E2E + Terraform +
      desktop gate set.
- [ ] Production migration applied base→head with retained schema evidence.
- [ ] OpenFGA enforcing; org-bound tokens enforced; zero billed calls on
      unauthenticated endpoints; cross-tenant + intra-tenant denial green.
- [ ] Paid models + funded direct fallback proven under forced primary outage.
- [ ] Every enabled capability has live success **and** honest-degraded proof;
      unproven capabilities disabled in the launch tenant.
- [ ] The six Final Parity Proof Scenarios pass on the deployed SHA.
- [ ] One rehearsed DR restore with measured RTO/RPO.
- [ ] Sentry + Langfuse live; alerting + paging owned; a cheap `/health` wired
      to the platform check.
- [ ] Commercial/legal/support contract signed off.

Until every box is checked on **one** release, Chronos is not represented as
GA-ready for unattended real-client use.

## What I will start on immediately (no credentials required)

1. Stand up the test stack in this environment and run the full backend suite +
   the complete Alembic chain; produce a real pass/fail ledger (the audit could
   only rely on CI history — I will get first-hand results).
2. Run web typecheck/lint/build and the Playwright static specs.
3. `terraform fmt/validate` and a credential-free `plan` review of `infra/`.
4. Grep-audit the three seams for any direct-connector / direct-memory /
   inline-permission bypass, and confirm CI actions are commit-pinned.
5. Produce a per-capability "live proof runbook" so each Human-required exercise
   is a scripted checklist, not an open question.

Then report a concrete, evidence-backed gap list and drive the code-side items
to green while the account/credential work proceeds in parallel.
