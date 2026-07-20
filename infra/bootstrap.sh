#!/usr/bin/env bash
# bootstrap.sh — one-time setup before running `terraform init`.
# Run this once from your local machine with AWS credentials configured.
# Usage: cd infra && bash bootstrap.sh [aws-region] [aws-profile]
set -euo pipefail

REGION="${1:-us-east-1}"
PROFILE="${2:-default}"
ACCOUNT_ID=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)

echo "==> AWS Account: $ACCOUNT_ID  Region: $REGION"

# ── Terraform state bucket ────────────────────────────────────────────────────
BUCKET="chronos-terraform-state-${ACCOUNT_ID}-${REGION}"
echo "==> Creating S3 state bucket: $BUCKET"
if aws s3api head-bucket --bucket "$BUCKET" --profile "$PROFILE" 2>/dev/null; then
  echo "    (already exists)"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --profile "$PROFILE" \
    $( [[ "$REGION" != "us-east-1" ]] && echo "--create-bucket-configuration LocationConstraint=$REGION" )
  echo "    created."
fi

# Apply the controls on every run so an existing bootstrap bucket cannot drift
# silently. The customer-managed key is necessary because Terraform state holds
# generated passwords and provider inputs even when application secrets use
# Secrets Manager at runtime.
STATE_KEY_ALIAS="alias/chronos-terraform-state"
STATE_KEY_ID=$(aws kms list-aliases --region "$REGION" --profile "$PROFILE" \
  --query "Aliases[?AliasName=='${STATE_KEY_ALIAS}'].TargetKeyId | [0]" --output text)
if [[ -z "$STATE_KEY_ID" || "$STATE_KEY_ID" == "None" ]]; then
  STATE_KEY_ID=$(aws kms create-key \
    --description "Chronos Terraform state" \
    --region "$REGION" \
    --profile "$PROFILE" \
    --query KeyMetadata.KeyId --output text)
  aws kms enable-key-rotation --key-id "$STATE_KEY_ID" --region "$REGION" --profile "$PROFILE"
  aws kms create-alias --alias-name "$STATE_KEY_ALIAS" --target-key-id "$STATE_KEY_ID" \
    --region "$REGION" --profile "$PROFILE"
fi
STATE_KEY_ARN=$(aws kms describe-key --key-id "$STATE_KEY_ID" --region "$REGION" --profile "$PROFILE" \
  --query KeyMetadata.Arn --output text)

aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled \
  --profile "$PROFILE"
aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration \
    "{\"Rules\":[{\"ApplyServerSideEncryptionByDefault\":{\"SSEAlgorithm\":\"aws:kms\",\"KMSMasterKeyID\":\"${STATE_KEY_ARN}\"},\"BucketKeyEnabled\":true}]}" \
  --profile "$PROFILE"
aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
  --profile "$PROFILE"
aws s3api put-bucket-lifecycle-configuration \
  --bucket "$BUCKET" \
  --lifecycle-configuration \
    '{"Rules":[{"ID":"retain-state-history-one-year","Status":"Enabled","Filter":{"Prefix":""},"NoncurrentVersionExpiration":{"NoncurrentDays":365}}]}' \
  --profile "$PROFILE"
aws s3api put-bucket-policy \
  --bucket "$BUCKET" \
  --policy "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"DenyInsecureTransport\",\"Effect\":\"Deny\",\"Principal\":\"*\",\"Action\":\"s3:*\",\"Resource\":[\"arn:aws:s3:::${BUCKET}\",\"arn:aws:s3:::${BUCKET}/*\"],\"Condition\":{\"Bool\":{\"aws:SecureTransport\":\"false\"}}}]}" \
  --profile "$PROFILE"

# ── GitHub Actions OIDC provider ─────────────────────────────────────────────
echo "==> Ensuring GitHub OIDC provider exists"
OIDC_URL="https://token.actions.githubusercontent.com"
if aws iam list-open-id-connect-providers --profile "$PROFILE" \
    | grep -q "token.actions.githubusercontent.com"; then
  echo "    (already exists)"
else
  aws iam create-open-id-connect-provider \
    --url "$OIDC_URL" \
    --client-id-list "sts.amazonaws.com" \
    --thumbprint-list "6938fd4d98bab03faadb97b34396831e3780aea1" \
    --profile "$PROFILE"
  echo "    created."
fi

echo ""
echo "==> Bootstrap complete. Next steps:"
echo "    1. Copy terraform.tfvars.example → terraform.tfvars and fill in secrets."
echo "    2. Follow docs/TERRAFORM_STATE_ADOPTION.md before the first plan."
echo "    3. Follow the zero-task bootstrap sequence in docs/PRODUCTION_OPERATIONS.md."
echo "    4. Use post-apply.sh only to verify Terraform-managed secret versions."
