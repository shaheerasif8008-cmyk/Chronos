#!/usr/bin/env bash
# post-apply.sh — read-only verification for Terraform-managed connection
# secrets. Terraform creates the AWSCURRENT versions atomically; this script
# must never reconstruct or print credentials outside state/Secrets Manager.
set -euo pipefail

REGION="${AWS_REGION:-$(terraform output -raw aws_region)}"

verify_current_version() {
  local output_name="$1"
  local arn
  arn="$(terraform output -raw "$output_name")"
  aws secretsmanager describe-secret \
    --secret-id "$arn" \
    --region "$REGION" \
    --query '{ARN:ARN,DeletedDate:DeletedDate}' \
    --output json >/dev/null
  if ! aws secretsmanager list-secret-version-ids \
    --secret-id "$arn" \
    --region "$REGION" \
    --query 'Versions[?contains(VersionStages, `AWSCURRENT`)] | length(@)' \
    --output text | grep -qx '1'; then
    echo "$output_name does not have exactly one AWSCURRENT version" >&2
    return 1
  fi
  echo "Verified Terraform-managed AWSCURRENT version: $output_name"
}

verify_current_version database_url_secret_arn
verify_current_version redis_url_secret_arn
verify_current_version openfga_database_url_secret_arn

echo ""
echo "==> Connection secrets exist; no secret material was read or written."
