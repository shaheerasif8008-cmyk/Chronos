# All app secrets are stored in Secrets Manager and injected into the ECS
# task definition via the `secrets` block (never passed as plain env vars).
# The values are set either from Terraform variables or generated randomly.

locals {
  required_app_secrets = {
    jwt_secret           = var.jwt_secret
    vault_encryption_key = var.vault_encryption_key
    admin_email          = var.admin_email
    openfga_api_token    = random_password.openfga_api_token.result
  }

  optional_app_secrets = {
    sendgrid_api_key            = var.sendgrid_api_key
    openrouter_api_key          = var.openrouter_api_key
    backup_api_key              = var.backup_api_key
    tavily_api_key              = var.tavily_api_key
    browserbase_api_key         = var.browserbase_api_key
    composio_api_key            = var.composio_api_key
    canva_client_id             = var.canva_client_id
    canva_client_secret         = var.canva_client_secret # gitleaks:allow -- Terraform variable reference only
    e2b_api_key                 = var.e2b_api_key         # gitleaks:allow -- Terraform variable reference only
    langfuse_public_key         = var.langfuse_public_key
    langfuse_secret_key         = var.langfuse_secret_key
    sentry_dsn                  = var.sentry_dsn
    stripe_secret_key           = var.stripe_secret_key
    stripe_webhook_secret       = var.stripe_webhook_secret
    google_client_id            = var.google_client_id
    google_client_secret        = var.google_client_secret
    slack_client_id             = var.slack_client_id
    slack_client_secret         = var.slack_client_secret
    slack_signing_secret        = var.slack_signing_secret
    microsoft_client_id         = var.microsoft_client_id
    microsoft_client_secret     = var.microsoft_client_secret
    teams_bot_app_id            = var.teams_bot_app_id
    sendgrid_inbound_public_key = var.sendgrid_inbound_public_key
    github_client_id            = var.github_client_id
    github_client_secret        = var.github_client_secret
    cognito_app_client_secret   = var.cognito_app_client_secret
  }

  # Optional providers stay genuinely absent when they are not configured.
  # Injecting a sentinel such as "not_configured" makes Settings treat the
  # provider as enabled and causes misleading runtime failures.
  configured_optional_app_secrets = {
    for key, value in local.optional_app_secrets : key => value
    if nonsensitive(trimspace(value) != "")
  }
  app_secrets = merge(local.required_app_secrets, local.configured_optional_app_secrets)
}

resource "random_password" "openfga_api_token" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "app" {
  for_each                = local.app_secrets
  name                    = "${local.prefix}/${each.key}"
  recovery_window_in_days = 7

  dynamic "replica" {
    for_each = var.backups_enabled ? [var.backup_copy_region] : []
    content {
      region     = replica.value
      kms_key_id = aws_kms_key.dr[0].arn
    }
  }
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
