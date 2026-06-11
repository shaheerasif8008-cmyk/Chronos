#!/usr/bin/env bash
# post-apply.sh — writes the computed DATABASE_URL and REDIS_URL secrets into
# Secrets Manager after `terraform apply` has created the RDS and Redis resources.
# Run from the infra/ directory after every `terraform apply`.
set -euo pipefail

REGION="${1:-us-east-1}"
PREFIX="chronos-prod"

echo "==> Reading Terraform outputs"
RDS_HOST=$(terraform output -raw rds_endpoint)
REDIS_HOST=$(terraform output -raw redis_primary_endpoint)
DB_PASS=$(aws secretsmanager get-secret-value \
  --secret-id "${PREFIX}/rds/password" \
  --region "$REGION" \
  --query SecretString --output text)

DATABASE_URL="postgresql+asyncpg://chronos:${DB_PASS}@${RDS_HOST}:5432/chronos"
REDIS_URL="rediss://${REDIS_HOST}:6379/0"

echo "==> Writing DATABASE_URL secret"
aws secretsmanager put-secret-value \
  --secret-id "${PREFIX}/database_url" \
  --secret-string "$DATABASE_URL" \
  --region "$REGION"

echo "==> Writing REDIS_URL secret"
aws secretsmanager put-secret-value \
  --secret-id "${PREFIX}/redis_url" \
  --secret-string "$REDIS_URL" \
  --region "$REGION"

echo ""
echo "==> Done. Run the migration task now or let the next deploy trigger it."
echo "    aws ecs run-task --cluster ${PREFIX}-cluster --task-definition ${PREFIX}-migrate ..."
