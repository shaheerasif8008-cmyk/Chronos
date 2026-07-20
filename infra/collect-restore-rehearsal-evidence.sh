#!/usr/bin/env bash
# Read-only infrastructure evidence for an applied restore rehearsal. Application
# and tenant validation still must run from the approved private operator path.
set -euo pipefail

if [[ -d /opt/homebrew/bin ]]; then
  export PATH="/opt/homebrew/bin:$PATH"
fi

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for command in aws jq; do
  command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
done

EXERCISE_ID="${EXERCISE_ID:-}"
[[ "$EXERCISE_ID" =~ ^[a-z0-9]{4,8}$ ]] || die "EXERCISE_ID must be 4-8 lowercase letters/digits"

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-us-west-2}"
EXPECT_SERVICES="${EXPECT_SERVICES:-stopped}"
EXPECTED_MIN_ARTIFACT_KEYS="${EXPECTED_MIN_ARTIFACT_KEYS:-0}"
[[ "$EXPECT_SERVICES" == "stopped" || "$EXPECT_SERVICES" == "running" ]] || die "EXPECT_SERVICES must be stopped or running"
[[ "$EXPECTED_MIN_ARTIFACT_KEYS" =~ ^[0-9]+$ ]] || die "EXPECTED_MIN_ARTIFACT_KEYS must be a non-negative integer"
(( EXPECTED_MIN_ARTIFACT_KEYS <= 1000 )) || die "EXPECTED_MIN_ARTIFACT_KEYS cannot exceed the 1000-key evidence sample"

ACCOUNT_ID=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || die "could not resolve AWS account"
PREFIX="chronos-restore-${EXERCISE_ID}"
ARTIFACT_BUCKET="${PREFIX}-artifacts-${ACCOUNT_ID}"
EVIDENCE_FILE="${RESTORE_EVIDENCE_FILE:-${TMPDIR:-/tmp}/${PREFIX}-evidence.json}"

APP_DB_JSON=$(aws rds describe-db-instances --db-instance-identifier "${PREFIX}-postgres" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
FGA_DB_JSON=$(aws rds describe-db-instances --db-instance-identifier "${PREFIX}-openfga-postgres" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)

for item in "$APP_DB_JSON" "$FGA_DB_JSON"; do
  jq -e '
    .DBInstances | length == 1 and
    .[0].DBInstanceStatus == "available" and
    .[0].Engine == "postgres" and
    .[0].StorageEncrypted == true and
    .[0].PubliclyAccessible == false and
    .[0].DeletionProtection == true
  ' <<<"$item" >/dev/null || die "an RDS restore is absent, unavailable, public, unencrypted, or lacks deletion protection"
done

REDIS_JSON=$(aws elasticache describe-replication-groups --replication-group-id "${PREFIX}-redis" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
jq -e '
  .ReplicationGroups | length == 1 and
  .[0].Status == "available" and
  .[0].AtRestEncryptionEnabled == true and
  .[0].TransitEncryptionEnabled == true and
  .[0].AuthTokenEnabled == true
' <<<"$REDIS_JSON" >/dev/null || die "Redis rehearsal group is absent, unavailable, or missing encryption/auth"

API_ALB_JSON=$(aws elbv2 describe-load-balancers --names "${PREFIX}-api-alb" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
WEB_ALB_JSON=$(aws elbv2 describe-load-balancers --names "${PREFIX}-web-alb" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
for item in "$API_ALB_JSON" "$WEB_ALB_JSON"; do
  jq -e '.LoadBalancers | length == 1 and .[0].Scheme == "internal" and .[0].State.Code == "active"' \
    <<<"$item" >/dev/null || die "a rehearsal ALB is absent, non-internal, or inactive"
done

ALB_SG_JSON=$(aws ec2 describe-security-groups \
  --filters "Name=group-name,Values=${PREFIX}-alb-sg" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
jq -e '
  .SecurityGroups | length == 1 and
  ([.SecurityGroups[0].IpPermissions[].IpRanges[]?.CidrIp] | index("0.0.0.0/0") == null) and
  ([.SecurityGroups[0].IpPermissions[].Ipv6Ranges[]?.CidrIpv6] | index("::/0") == null)
' <<<"$ALB_SG_JSON" >/dev/null || die "rehearsal ALB security group is missing or public"

ECS_JSON=$(aws ecs describe-services --cluster "${PREFIX}-cluster" \
  --services "${PREFIX}-api" "${PREFIX}-web" "${PREFIX}-worker" "${PREFIX}-openfga" \
  --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
jq -e '.failures | length == 0 and .services | length == 4' <<<"$ECS_JSON" >/dev/null \
  || die "one or more rehearsal ECS services are missing"
if [[ "$EXPECT_SERVICES" == "stopped" ]]; then
  jq -e '.services | all(.desiredCount == 0 and .runningCount == 0 and .pendingCount == 0)' \
    <<<"$ECS_JSON" >/dev/null || die "bootstrap evidence expected all services stopped"
else
  jq -e '.services | all(.desiredCount > 0 and .runningCount == .desiredCount and .pendingCount == 0)' \
    <<<"$ECS_JSON" >/dev/null || die "service-start evidence expected all services stable at desired count"
fi

[[ $(aws s3api get-bucket-versioning --bucket "$ARTIFACT_BUCKET" --profile "$AWS_PROFILE" --query Status --output text) == "Enabled" ]] \
  || die "artifact rehearsal bucket versioning is not enabled"
[[ $(aws s3api get-bucket-encryption --bucket "$ARTIFACT_BUCKET" --profile "$AWS_PROFILE" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' --output text) == "aws:kms" ]] \
  || die "artifact rehearsal bucket is not KMS encrypted"
PUBLIC_BLOCK=$(aws s3api get-public-access-block --bucket "$ARTIFACT_BUCKET" --profile "$AWS_PROFILE" \
  --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' --output text)
[[ "$PUBLIC_BLOCK" == $'True\tTrue\tTrue\tTrue' ]] || die "artifact rehearsal bucket does not block all public access"
ARTIFACT_KEY_COUNT=$(aws s3api list-objects-v2 --bucket "$ARTIFACT_BUCKET" --max-keys 1000 \
  --profile "$AWS_PROFILE" --query KeyCount --output text)
(( ARTIFACT_KEY_COUNT >= EXPECTED_MIN_ARTIFACT_KEYS )) \
  || die "artifact bucket has $ARTIFACT_KEY_COUNT keys, below required minimum $EXPECTED_MIN_ARTIFACT_KEYS"

mkdir -p "$(dirname "$EVIDENCE_FILE")"
umask 077
jq -n \
  --arg collected_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg account_id "$ACCOUNT_ID" \
  --arg region "$AWS_REGION" \
  --arg exercise_id "$EXERCISE_ID" \
  --arg artifact_bucket "$ARTIFACT_BUCKET" \
  --argjson artifact_key_count "$ARTIFACT_KEY_COUNT" \
  --argjson app_db "$(jq '.DBInstances[0] | {DBInstanceIdentifier, DBInstanceStatus, Engine, EngineVersion, StorageEncrypted, PubliclyAccessible, MultiAZ, DeletionProtection}' <<<"$APP_DB_JSON")" \
  --argjson openfga_db "$(jq '.DBInstances[0] | {DBInstanceIdentifier, DBInstanceStatus, Engine, EngineVersion, StorageEncrypted, PubliclyAccessible, MultiAZ, DeletionProtection}' <<<"$FGA_DB_JSON")" \
  --argjson redis "$(jq '.ReplicationGroups[0] | {ReplicationGroupId, Status, AtRestEncryptionEnabled, TransitEncryptionEnabled, AuthTokenEnabled, AutomaticFailover}' <<<"$REDIS_JSON")" \
  --argjson services "$(jq '[.services[] | {serviceName, desiredCount, runningCount, pendingCount, taskDefinition}]' <<<"$ECS_JSON")" \
  '{
    collected_at: $collected_at,
    account_id: $account_id,
    region: $region,
    exercise_id: $exercise_id,
    quarantine: {alb_scheme: "internal", public_ingress: false},
    app_db: $app_db,
    openfga_db: $openfga_db,
    redis: $redis,
    services: $services,
    artifacts: {bucket: $artifact_bucket, key_count_first_1000: $artifact_key_count, versioning: "Enabled", encryption: "aws:kms", public_access_blocked: true}
  }' >"$EVIDENCE_FILE"

printf 'Read-only infrastructure evidence passed: %s\n' "$EVIDENCE_FILE"
printf 'Still required: S3 version/reference reconciliation, migrations, private health/auth/OpenFGA/tenant tests, durable-work reconciliation, provider isolation, and measured RTO/RPO.\n'
