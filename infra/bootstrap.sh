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
  aws s3api put-bucket-versioning \
    --bucket "$BUCKET" \
    --versioning-configuration Status=Enabled \
    --profile "$PROFILE"
  aws s3api put-bucket-encryption \
    --bucket "$BUCKET" \
    --server-side-encryption-configuration \
      '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' \
    --profile "$PROFILE"
  aws s3api put-public-access-block \
    --bucket "$BUCKET" \
    --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" \
    --profile "$PROFILE"
  echo "    created."
fi

# ── DynamoDB lock table ───────────────────────────────────────────────────────
TABLE="chronos-terraform-locks"
echo "==> Creating DynamoDB lock table: $TABLE"
if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" --profile "$PROFILE" 2>/dev/null; then
  echo "    (already exists)"
else
  aws dynamodb create-table \
    --table-name "$TABLE" \
    --attribute-definitions AttributeName=LockID,AttributeType=S \
    --key-schema AttributeName=LockID,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST \
    --region "$REGION" \
    --profile "$PROFILE"
  echo "    created."
fi

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
echo "    2. terraform init"
echo "    3. terraform plan -out=tfplan"
echo "    4. terraform apply tfplan"
echo "    5. After apply, run: bash post-apply.sh to write DB/Redis connection URLs."
echo "    6. Add AWS_DEPLOY_ROLE_ARN to GitHub Actions secrets."
