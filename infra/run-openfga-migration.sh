#!/usr/bin/env bash
# Run the pinned OpenFGA datastore migration exactly once, wait for it, and
# fail unless the named migration container exits zero. Run this through the
# approved production terminal session described in PRODUCTION_OPERATIONS.md.
set -euo pipefail

REGION="${AWS_REGION:-$(terraform output -raw aws_region)}"
CLUSTER="$(terraform output -raw ecs_cluster_name)"
TASK_FAMILY="$(terraform output -raw openfga_migrate_task_definition_family)"
SUBNETS="$(terraform output -json private_subnet_ids | jq -r 'join(",")')"
SECURITY_GROUP="$(terraform output -raw openfga_security_group_id)"

if [[ -z "$SUBNETS" || -z "$SECURITY_GROUP" ]]; then
  echo "OpenFGA migration network outputs are empty" >&2
  exit 1
fi

RUN_RESULT="$(aws ecs run-task \
  --region "$REGION" \
  --cluster "$CLUSTER" \
  --task-definition "$TASK_FAMILY" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SECURITY_GROUP],assignPublicIp=DISABLED}" \
  --output json)"

FAILURE_COUNT="$(jq '.failures | length' <<<"$RUN_RESULT")"
if [[ "$FAILURE_COUNT" != "0" ]]; then
  jq '{failures}' <<<"$RUN_RESULT" >&2
  exit 1
fi

TASK_ARN="$(jq -r '.tasks[0].taskArn // empty' <<<"$RUN_RESULT")"
if [[ -z "$TASK_ARN" ]]; then
  echo "ECS did not return an OpenFGA migration task ARN" >&2
  exit 1
fi

echo "Waiting for OpenFGA migration task: $TASK_ARN"
aws ecs wait tasks-stopped --region "$REGION" --cluster "$CLUSTER" --tasks "$TASK_ARN"

TASK_RESULT="$(aws ecs describe-tasks \
  --region "$REGION" \
  --cluster "$CLUSTER" \
  --tasks "$TASK_ARN" \
  --output json)"

EXIT_CODE="$(jq -r '.tasks[0].containers[] | select(.name == "openfga-migrate") | .exitCode // empty' <<<"$TASK_RESULT")"
STOP_REASON="$(jq -r '.tasks[0].stoppedReason // "unknown"' <<<"$TASK_RESULT")"

if [[ "$EXIT_CODE" != "0" ]]; then
  echo "OpenFGA migration failed (exit=$EXIT_CODE, reason=$STOP_REASON). Inspect /ecs/chronos-prod/openfga." >&2
  exit 1
fi

echo "OpenFGA datastore migration completed successfully."
