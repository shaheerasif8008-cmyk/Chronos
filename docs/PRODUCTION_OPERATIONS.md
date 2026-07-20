# Chronos Production Operations

This is the operational runbook for the Terraform-managed AWS production path.
It covers first deployment, routine releases, monitoring, incidents,
maintenance, and client onboarding. It does not certify the current AWS,
provider, DNS, or identity configuration as complete.

Read these companion documents before changing production:

- [`PRODUCTION_CONFIGURATION.md`](PRODUCTION_CONFIGURATION.md) — required
  accounts, values, provider setup, and launch evidence;
- [`TERRAFORM_STATE_ADOPTION.md`](TERRAFORM_STATE_ADOPTION.md) — state/backend
  safety and import/migration procedure; and
- [`DISASTER_RECOVERY.md`](DISASTER_RECOVERY.md) — backup, restore, regional
  recovery, and rehearsal procedure.

## Production architecture

The supported production path is AWS ECS/Fargate in private subnets:

- public web and API ALBs terminate TLS and are protected by separate WAF ACLs;
- API, web, connector worker, and OpenFGA services run at two or more tasks;
- the API and worker share the same validated runtime configuration;
- Chronos and OpenFGA use separate encrypted Multi-AZ PostgreSQL 15 RDS
  instances;
- Redis 7 uses TLS/auth, Multi-AZ failover, `noeviction`, and snapshots;
- artifacts use a private KMS-encrypted, versioned S3 bucket;
- secrets are read from Secrets Manager through the ECS execution role;
- API/web image tags are immutable, published only under the release Git SHA,
  and replicated into immutable backup-Region ECR repositories;
- CloudWatch, EventBridge, encrypted SNS, WAF logs, and AWS Budgets provide the
  operator signal path; and
- a KMS-encrypted, versioned, COMPLIANCE-locked audit bucket receives the
  validated multi-Region CloudTrail, and a separate KMS-encrypted/versioned
  bucket receives AWS Config snapshots (AWS Config does not support an S3
  delivery channel with default Object Lock retention); GuardDuty, Security
  Hub, Inspector, and IAM Access Analyzer provide account-level detection and
  posture findings; and
- AWS Backup protects both databases and artifacts with cross-Region copies.

The application database, artifact bucket, and OpenFGA datastore are durable
sources. Redis also carries operational state that must be reconciled after
loss; it must not be treated as harmless cache-only infrastructure.

## Change-control rules

- All production changes have a ticket, owner, reviewer, rollback path, and
  evidence location.
- Infrastructure changes use a saved reviewed Terraform plan. Release changes
  use immutable Git-SHA image tags.
- No operator pastes secrets or Terraform state into logs, screenshots, tickets,
  or chat.
- Database/OpenFGA migrations run once as one-off ECS tasks before service
  revisions deploy.
- Routine releases cannot skip migrations. The only workflow path that omits
  them is the explicit `bootstrap_images_only` first-deploy phase, which also
  cannot update or start a service.
- A failed smoke test is a failed deploy even if ECS reports stable tasks.
- Never disable OpenFGA enforcement, WAF, backup, alarms, or production startup
  guards to make a release pass.
- Apply/deploy/restore/DNS changes are serialized. The GitHub deploy workflow
  intentionally does not cancel an in-progress deployment.

## Launch-blocking checklist

Do not route real clients until every item has current evidence:

- State adoption is complete and the reviewed normal plan has no unexplained
  replacement/deletion.
- Terraform's production guard passes with `platform_bootstrap_mode=false`.
- Wildcard web and exact API certificates are issued in the primary Region.
- App, API, and wildcard tenant DNS resolve to the current ALBs over HTTPS.
- Cognito production login, logout, recovery, invitation, tenant selection, and
  approved MFA policy work for owner/admin/member test users.
- GitHub OIDC assumes the exact Terraform-managed deploy role; no long-lived AWS
  keys are used.
- API, web, worker, and OpenFGA each have the required running tasks across
  availability zones.
- RDS, Redis, S3, OpenFGA, and computed connection secret checks pass.
- Production provider configuration and capability smoke tests in
  `PRODUCTION_CONFIGURATION.md` pass.
- The operations SNS subscription is confirmed and a test alert reaches the
  monitored route.
- The CloudWatch dashboard, WAF logs/alarms, RDS/Redis alarms, backup alarms,
  and monthly budget are visible to the on-call operator.
- CloudTrail is actively logging global management events and artifact-object
  data events with validation enabled; Config is recording; GuardDuty,
  Security Hub standards, Inspector, and Access Analyzer are enabled; security
  alarms reach the confirmed operations route; and findings have an owner.
- Current primary and cross-Region recovery points exist; the latest automated
  restore test passed; a full application-level restore rehearsal has recorded
  evidence.
- ECR rejects tag overwrites, no `latest` tag is published, and released image
  digests are present in both the primary and backup Regions.
- CI, behavioral E2E, production-edge smoke, cross-tenant authorization, and
  exhaustive desktop/mobile browser audits pass on the released SHA.
- On-call rotation, incident communications, data-processing/privacy terms,
  support ownership, client SLA/SLO, RTO/RPO, retention, and deletion procedures
  are approved outside the codebase.

## First deployment: zero-task bootstrap

The first deployment has a circular dependency: Terraform must create ECR,
networking, databases, secrets, and task definitions before images/migrations
exist, while services must not start against missing images or schemas.
`platform_bootstrap_mode=true` resolves this by creating the platform with all
service counts at zero.

### 1. Configure external dependencies

Complete the provider, identity, DNS/certificate, GitHub, operations, and billing
inventory. Request and validate certificates before planning because the
production guard looks up the issued wildcard/API certificates.

For coding workspaces, create the dedicated E2B repo template with git, Python,
and pytest, then set the `e2b_repo_*` inputs from the configuration guide. Create
the direct GitHub OAuth App and register
`https://api.cognisiatech.com/connectors/github/oauth-callback`; this is separate
from GitHub Actions OIDC. The OAuth token stays in the encrypted Chronos vault:
private source archives are fetched by the API and only repository bytes enter
the isolated E2B workspace.

For cloud computers, create a separate hardened E2B desktop template containing
Xvfb, XFCE, `scrot`, and `xdotool`; do not reuse the deny-egress code/data or
repository images. Set `e2b_computer_template_id`, idle pause, hard consent
expiry, per-member/per-org quotas, resolution, and
`e2b_computer_egress_allowlist`. Keep the ceiling to approved DNS domains; each
network-enabled session requires the user to approve an exact-domain subset.
Set `e2b_repo_egress_allowlist` independently to only the Git and package hosts
needed by the repo template. Do not use IP literals or an all-internet rule.
Choose a paid E2B tier whose documented current concurrency exceeds the reviewed
aggregate code/data/computer/repo quotas with headroom for multiple tenants; do
not hard-code a vendor plan name or limit into the launch decision. Record the
provider-side quota and alert thresholds in the release evidence.
Before enabling clients, prove with a non-production tenant that one session can
capture pixels, accept bounded click/type input, auto-pause, resume from the same
sandbox id after an API restart, export an artifact, and be permanently destroyed
by both manual cancellation and the scheduled consent-expiry job.
For both network-enabled profiles, retain the provider policy metadata and
pre-use attestation showing an allowed TLS host succeeded while unlisted
`1.1.1.1:443` was blocked. A failed probe must destroy the sandbox before user
data is written.

The API task also includes an essential, loopback-only ClamAV sidecar. Confirm
that `/settings/runtime-health` reports the file-security check healthy, then
upload one clean fixture and the standard harmless EICAR test fixture through
both attachment and Browserbase download paths. The clean file must be usable;
EICAR must produce a durable infected verdict without an artifact/download;
stopping the sidecar must block new file ingress with a retryable service error.
Repeat the exercise with a connector-synchronized binary: the clean input must
persist only as generated text/plain content, while an infected or active input
must remove any older chunks and appear in Admin > File quarantine. Confirm an
admin can acknowledge/close or explicitly mark a false positive, and that no
review action offers a preview, download, restore, or original byte payload.
Capture only hashes, bounded engine/signature metadata, and audit/event ids in
release evidence—never fixture bytes or arbitrary scanner responses.

### 2. Bootstrap and adopt state

From `infra/`:

```bash
bash bootstrap.sh us-east-1 <approved-profile>
terraform init -reconfigure -input=false
```

Follow `TERRAFORM_STATE_ADOPTION.md` in full. Stop if the backend account/key or
live-resource inventory does not match the intended production stack.

### 3. Apply the zero-task platform

Create an ignored, access-restricted `terraform.tfvars` from the example. Set:

```text
platform_bootstrap_mode = true
api_image_tag            = "REPLACE_WITH_8_CHAR_GIT_SHA"
web_image_tag            = "REPLACE_WITH_8_CHAR_GIT_SHA"
```

All other production guards still apply, including providers, identity,
observability, billing, WAF, account security services, immutable audit
evidence, HA, alarms, and backups.

```bash
terraform fmt -check -recursive
terraform validate
terraform plan -input=false -out=bootstrap.plan
terraform show bootstrap.plan
terraform apply bootstrap.plan
bash post-apply.sh
```

Verify `terraform output -raw platform_bootstrap_active` is `true` and all four
ECS services have desired count zero.

### 4. Configure GitHub and push bootstrap images

Set `AWS_DEPLOY_ROLE_ARN` to `terraform output -raw
github_deploy_role_arn` and configure all required repository variables from
`PRODUCTION_CONFIGURATION.md`. The bootstrap commit must be the current `main`
head, have successful `CI` on that exact SHA, and be associated with an
approved merged pull request. Trigger **Deploy to AWS** manually from `main`
with `bootstrap_images_only=true`.

The workflow waits for either basic ECR scanning to complete or enhanced ECR
scanning to become active, and fails when either image has a HIGH or CRITICAL
finding. Record that scan result, the resulting eight-character image tag, and
both ECR image digests. The repositories reject overwrites and the workflow
does not publish `latest`, so a bootstrap rerun for an already-pushed SHA must
verify the existing digest rather than trying to replace it. Set that exact tag in `api_image_tag` and
`web_image_tag`, keep bootstrap mode true,
review/apply the task-definition update, and verify task definitions reference
the immutable tag.

### 5. Run datastore migrations exactly once

Use the approved production operator session from `infra/`:

```bash
bash run-openfga-migration.sh
bash run-app-migration.sh
```

Each helper starts a private one-off Fargate task, waits for it, and fails unless
the named container exits zero. Inspect `/ecs/chronos-prod/openfga` and
`/ecs/chronos-prod/migrate` if either fails. Do not start services or rerun a
failed migration blindly; determine whether it is safe and idempotent first.

### 6. Start production services

Set `platform_bootstrap_mode=false`. Review a new plan. It must start at least
two API, web, worker, and OpenFGA tasks and preserve all production safeguards.
Terraform tracks service desired counts (only deploy-managed task-definition
revisions are ignored), so this plan must explicitly show the zero-to-baseline
count transition.

```bash
terraform plan -input=false -out=enable-services.plan
terraform show enable-services.plan
terraform apply enable-services.plan
```

Wait for all services and target groups to stabilize. Confirm the bootstrap
output is now `false`.

### 7. Publish DNS and identity callbacks

Use Terraform outputs for ALB targets:

```bash
terraform output -raw api_alb_dns
terraform output -raw web_alb_dns
terraform output -raw tenant_web_dns_record
```

Update `api`, `app`, and wildcard tenant DNS together only after direct ALB
health proof. Confirm the Cognito callback/logout URLs and provider OAuth
callbacks use the final HTTPS origins.

### 8. Confirm alerts and complete launch proof

Confirm the SNS email/paging subscription, trigger a controlled test alarm,
inspect the operations dashboard, verify current backups, and run the full
post-deploy gate below. Attach evidence to the launch record.

## Routine deployment

Merges to `main` deploy only after the `CI` workflow succeeds. Automatic and
manual releases both machine-verify that the exact current `main` SHA has a
successful same-repository `CI` push run and an associated approved merged pull
request; manually selecting another ref or an older SHA fails closed. The AWS
workflow:

1. validates the reviewed SHA and exact successful CI run;
2. assumes the Terraform-managed role through GitHub OIDC;
3. validates public URL/smoke-tenant variables;
4. builds and pushes API/web images tagged with the source Git SHA;
5. waits for ECR basic/enhanced scan readiness and rejects HIGH/CRITICAL findings;
6. runs pinned OpenFGA and Chronos migrations with no routine skip input;
7. registers coordinated API, web, and worker task revisions;
8. waits for service stability and proves the requested revisions are active;
9. runs public health, Cognito config, app/tenant login, and CORS checks; and
10. restores previous API/web/worker revisions when a deploy step fails.

Before merge:

- review migration forward/backward compatibility and deployment order;
- verify configuration additions exist in `.env.example`, Terraform variables,
  Secrets Manager injection, and task definitions as appropriate;
- ensure provider/model changes have quota, cost, privacy, and failure-mode
  review; and
- identify the prior healthy task revisions and data rollback constraints.

After workflow success, an operator completes authenticated checks. Public edge
smoke is necessary but not sufficient.

## Post-deploy verification gate

### Platform and edge

- `GET /health/live` returns `status=ok`.
- `GET /ready` returns HTTP 200 with Postgres, Redis, S3, OpenFGA, and at
  least one current connector-worker heartbeat `ok` in production.
- `GET /health` returns `status=ok`, not `degraded`.
- `GET /auth/config?tenant=<smoke-tenant>` reports Cognito enabled and
  development OTP disabled.
- App and tenant login routes load over HTTPS with the expected certificate,
  security headers, CSP, and CORS behavior.
- ECS service task definitions, desired/running counts, rollout state, image
  digests, target health, and AZ placement match the release.

### Auth, authorization, and tenant safety

- Owner, admin, and member can log in and see only role-appropriate controls.
- An invited member accepts into the intended tenant and role.
- A member is denied an owner/admin action.
- A user/resource from tenant A cannot read or mutate tenant B data.
- OpenFGA interruption fails protected actions closed; enforcement remains on.

### Core product

- Chat streams a real model response and persists across refresh.
- The dedicated synthetic tenant passes the deterministic cross-industry suite
  in [`CROSS_INDUSTRY_EVALUATION.md`](CROSS_INDUSTRY_EVALUATION.md) with no
  critical privacy, tenant-isolation, prompt-injection, refusal, citation,
  calculation, escalation, or approval-boundary failures. AWS releases enforce
  this against the exact reviewed source SHA; missing evaluation credentials are
  an external launch blocker, never a skipped green gate.
- Upload/source retrieval and artifact create/version/restore work.
- Memory create/retrieve/edit/delete respects member/project scope.
- A live connector read works for the initiating member.
- A risky connector write creates an approval, resumes once after authorization,
  and does not duplicate the external action.
- Worker queue progress, task recovery, cancellation, schedules, and monitors
  behave once across multiple API tasks.
- Browser/cloud-computer, code/data sandbox, vision/image/voice, notifications,
  billing, SSO/SCIM, and audit export each receive their configured production
  smoke test.

### Signals and recovery

- Langfuse receives a trace and Sentry receives a controlled test event without
  secrets or cross-tenant metadata leakage.
- The existing Sentry project receives one controlled Next.js server error and
  one browser-render error tagged with the deployed eight-character release.
  Confirm request bodies, query strings, console breadcrumbs, cookies,
  Authorization/Cookie/CSRF headers, and user PII are absent before clearing
  either event.
- Email notification delivery works from the verified domain.
- Operations dashboard/alarms are current; SNS delivery is confirmed.
- WAF sampled counted/body rules are reviewed for false positives; a normal
  large prompt/upload is not blocked.
- Backup jobs and cross-Region copies are successful.

## Health endpoint interpretation

| Endpoint | Authentication | Meaning | Operator action |
|---|---|---|---|
| `/health/live` | None | Process is responsive; no dependency calls | ECS container liveness only |
| `/ready` | None | Postgres, Redis, S3, OpenFGA, and in production a current connector-worker heartbeat are reachable; returns 503 when any check is degraded | ALB readiness and incident gate |
| `/health` | None | Same core dependency detail but retains HTTP 200 for compatibility | Structured public/operator signal; inspect `status` and every check |
| `/health/deep` | Admin | Core checks plus a billed fast-model probe | Manual provider check; do not use as automated liveness |

Never use `/health/live` alone to declare the product healthy.

## Alarm triage

All alarms and infrastructure-failure events route to the encrypted
`chronos-prod-operations` SNS topic. A pending email subscription is a launch
blocker.

| Signal | First checks | Typical action |
|---|---|---|
| API/web target 5xx or unhealthy hosts | Deploy revision, target health, `/ready`, RDS/Redis/S3, application logs | Let circuit breaker roll back; preserve failed task logs |
| API/worker/OpenFGA log errors | Correlated trace/task/deploy, provider response, secret/version metadata | Contain affected capability; do not log secret bodies |
| ECS running tasks low | Service events, image pull, subnet/NAT capacity, task stop reasons | Restore minimum capacity; roll back bad revision |
| ECS CPU/memory high | Tenant/task load, runaway tools, provider latency, autoscaling state | Enforce budgets/cancel unsafe work; scale within reviewed limits |
| RDS CPU/storage/memory | Slow queries, connections, storage autoscaling, maintenance/events | Shed expensive work; tune/scale through reviewed change |
| Redis CPU/memory/evictions/lag/auth | `noeviction` behavior, client errors, failover/events, queue depth | Protect writes/queues; fail over or scale; reconcile durable work |
| NAT port errors | Connection churn, provider timeouts, task concurrency | Reduce concurrency and investigate connection reuse/provider outage |
| WAF block spike | Sampled requests and rule name, expected client traffic, attack indicators | Block attack or tune only the exact false-positive rule after review |
| Backup/restore failure | Job/resource/vault/KMS/IAM details | Open recovery incident; do not accept a missing daily copy |
| Budget threshold | Cost explorer by service/provider/tenant | Stop runaway workloads and obtain spend approval |

Use server-side logs for error detail. Public health responses intentionally do
not reveal internal endpoints or provider bodies.

## Rollback and emergency controls

- For a bad release without data corruption, restore the coordinated prior API,
  web, and worker task revisions and verify them. Do not downgrade schemas
  automatically.
- For data corruption, authorization inconsistency, Redis loss, or regional
  failure, follow `DISASTER_RECOVERY.md`.
- Cancel unsafe customer tasks through product controls where possible, then
  confirm durable cancellation and external-provider state.
- Temporarily reduce service concurrency or block a narrowly identified abusive
  source through reviewed AWS controls. Preserve evidence.
- Do not switch to `DEMO_MODE`, development OTP, unlimited org budgets, or
  `PERMISSIONS_ENFORCE=false` in production.

## Secret and provider incidents

1. Identify the exposed credential and affected provider/tenant scope.
2. Preserve evidence without copying the credential.
3. Revoke/rotate at the provider, update the approved Terraform secret input,
   apply, and deploy a new task revision.
4. Audit use since the earliest possible exposure and notify security/client
   owners as required.
5. Reconnect or re-encrypt affected connector credentials when rotating the
   vault key.

See `PRODUCTION_CONFIGURATION.md` for rotation sequencing. A provider key
rotation is not complete until the old key is revoked and a real capability
smoke passes.

## Application retention operations

Organization administrators manage the active-memory, deleted-memory, and
deleted-artifact grace periods in **Settings → Memory**. The same screen exposes
active legal holds and manual execution. Pinned memories, resource holds, and an
organization-wide hold are excluded from every deletion phase.
Retention execution, hold create/release, bulk memory purge, and individual
memory/artifact deletion are serialized per organization so a newly accepted
hold cannot race an irreversible cleanup.

Before changing or manually executing a client policy:

1. Confirm the approved contractual/legal retention periods and save them.
2. Run the audited dry run (`POST /settings/retention/run` with
   `{"dry_run": true}`) and review every candidate/exclusion count.
3. Add a legal hold for the organization or specific memory/artifact when legal
   review requires preservation. Holds are released, never deleted.
4. Execute only after the dry run is approved. The API requires the exact
   `RUN RETENTION` confirmation for irreversible execution.
5. Review `retention.run.evaluate` and `retention.run.complete` in the append-only
   audit log. A `partial_failure` means object deletion failed; Chronos preserves
   that artifact's metadata and retries it on a later run.

The leader-elected scheduler runs application retention daily at 05:15 UTC. Memory
first enters soft deletion and is irreversibly removed only after its separate
grace period. Artifact metadata is erased only after every distinct version
object has been deleted successfully. Audit records are never retention targets.

## Routine operating cadence

### Every day

- Review open alarms, failed tasks/deployments, queue/recovery anomalies,
  provider quota/spend, RDS/Redis health, WAF blocks, and backup/copy jobs.
- Confirm on-call route health and investigate any missing telemetry.

### Every week

- Review error/latency trends, autoscaling saturation, cost by AWS/provider,
  connector failures/revocations, suspicious auth/tenant denials, and audit
  export integrity.
- Review sampled WAF count-mode API body rules for attacks and false positives.

### Every month

- Review dependencies/container scans and apply tested updates.
- Verify the AWS Backup restore-test result and inspect the restored RDS test.
- Review IAM/GitHub/provider access, Secrets Manager rotation ages, Cognito/MFA,
  data retention/deletion jobs, and budget thresholds.
- Reconfirm owner/admin roster and operations subscription.

### Every quarter

- Run the application-level restore rehearsal in `DISASTER_RECOVERY.md`.
- Exercise incident escalation, provider outage, cross-tenant negative tests,
  and approval/idempotency recovery.
- Review capacity/load evidence and client SLA/SLO assumptions.

## Client tenant onboarding

For each real client:

1. Execute the approved contract, privacy/data-processing, retention, residency,
   support, incident-notification, and provider/subprocessor review.
2. Create the tenant through the supported onboarding flow; do not copy the
   default development organization.
3. Verify tenant DNS, invitation domain, owner/admin members, MFA/SSO/SCIM,
   role/group/workspace policy, and connector entity scope.
4. Set finite token/concurrency/provider budgets and alert ownership.
5. Connect only approved provider accounts and prove member-level isolation and
   revocation.
6. Run the tenant acceptance flow: login, chat, source/project, memory, artifact,
   connector read, approval-gated write, audit, notification, and export/delete.
7. Record evidence and obtain client/internal acceptance before importing real
   confidential data.

## Evidence and handoff

Every production change closes with:

```text
Change ID and owner:
Git SHA and image digests:
Terraform plan/apply identifiers (if any):
Migration task ARNs and exit codes:
Prior and active task revisions:
Public and authenticated smoke results:
Tenant/role negative-test evidence:
Alarm/dashboard/backup evidence:
Rollback window and retained resources:
Known exceptions, owner, and expiry:
Operator and reviewer:
```

If a required check cannot be executed, record it as a blocker. Do not convert
an unverified check into a pass based on infrastructure or test code alone.
