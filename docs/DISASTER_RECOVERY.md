# Chronos Disaster Recovery and Restore Runbook

This runbook covers production data recovery, service restoration, and
cross-Region disaster response for the Terraform-managed AWS deployment. It is
an executable procedure, not proof that a restore has succeeded. Record the
evidence from every rehearsal and incident.

## Current recovery posture

The infrastructure defines the following controls:

| Asset | Primary protection | Cross-Region protection | Automated validation |
|---|---|---|---|
| Chronos Postgres | Multi-AZ RDS, 35-day native PITR, daily AWS Backup with continuous backup | Daily AWS Backup copy retained at least 365 days | Monthly AWS Backup restore-test provisioning |
| OpenFGA Postgres | Separate Multi-AZ RDS, 35-day native PITR, daily AWS Backup with continuous backup | Daily AWS Backup copy retained at least 365 days | Monthly AWS Backup restore-test provisioning |
| Artifact S3 bucket | KMS encryption, versioning, current objects retained, noncurrent versions retained 365 days, AWS Backup | Daily AWS Backup copy retained at least 365 days | No automated S3 content/application validation |
| Redis | Multi-AZ automatic failover, `noeviction`, 35 days of native snapshots | None configured by Terraform | Alarms only; no automated restore test |
| Application/provider secrets | Secrets Manager recovery window and KMS encryption | Replicated to the backup Region | Version presence check only |
| Computed connection URLs | Recreated from provisioned endpoints in primary Terraform state | Not replicated | `infra/post-apply.sh` verifies an `AWSCURRENT` version |
| Container images | Immutable ECR tags, scan-on-push, and last ten images retained | Immutable backup-Region repositories with ECR replication | CI container build and deploy smoke tests; live replica/digest proof still required |
| Terraform state | KMS-encrypted, versioned S3; noncurrent versions retained one year | No explicit cross-Region replication | Manual state review |

AWS Backup runs at 02:00 UTC. Native RDS backup windows are 03:00-04:00 UTC
for Chronos and 01:00-02:00 UTC for OpenFGA. Redis snapshots run 04:00-05:00
UTC. These mechanisms do not establish a contractual recovery point objective
or recovery time objective by themselves.

## Known limitations that must remain visible

- The Terraform module creates a primary stack plus backup vaults and secret
  replicas; it does not create a warm application stack in the backup Region.
- Terraform accepts application-RDS, OpenFGA-RDS, and optional same-Region Redis
  snapshot inputs only behind `restore_rehearsal_mode`. That mode is a
  quarantined exercise path, not a production promotion switch. A regional
  promotion still requires incident review, separate DR state, and exact imports
  or an approved overlay after the restored resources are validated.
- Redis snapshots are not copied cross-Region. A full primary-Region loss starts
  with empty Redis unless an operator performs a separate available snapshot
  export/import and restore in the destination Region. `restore_redis_snapshot_name`
  can reference only a snapshot already visible in the rehearsal Region.
  In-flight queue/lease/idempotency state may be lost and must be reconciled
  against durable database records.
- ECR replication is declared, but no live cross-Region image/digest evidence is
  recorded here. Terraform state is not explicitly replicated cross-Region, so
  state recovery still depends on the primary bucket remaining accessible or a
  separately bootstrapped, KMS-encrypted, versioned destination bucket. The
  rehearsal script refuses the production state key but does not create that
  missing backend.
- Terraform creates an empty versioned artifact bucket for the rehearsal. AWS
  Backup S3 restore is a separate, potentially expensive job. Cross-Region S3
  copies restore only in the Region containing the copy, and copied recovery
  points do not preserve continuous PITR semantics.
- Automated restore testing covers RDS provisioning only. It does not validate
  pgvector, Alembic revision, OpenFGA authorization behavior, tenant isolation,
  S3 artifact bytes, Redis recovery, or end-to-end application behavior.
- No measured client-approved RTO/RPO evidence is recorded in the repository.

Do not describe cross-Region recovery as turnkey or tested until those gaps are
closed by a full rehearsal.

## Recovery priorities

1. Protect people and credentials; contain an active security incident.
2. Freeze deploys, Terraform applies, destructive jobs, retention jobs, and
   provider rotations.
3. Preserve logs, state versions, database recovery points, S3 versions, task
   revisions, image digests, DNS answers, and incident timestamps.
4. Restore authorization and tenant boundaries before reopening client access.
5. Restore the Chronos database and artifact store to one consistent point.
6. Reconcile Redis-backed coordination/queues from durable records.
7. Validate with a quarantined tenant and negative cross-tenant tests.
8. Shift traffic gradually, monitor, and retain the damaged environment for
   investigation until the incident owner releases it.

## Roles

| Role | Responsibility |
|---|---|
| Incident commander | Declares severity, owns decisions/timeline, approves traffic changes |
| Infrastructure operator | AWS Backup, RDS, S3, Redis, ECS, Terraform, DNS |
| Application operator | Migrations, task reconciliation, tenant/data validation |
| Security lead | Credential compromise, evidence retention, tenant-boundary approval |
| Client communications owner | Status and contractual notifications |
| Scribe | Exact timestamps, commands, resource IDs, evidence, and approvals |

The infrastructure operator and security/application reviewer must be different
people for state restoration, destructive cleanup, and DNS cutover.

## Immediate incident procedure

1. Open an incident record and record detection time, symptom, affected tenants,
   last known-good release, and last known-good data time.
2. Disable the GitHub deploy workflow and freeze Terraform applies.
3. If writes can worsen corruption, remove public traffic or set affected
   services to a controlled maintenance response. Do not destroy the source.
4. Capture ECS service/task definitions, running image digests, CloudWatch
   alarms/log pointers, RDS events, Backup job IDs, S3 version IDs, Redis events,
   Terraform state object version, DNS answers, and Cognito/provider status.
5. Decide whether this is a release rollback, dependency outage, data restore,
   credential incident, or regional disaster. Follow the narrowest applicable
   recovery path below.

## Quarantined full-application restore rehearsal

Use [`infra/plan-restore-rehearsal.sh`](../infra/plan-restore-rehearsal.sh) for
quarterly and pre-release restore exercises. Its default action is read-only
preflight plus `terraform plan`; it does not apply resources, restore S3, change
DNS, or start application tasks. The Terraform guard enforces all of the
following:

- a distinct `restore-<exercise>` environment and `rehearsals/...` state key;
- both Chronos and OpenFGA encrypted PostgreSQL snapshot seeds;
- internal ALBs, with no ingress unless restricted operator CIDRs are supplied;
- dev OTP plus demo connectors, with every external provider/auth/email/billing/
  observability credential rejected, so a rehearsal cannot contact clients or
  perform provider writes;
- no production domain/certificate, WAF, account-wide security services, or new
  backup plan in the short-lived stack; and
- zero ECS tasks in the initial bootstrap phase.

Before the first exercise in a Region, create and separately approve a
KMS-encrypted, versioned state bucket there. `infra/bootstrap.sh` can create the
bucket, but doing so changes AWS and is not part of the read-only rehearsal
plan. Never reuse `prod/terraform.tfstate`, and never store the generated plan or
tfvars in the repository; both contain sensitive values.

Example read-only plan:

```bash
EXERCISE_ID=20260712 \
APP_DB_SNAPSHOT_IDENTIFIER=<available-encrypted-rds-snapshot> \
OPENFGA_DB_SNAPSHOT_IDENTIFIER=<available-encrypted-openfga-snapshot> \
AWS_REGION=us-west-2 AWS_PROFILE=<approved-profile> \
bash infra/plan-restore-rehearsal.sh
```

The script verifies the caller/account, protected backend, snapshot availability,
encryption, KMS access, PostgreSQL major version, master/database names, and—if
provided—same-Region Redis snapshot status. It then asserts in plan JSON that
both ALBs are internal, no `0.0.0.0/0` ingress exists, the selected RDS snapshots
are creation seeds, and all ECS desired counts are zero.

Applying the reviewed plan is cost-bearing and requires both `ACTION=apply` and
the exact `CONFIRM_RESTORE_APPLY=apply-restore-<exercise>` acknowledgement. This
is only the infrastructure/bootstrap phase. After it completes:

1. Use the selected S3 recovery point in the same Region to restore all required
   versions into the new Terraform output `s3_artifacts_bucket`. Keep Block
   Public Access enabled, restore without ACLs unless the recovery point included
   them, and use the rehearsal KMS key. Record the restore job ID and every
   partial-object failure; a `COMPLETED` job can still report skipped objects.
2. Compare the restored object inventory/time with database references before
   running code. A database-only restore is incomplete.
3. Verify the exact release image digests in the Region, push them to the
   rehearsal ECR repositories without mutable tags, and run OpenFGA then Chronos
   migrations exactly once. Preserve the pre-migration RDS restores.
4. If Redis was not restored, keep writes closed and perform the durable-work
   reconciliation below. Never infer completion of external actions from an
   empty queue.
5. Re-run the same script with `START_SERVICES=true`, immutable image tags, and
   restricted private `RESTORE_INGRESS_CIDRS`. Review and apply the new plan.
6. Run the read-only
   [`infra/collect-restore-rehearsal-evidence.sh`](../infra/collect-restore-rehearsal-evidence.sh)
   first with `EXPECT_SERVICES=stopped`, then after service start with
   `EXPECT_SERVICES=running`. It records only resource/security summaries and
   never connection secrets. Set `EXPECTED_MIN_ARTIFACT_KEYS` to a reviewed
   nonzero floor when the selected recovery point contains artifacts.
7. Complete every post-restore check from a private operator path. Do not add
   public DNS, real provider keys, or Cognito clients to a rehearsal stack.
8. Record measured RTO/RPO and evidence. Destruction is a separate approved
   action after retention/legal/security review.

The RDS and Redis snapshot arguments are creation-only seeds and are ignored for
later drift. Keep the original values recorded and pinned. Changing an exercise
to a different recovery point requires a new exercise ID and new state; it must
not replace a validated database in place.

## Release rollback without data restore

Use this path only when migrations are backward compatible and data is not
corrupt.

1. Identify the last healthy API, web, and worker task-definition ARNs and image
   digests.
2. Review migrations introduced by the failed release. Never run an Alembic
   downgrade automatically in production.
3. Update all three ECS services to their matching prior revisions. The deploy
   workflow performs this rollback automatically when a deployment step fails,
   but operators must verify the resulting active revisions.
4. Wait for service stability and verify desired/running counts and completed
   rollouts.
5. Run public `/health`, `/auth/config?tenant=<smoke>`, web/tenant login, CORS,
   and authenticated core-flow smoke tests.
6. Confirm worker progress and OpenFGA checks before reopening traffic.

## Application database point-in-time restore

Always restore into a new isolated RDS instance. Never overwrite the source
instance during diagnosis.

1. Select the recovery time using the incident timeline, audit records, and S3
   version timestamps. Record why it is consistent enough for the business.
2. Restore the Chronos RDS instance to a new identifier, private subnets, the
   restore-test security group or a dedicated quarantined group, and the same
   encrypted storage posture.
3. Do not point production tasks at it. Connect only through the approved private
   operator path.
4. Verify PostgreSQL version, pgvector extension, schema/Alembic head, row counts,
   tenant IDs, immutable audit-log constraints, and representative records.
5. Restore/reconcile S3 objects to the corresponding point. A database row that
   references a missing or later object is not a successful restore.
6. Register a quarantined migration task revision with a temporary connection
   secret if forward migrations are required. Preserve the pre-migration restore.
7. Validate at least two tenants, two member roles, projects, conversations,
   memory authorization, artifacts and versions, approvals, scheduled work, and
   cross-tenant negative access.
8. After security/application approval, create a new production connection
   secret version and deploy coordinated API/worker revisions. Keep the old
   instance and secret version until the rollback window closes.

If only a subset of rows is corrupt, prefer an audited logical repair from a
quarantined restore over a full cutover. Every repair must retain tenant IDs,
creator/member ownership, and audit evidence.

## OpenFGA datastore restore

OpenFGA authorization data is a security control, not a disposable cache.

1. Restore the OpenFGA RDS recovery point into an isolated instance from a time
   consistent with the Chronos database.
2. Start a quarantined pinned OpenFGA revision against the restored datastore.
3. Verify its datastore migration version and model/store resolution.
4. Compare representative owner/admin/member relations with the corresponding
   Chronos members, projects, workspaces, tasks, and artifacts.
5. Prove allowed and denied checks, including a cross-tenant denial and an
   ordinary member denied an owner/admin action.
6. Cut over the private OpenFGA connection only with security approval. The
   permission seam must fail closed while OpenFGA is unavailable; never disable
   `PERMISSIONS_ENFORCE` to make the application appear healthy.

## Artifact S3 recovery

1. Restore into a new quarantined bucket/prefix whenever possible; do not bulk
   overwrite the production bucket first.
2. Preserve version IDs, KMS access, object metadata, tenant prefixes, and legal
   holds/retention requirements.
3. Compare object inventory with database artifact/source/attachment records.
4. Open representative current and historical artifact versions for multiple
   tenants. Verify hashes or byte size/content where a canonical hash is absent.
5. Confirm presigned access cannot cross tenant/member scope.
6. Only then copy the approved object versions to the production location or
   cut over through a reviewed configuration change.

Glacier Instant Retrieval transitions and noncurrent version retention can make
bulk recovery slow or costly. Estimate and approve the restore before starting
a large operation.

## Redis recovery and durable-work reconciliation

Redis contains cache, pub/sub, connector queues, leases, scheduler leadership,
rate limits, loop/idempotency state, and other coordination data. Treat an empty
Redis start as a controlled partial-data-loss event even when PostgreSQL and S3
are intact.

1. Prefer native failover for an instance/AZ failure.
2. For corruption, restore a snapshot into a new replication group and inspect
   it before cutover.
3. For a regional disaster with no available Redis copy, start a clean encrypted
   HA cluster with `noeviction` and keep client traffic closed.
4. Run the application recovery/reaper paths once under leader election.
5. Reconcile every durable task/workflow/research/connector execution status;
   requeue only work proven incomplete and safe to retry.
6. Expire or rebuild caches. Do not synthesize approvals or mark external writes
   complete without provider evidence.
7. Prove idempotency for a representative risky action before reopening writes.

## Full primary-Region loss

This is a manual promotion with no warm-stack claim.

1. Confirm the backup Region, newest completed cross-Region recovery points,
   replicated secret versions, available Git commit, and DNS/ACM readiness.
2. Create a distinct DR backend key such as `dr/terraform.tfstate`; never reuse
   `prod/terraform.tfstate` for a second Region.
3. Prepare an incident-reviewed Terraform change/overlay for the backup Region,
   regional availability zones, new certificates, restored RDS identifiers,
   restored/new S3 and Redis resources, and region-specific secret URLs.
4. Restore Chronos RDS, OpenFGA RDS, and artifacts from the selected cross-Region
   recovery points. Import restored objects into the DR state only after exact
   identity review.
5. Verify the immutable API/web images for the exact approved Git SHA exist in
   backup-Region ECR and that their digests match primary evidence. If a replica
   is missing, rebuild from the exact commit under a new incident-approved
   immutable release tag; never overwrite a tag or deploy `latest`.
6. Run OpenFGA and Chronos migrations once, using the one-off task pattern.
7. Start services without public traffic and complete database, authorization,
   artifact, Redis-reconciliation, health, and tenant-isolation validation.
8. Issue/validate regional certificates and lower DNS TTL only through the
   approved incident change. Update API, app, and wildcard tenant records
   together after all smoke tests pass.
9. Monitor error rates, latency, authentication, queues, provider calls, and
   tenant reports. Increase traffic gradually where the DNS/provider permits.

Do not rotate replicated database/vault/provider secrets during promotion unless
the incident is a credential compromise. A simultaneous data restore and broad
credential rotation makes failure attribution and rollback substantially harder.

## Post-restore validation gate

All checks must pass before client traffic resumes:

- `/health/live` returns `ok`; `/ready` returns HTTP 200 with Postgres, Redis,
  S3, OpenFGA, and a current connector-worker heartbeat `ok`; public `/health`
  is not degraded.
- Auth config reports Cognito only, development OTP is disabled, and real
  login/logout/recovery works on the app and a tenant subdomain.
- OpenFGA allows representative authorized actions and denies representative
  role/cross-tenant actions.
- Database is at the expected Alembic head; no migration task is still running.
- Representative artifacts and historical versions open with correct tenant
  access.
- Worker queues advance; interrupted work is reconciled once without duplicate
  external writes.
- Schedules/monitors have one leader and do not duplicate executions.
- A real model turn, web/browser flow, connector read, approval-gated write,
  email notification, observability event, and billing webhook follow expected
  paths.
- CloudWatch alarms/dashboard, SNS/paging, WAF, Backup jobs, and cost controls
  are active in the restored environment.
- The full behavioral E2E and frontend browser/device audit pass against the
  restored release.

## Rehearsal schedule and evidence

| Frequency | Required exercise |
|---|---|
| Daily | Verify latest backup jobs and cross-Region copies; investigate every failure |
| Monthly | Review AWS RDS restore-test result and validate the restored instance before automatic deletion |
| Quarterly | Quarantined application DB + OpenFGA + representative S3 restore, migrations, tenant/access validation, and Redis-loss reconciliation |
| Semiannual | Full backup-Region promotion rehearsal including image rebuild, DNS plan, provider/auth smoke, and failback plan |
| After major schema/storage change | Targeted restore and forward-migration rehearsal before release |

Record measured timings instead of estimating them:

```text
Exercise/incident ID:
Scenario:
Source release and image digests:
Selected recovery point/time:
Declared start:
Data restored:
Services internally ready:
Validation complete:
Traffic restored:
Observed data gap:
Observed RTO:
Observed RPO:
Failed checks and remediation owners:
Approvers:
Evidence locations:
```

## Failback and cleanup

Failback is a second migration and requires its own plan. Reconcile writes made
in the recovery environment, establish a new consistent restore/replication
point, validate the target, and shift traffic only after approval.

Keep damaged and restored sources until legal/security/incident owners approve
cleanup. Deleting temporary RDS instances, restored buckets, snapshots, state
copies, or logs is a separately reviewed destructive action.
