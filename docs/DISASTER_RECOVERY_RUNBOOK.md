# Disaster Recovery Runbook

The canonical Chronos disaster-recovery procedure is
[`DISASTER_RECOVERY.md`](DISASTER_RECOVERY.md).

This compatibility document exists because the production CloudWatch dashboard
currently links to this filename.

The guarded rehearsal entrypoint is
[`infra/plan-restore-rehearsal.sh`](../infra/plan-restore-rehearsal.sh). It is
plan-only by default, requires a distinct `restore-*` environment/backend key,
and cannot restore S3 data or promote public traffic. The read-only
[`infra/collect-restore-rehearsal-evidence.sh`](../infra/collect-restore-rehearsal-evidence.sh)
checks the applied infrastructure quarantine without exposing connection
secrets. Follow the canonical runbook for the required S3 restore, migrations,
authorization/tenant checks, Redis-loss reconciliation, evidence, and cleanup
approval.
