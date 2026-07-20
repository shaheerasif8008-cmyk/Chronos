#!/usr/bin/env bash
# Plan (and, only with an explicit cost acknowledgement, apply) a quarantined
# Chronos restore rehearsal in a separate state key. This script never restores
# S3 objects, changes DNS, or promotes client traffic.
set -euo pipefail

# Homebrew is outside PATH in some non-interactive operator shells on macOS.
if [[ -d /opt/homebrew/bin ]]; then
  export PATH="/opt/homebrew/bin:$PATH"
fi

usage() {
  cat <<'EOF'
Usage:
  EXERCISE_ID=20260712 \
  APP_DB_SNAPSHOT_IDENTIFIER=<rds-snapshot-id-or-arn> \
  OPENFGA_DB_SNAPSHOT_IDENTIFIER=<rds-snapshot-id-or-arn> \
  AWS_REGION=us-west-2 AWS_PROFILE=<profile> \
  bash infra/plan-restore-rehearsal.sh

Required:
  EXERCISE_ID                         4-8 lowercase letters/digits
  APP_DB_SNAPSHOT_IDENTIFIER          available encrypted PostgreSQL snapshot
  OPENFGA_DB_SNAPSHOT_IDENTIFIER      available encrypted PostgreSQL snapshot

Optional:
  REDIS_SNAPSHOT_NAME                 available snapshot in AWS_REGION only
  APP_DB_USERNAME / APP_DB_NAME       source values (defaults: chronos/chronos)
  OPENFGA_DB_USERNAME / OPENFGA_DB_NAME
                                      source values (defaults: openfga/openfga)
  RESTORE_STATE_BUCKET                pre-created KMS-encrypted versioned bucket
                                      (default: chronos-terraform-state-ACCOUNT-REGION)
  RESTORE_STATE_KEY                   distinct non-production key
                                      (default: rehearsals/EXERCISE_ID/terraform.tfstate)
  RESTORE_INGRESS_CIDRS               comma-separated private operator CIDRs;
                                      empty keeps both internal ALBs unreachable
  START_SERVICES=false                true only after S3 restore, migrations,
                                      image digest verification, and review
  API_IMAGE_TAG / WEB_IMAGE_TAG       immutable release tags for service start
  ACTION=plan                         set apply only with confirmation below
  CONFIRM_RESTORE_APPLY=apply-restore-EXERCISE_ID
  RESTORE_WORK_DIR                    protected directory for the sensitive plan

The plan and generated tfvars contain secrets. Keep the work directory mode 700,
never copy it into the repository, and delete it only after evidence retention
and cleanup approval.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for command in aws jq openssl terraform; do
  command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
done

EXERCISE_ID="${EXERCISE_ID:-}"
APP_DB_SNAPSHOT_IDENTIFIER="${APP_DB_SNAPSHOT_IDENTIFIER:-}"
OPENFGA_DB_SNAPSHOT_IDENTIFIER="${OPENFGA_DB_SNAPSHOT_IDENTIFIER:-}"
[[ "$EXERCISE_ID" =~ ^[a-z0-9]{4,8}$ ]] || { usage; die "EXERCISE_ID must be 4-8 lowercase letters/digits"; }
[[ -n "$APP_DB_SNAPSHOT_IDENTIFIER" ]] || { usage; die "APP_DB_SNAPSHOT_IDENTIFIER is required"; }
[[ -n "$OPENFGA_DB_SNAPSHOT_IDENTIFIER" ]] || { usage; die "OPENFGA_DB_SNAPSHOT_IDENTIFIER is required"; }

AWS_PROFILE="${AWS_PROFILE:-default}"
AWS_REGION="${AWS_REGION:-us-west-2}"
ACTION="${ACTION:-plan}"
START_SERVICES="${START_SERVICES:-false}"
REDIS_SNAPSHOT_NAME="${REDIS_SNAPSHOT_NAME:-}"
RESTORE_INGRESS_CIDRS="${RESTORE_INGRESS_CIDRS:-}"
DB_ENGINE_MAJOR="${DB_ENGINE_MAJOR:-15}"
APP_DB_USERNAME="${APP_DB_USERNAME:-chronos}"
APP_DB_NAME="${APP_DB_NAME:-chronos}"
OPENFGA_DB_USERNAME="${OPENFGA_DB_USERNAME:-openfga}"
OPENFGA_DB_NAME="${OPENFGA_DB_NAME:-openfga}"
API_IMAGE_TAG="${API_IMAGE_TAG:-restore-${EXERCISE_ID}}"
WEB_IMAGE_TAG="${WEB_IMAGE_TAG:-restore-${EXERCISE_ID}}"

[[ "$ACTION" == "plan" || "$ACTION" == "apply" ]] || die "ACTION must be plan or apply"
[[ "$START_SERVICES" == "true" || "$START_SERVICES" == "false" ]] || die "START_SERVICES must be true or false"

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
INFRA_DIR="$ROOT_DIR/infra"
ACCOUNT_ID=$(aws sts get-caller-identity --profile "$AWS_PROFILE" --query Account --output text)
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || die "could not resolve a 12-digit AWS account ID"

RESTORE_STATE_BUCKET="${RESTORE_STATE_BUCKET:-chronos-terraform-state-${ACCOUNT_ID}-${AWS_REGION}}"
RESTORE_STATE_KEY="${RESTORE_STATE_KEY:-rehearsals/${EXERCISE_ID}/terraform.tfstate}"
[[ "$RESTORE_STATE_KEY" == rehearsals/* ]] || die "RESTORE_STATE_KEY must be under rehearsals/"
[[ "$RESTORE_STATE_KEY" != "prod/terraform.tfstate" ]] || die "the production backend key is forbidden"

RESTORE_WORK_DIR="${RESTORE_WORK_DIR:-${TMPDIR:-/tmp}/chronos-restore-${EXERCISE_ID}}"
mkdir -p "$RESTORE_WORK_DIR"
chmod 700 "$RESTORE_WORK_DIR"
umask 077

TF_DATA_DIR="$RESTORE_WORK_DIR/terraform-data"
TFVARS_FILE="$RESTORE_WORK_DIR/restore.auto.tfvars.json"
PLAN_FILE="$RESTORE_WORK_DIR/restore.plan"
PLAN_JSON="$RESTORE_WORK_DIR/restore.plan.json"
mkdir -p "$TF_DATA_DIR"

printf 'Preflight account=%s region=%s exercise=%s\n' "$ACCOUNT_ID" "$AWS_REGION" "$EXERCISE_ID"

aws s3api head-bucket --bucket "$RESTORE_STATE_BUCKET" --profile "$AWS_PROFILE" >/dev/null 2>&1 \
  || die "state bucket $RESTORE_STATE_BUCKET is missing or inaccessible; create the independent protected backend before the incident"
STATE_VERSIONING=$(aws s3api get-bucket-versioning --bucket "$RESTORE_STATE_BUCKET" --profile "$AWS_PROFILE" --query Status --output text)
[[ "$STATE_VERSIONING" == "Enabled" ]] || die "state bucket versioning must be Enabled"
STATE_REGION=$(aws s3api get-bucket-location --bucket "$RESTORE_STATE_BUCKET" --profile "$AWS_PROFILE" --query LocationConstraint --output text)
[[ "$STATE_REGION" == "None" ]] && STATE_REGION="us-east-1"
[[ "$STATE_REGION" == "$AWS_REGION" ]] || die "state bucket is in $STATE_REGION, not rehearsal Region $AWS_REGION"
STATE_ENCRYPTION=$(aws s3api get-bucket-encryption --bucket "$RESTORE_STATE_BUCKET" --profile "$AWS_PROFILE" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' --output text)
[[ "$STATE_ENCRYPTION" == "aws:kms" ]] || die "state bucket must use aws:kms default encryption"
STATE_PUBLIC_BLOCK=$(aws s3api get-public-access-block --bucket "$RESTORE_STATE_BUCKET" --profile "$AWS_PROFILE" \
  --query 'PublicAccessBlockConfiguration.[BlockPublicAcls,IgnorePublicAcls,BlockPublicPolicy,RestrictPublicBuckets]' --output text)
[[ "$STATE_PUBLIC_BLOCK" == $'True\tTrue\tTrue\tTrue' ]] || die "state bucket must enable all four S3 Block Public Access controls"

check_rds_snapshot() {
  local label="$1"
  local identifier="$2"
  local expected_user="$3"
  local expected_db="$4"
  local snapshot_json status encrypted engine version username db_name kms_key key_state

  snapshot_json=$(aws rds describe-db-snapshots \
    --db-snapshot-identifier "$identifier" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
  [[ $(jq '.DBSnapshots | length' <<<"$snapshot_json") -eq 1 ]] || die "$label snapshot did not resolve uniquely"
  status=$(jq -r '.DBSnapshots[0].Status' <<<"$snapshot_json")
  encrypted=$(jq -r '.DBSnapshots[0].Encrypted' <<<"$snapshot_json")
  engine=$(jq -r '.DBSnapshots[0].Engine' <<<"$snapshot_json")
  version=$(jq -r '.DBSnapshots[0].EngineVersion' <<<"$snapshot_json")
  username=$(jq -r '.DBSnapshots[0].MasterUsername' <<<"$snapshot_json")
  db_name=$(jq -r '.DBSnapshots[0].DBName // empty' <<<"$snapshot_json")
  kms_key=$(jq -r '.DBSnapshots[0].KmsKeyId // empty' <<<"$snapshot_json")

  [[ "$status" == "available" ]] || die "$label snapshot status is $status, not available"
  [[ "$encrypted" == "true" ]] || die "$label snapshot must be encrypted; copy it under an approved KMS key first"
  [[ "$engine" == "postgres" ]] || die "$label snapshot engine is $engine, not postgres"
  [[ "${version%%.*}" == "$DB_ENGINE_MAJOR" ]] || die "$label snapshot engine major $version does not match configured $DB_ENGINE_MAJOR"
  [[ "$username" == "$expected_user" ]] || die "$label master username $username does not match configured $expected_user"
  if [[ -n "$db_name" && "$db_name" != "$expected_db" ]]; then
    die "$label database name $db_name does not match configured $expected_db"
  fi
  [[ -n "$kms_key" ]] || die "$label encrypted snapshot has no visible KMS key"
  key_state=$(aws kms describe-key --key-id "$kms_key" --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    --query KeyMetadata.KeyState --output text) \
    || die "$label snapshot KMS key is not visible to this operator in $AWS_REGION"
  [[ "$key_state" == "Enabled" ]] || die "$label snapshot KMS key state is $key_state, not Enabled"
  printf '  %s snapshot: available, encrypted, postgres %s, user=%s, db=%s\n' \
    "$label" "$version" "$username" "${db_name:-$expected_db (not reported)}"
}

check_rds_snapshot "application" "$APP_DB_SNAPSHOT_IDENTIFIER" "$APP_DB_USERNAME" "$APP_DB_NAME"
check_rds_snapshot "OpenFGA" "$OPENFGA_DB_SNAPSHOT_IDENTIFIER" "$OPENFGA_DB_USERNAME" "$OPENFGA_DB_NAME"

if [[ -n "$REDIS_SNAPSHOT_NAME" ]]; then
  REDIS_JSON=$(aws elasticache describe-snapshots --snapshot-name "$REDIS_SNAPSHOT_NAME" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" --output json)
  [[ $(jq '.Snapshots | length' <<<"$REDIS_JSON") -eq 1 ]] || die "Redis snapshot did not resolve uniquely in $AWS_REGION"
  [[ $(jq -r '.Snapshots[0].SnapshotStatus' <<<"$REDIS_JSON") == "available" ]] || die "Redis snapshot is not available"
  printf '  Redis snapshot: available in %s\n' "$AWS_REGION"
else
  printf '  Redis snapshot: none; rehearsal will start with empty Redis and must run durable-work reconciliation\n'
fi

AZ_JSON=$(aws ec2 describe-availability-zones --region "$AWS_REGION" --profile "$AWS_PROFILE" \
  --filters Name=state,Values=available --query 'AvailabilityZones[0:2].ZoneName' --output json)
[[ $(jq 'length' <<<"$AZ_JSON") -ge 2 ]] || die "at least two available Availability Zones are required"

INGRESS_JSON=$(jq -Rn --arg value "$RESTORE_INGRESS_CIDRS" \
  '$value | split(",") | map(gsub("^[[:space:]]+|[[:space:]]+$"; "")) | map(select(length > 0))')
if jq -e 'index("0.0.0.0/0") != null' <<<"$INGRESS_JSON" >/dev/null; then
  die "RESTORE_INGRESS_CIDRS cannot contain 0.0.0.0/0"
fi

STATE_ALREADY_EXISTS=false
if aws s3api head-object --bucket "$RESTORE_STATE_BUCKET" --key "$RESTORE_STATE_KEY" --profile "$AWS_PROFILE" >/dev/null 2>&1; then
  STATE_ALREADY_EXISTS=true
fi

if [[ ! -f "$TFVARS_FILE" ]]; then
  [[ "$STATE_ALREADY_EXISTS" == "false" ]] \
    || die "rehearsal state already exists but its protected tfvars is missing; recover the original tfvars or use a new EXERCISE_ID instead of rotating credentials implicitly"
  JWT_SECRET=$(openssl rand -hex 32)
  VAULT_KEY=$(openssl rand -hex 32)
  BACKUP_COPY_REGION=$([[ "$AWS_REGION" == "us-east-1" ]] && printf 'us-west-2' || printf 'us-east-1')

  jq -n \
    --arg aws_region "$AWS_REGION" \
    --arg environment "restore-${EXERCISE_ID}" \
    --arg jwt_secret "$JWT_SECRET" \
    --arg vault_key "$VAULT_KEY" \
    --arg app_snapshot "$APP_DB_SNAPSHOT_IDENTIFIER" \
    --arg fga_snapshot "$OPENFGA_DB_SNAPSHOT_IDENTIFIER" \
    --arg app_db_username "$APP_DB_USERNAME" \
    --arg app_db_name "$APP_DB_NAME" \
    --arg openfga_db_username "$OPENFGA_DB_USERNAME" \
    --arg openfga_db_name "$OPENFGA_DB_NAME" \
    --arg redis_snapshot "$REDIS_SNAPSHOT_NAME" \
    --arg backup_copy_region "$BACKUP_COPY_REGION" \
    --arg api_image_tag "$API_IMAGE_TAG" \
    --arg web_image_tag "$WEB_IMAGE_TAG" \
    --argjson availability_zones "$AZ_JSON" \
    --argjson ingress_cidrs "$INGRESS_JSON" \
    --argjson platform_bootstrap_mode "$([[ "$START_SERVICES" == "true" ]] && printf false || printf true)" \
    '{
      aws_region: $aws_region,
      environment: $environment,
      availability_zones: $availability_zones,
      domain_name: "",
      web_domain_name: "",
      api_domain_name: "",
      acm_certificate_arn: "",
      web_acm_certificate_arn: "",
      api_acm_certificate_arn: "",
      auth_provider: "dev_otp",
      demo_mode: true,
      permissions_enforce: true,
      jwt_secret: $jwt_secret,
      vault_encryption_key: $vault_key,
      admin_email: "restore-admin@example.invalid",
      backup_copy_region: $backup_copy_region,
      backups_enabled: false,
      automated_restore_testing_enabled: false,
      account_security_services_enabled: false,
      cloudtrail_s3_data_events_enabled: false,
      cloudtrail_insights_enabled: false,
      waf_enabled: false,
      monthly_cost_budget_usd: 0,
      operations_alarm_email: "",
      restore_rehearsal_mode: true,
      restore_app_db_snapshot_identifier: $app_snapshot,
      restore_openfga_db_snapshot_identifier: $fga_snapshot,
      db_username: $app_db_username,
      db_name: $app_db_name,
      openfga_db_username: $openfga_db_username,
      openfga_db_name: $openfga_db_name,
      restore_redis_snapshot_name: $redis_snapshot,
      restore_rehearsal_ingress_cidrs: $ingress_cidrs,
      platform_bootstrap_mode: $platform_bootstrap_mode,
      api_image_tag: $api_image_tag,
      web_image_tag: $web_image_tag
    }' >"$TFVARS_FILE"
else
  printf '  Reusing protected tfvars: %s\n' "$TFVARS_FILE"
  [[ $(jq -r '.restore_app_db_snapshot_identifier' "$TFVARS_FILE") == "$APP_DB_SNAPSHOT_IDENTIFIER" ]] \
    || die "existing tfvars uses a different application snapshot; choose a new EXERCISE_ID/work directory"
  [[ $(jq -r '.restore_openfga_db_snapshot_identifier' "$TFVARS_FILE") == "$OPENFGA_DB_SNAPSHOT_IDENTIFIER" ]] \
    || die "existing tfvars uses a different OpenFGA snapshot; choose a new EXERCISE_ID/work directory"

  # Preserve generated secrets while allowing the reviewed bootstrap-to-service
  # transition and a newly restricted operator CIDR set.
  UPDATED_TFVARS="$RESTORE_WORK_DIR/restore.auto.tfvars.json.next"
  jq \
    --arg api_image_tag "$API_IMAGE_TAG" \
    --arg web_image_tag "$WEB_IMAGE_TAG" \
    --argjson ingress_cidrs "$INGRESS_JSON" \
    --argjson platform_bootstrap_mode "$([[ "$START_SERVICES" == "true" ]] && printf false || printf true)" \
    '.api_image_tag = $api_image_tag |
     .web_image_tag = $web_image_tag |
     .restore_rehearsal_ingress_cidrs = $ingress_cidrs |
     .platform_bootstrap_mode = $platform_bootstrap_mode' \
    "$TFVARS_FILE" >"$UPDATED_TFVARS"
  mv "$UPDATED_TFVARS" "$TFVARS_FILE"
fi

export AWS_PROFILE AWS_REGION TF_DATA_DIR
terraform -chdir="$INFRA_DIR" init -reconfigure -input=false \
  -backend-config="bucket=$RESTORE_STATE_BUCKET" \
  -backend-config="key=$RESTORE_STATE_KEY" \
  -backend-config="region=$AWS_REGION" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
terraform -chdir="$INFRA_DIR" validate
terraform -chdir="$INFRA_DIR" plan -input=false -lock-timeout=5m \
  -var-file="$TFVARS_FILE" -out="$PLAN_FILE"
terraform -chdir="$INFRA_DIR" show -json "$PLAN_FILE" >"$PLAN_JSON"

jq -e \
  --arg app_snapshot "$APP_DB_SNAPSHOT_IDENTIFIER" \
  --arg fga_snapshot "$OPENFGA_DB_SNAPSHOT_IDENTIFIER" '
  ([.resource_changes[] | select(.address == "aws_db_instance.main") | .change.after.snapshot_identifier][0] == $app_snapshot) and
  ([.resource_changes[] | select(.address == "aws_db_instance.openfga") | .change.after.snapshot_identifier][0] == $fga_snapshot) and
  ([.resource_changes[] | select(.address == "aws_lb.api") | .change.after.internal][0] == true) and
  ([.resource_changes[] | select(.address == "aws_lb.web") | .change.after.internal][0] == true) and
  ([.resource_changes[] | select(.address == "aws_security_group.alb") | .change.after.ingress[]?.cidr_blocks[]?] | index("0.0.0.0/0") == null)
  ' "$PLAN_JSON" >/dev/null || die "plan failed the quarantine/snapshot contract"

if [[ "$START_SERVICES" == "false" ]]; then
  jq -e '
    ([.resource_changes[] |
        select(.address == "aws_ecs_service.api" or .address == "aws_ecs_service.web" or .address == "aws_ecs_service.worker" or .address == "aws_ecs_service.openfga") |
        .change.after.desired_count] | all(. == 0)) and
    ([.resource_changes[] |
        select(.address == "aws_appautoscaling_target.api" or .address == "aws_appautoscaling_target.web" or .address == "aws_appautoscaling_target.worker") |
        .change.after.min_capacity] | all(. == 0))
  ' "$PLAN_JSON" >/dev/null || die "bootstrap rehearsal plan must keep every ECS service at zero"
fi

printf '\nPlan passed restore safety checks.\n'
printf '  Sensitive plan: %s\n' "$PLAN_FILE"
printf '  Sensitive tfvars: %s\n' "$TFVARS_FILE"
printf '  State: s3://%s/%s\n' "$RESTORE_STATE_BUCKET" "$RESTORE_STATE_KEY"

if [[ "$ACTION" == "apply" ]]; then
  EXPECTED_CONFIRMATION="apply-restore-${EXERCISE_ID}"
  [[ "${CONFIRM_RESTORE_APPLY:-}" == "$EXPECTED_CONFIRMATION" ]] \
    || die "apply is cost-bearing; set CONFIRM_RESTORE_APPLY=$EXPECTED_CONFIRMATION after plan review"
  terraform -chdir="$INFRA_DIR" apply -input=false "$PLAN_FILE"
  printf '\nRestore infrastructure applied. Client traffic is still forbidden.\n'
  printf 'Next: restore representative/all S3 versions into the new artifact bucket, run migrations once, verify image digests, then re-plan START_SERVICES=true with restricted operator CIDRs.\n'
else
  printf 'No AWS resources were changed. Review the plan before any separately approved ACTION=apply run.\n'
fi
