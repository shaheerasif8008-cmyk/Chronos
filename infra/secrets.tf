# All app secrets are stored in Secrets Manager and injected into the ECS
# task definition via the `secrets` block (never passed as plain env vars).
# The values are set either from Terraform variables or generated randomly.

locals {
  app_secrets = {
    jwt_secret           = var.jwt_secret
    vault_encryption_key = var.vault_encryption_key
    admin_email          = var.admin_email
    sendgrid_api_key     = var.sendgrid_api_key
    openrouter_api_key   = var.openrouter_api_key
    backup_api_key       = var.backup_api_key
    tavily_api_key       = var.tavily_api_key
    langfuse_public_key  = var.langfuse_public_key
    langfuse_secret_key  = var.langfuse_secret_key
    sentry_dsn           = var.sentry_dsn
  }
}

resource "aws_secretsmanager_secret" "app" {
  for_each                = local.app_secrets
  name                    = "${local.prefix}/${each.key}"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "app" {
  for_each      = local.app_secrets
  secret_id     = aws_secretsmanager_secret.app[each.key].id
  secret_string = each.value
}

# ── Convenience locals for task definition secret injection ───────────────────
# Each entry maps an env var name → Secrets Manager ARN.
locals {
  task_secrets = [
    for k, v in aws_secretsmanager_secret.app : {
      name      = upper(k)
      valueFrom = v.arn
    }
  ]

  db_url_secret_arn      = aws_secretsmanager_secret.db_password.arn
  db_host                = aws_db_instance.main.address
  redis_primary_endpoint = aws_elasticache_replication_group.main.primary_endpoint_address
}
