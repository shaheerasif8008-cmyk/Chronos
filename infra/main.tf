terraform {
  # Keep local and CI behavior reproducible. Upgrade this constraint and the CI
  # setup-terraform version together after reviewing the generated plan.
  required_version = ">= 1.15.0, < 1.16.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.62"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Remote state.
  backend "s3" {
    bucket       = "chronos-terraform-state-544294779377-us-east-1"
    key          = "prod/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Application = var.app_name
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}

# The secondary provider is used only for cross-Region backup vaults, KMS, and
# secret replicas. It does not create a warm application stack; the tested DR
# runbook deliberately performs that promotion so DNS and restored data cannot
# be switched accidentally.
provider "aws" {
  alias  = "dr"
  region = var.backup_copy_region

  default_tags {
    tags = {
      Application = var.app_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Purpose     = "disaster-recovery"
    }
  }
}

data "aws_acm_certificate" "web_wildcard" {
  count       = local.is_production && var.domain_name != "" ? 1 : 0
  domain      = "*.${var.domain_name}"
  statuses    = ["ISSUED"]
  types       = ["AMAZON_ISSUED"]
  most_recent = true
}

data "aws_acm_certificate" "api" {
  count       = local.is_production && local.api_domain_name != "" ? 1 : 0
  domain      = local.api_domain_name
  statuses    = ["ISSUED"]
  types       = ["AMAZON_ISSUED"]
  most_recent = true
}

locals {
  prefix                = "${var.app_name}-${var.environment}"
  is_production         = startswith(lower(var.environment), "prod")
  api_service_count     = var.platform_bootstrap_mode ? 0 : var.api_desired_count
  web_service_count     = var.platform_bootstrap_mode ? 0 : var.web_desired_count
  worker_service_count  = var.platform_bootstrap_mode ? 0 : var.worker_desired_count
  openfga_service_count = var.platform_bootstrap_mode ? 0 : var.openfga_desired_count
  web_domain_name       = var.web_domain_name != "" ? var.web_domain_name : (var.domain_name != "" ? "app.${var.domain_name}" : "")
  api_domain_name       = var.api_domain_name != "" ? var.api_domain_name : (var.domain_name != "" ? "api.${var.domain_name}" : "")
  web_origin            = local.web_domain_name != "" ? "https://${local.web_domain_name}" : "http://${aws_lb.web.dns_name}"
  api_origin            = local.api_domain_name != "" ? "https://${local.api_domain_name}" : "http://${aws_lb.api.dns_name}"
  tenant_web_origin     = var.domain_name != "" ? "https://*.${var.domain_name}" : ""
  web_certificate_arn   = var.web_acm_certificate_arn != "" ? var.web_acm_certificate_arn : var.acm_certificate_arn
  api_certificate_arn   = var.api_acm_certificate_arn != "" ? var.api_acm_certificate_arn : var.acm_certificate_arn
  restore_inputs_configured = anytrue([
    trimspace(var.restore_app_db_snapshot_identifier) != "",
    trimspace(var.restore_openfga_db_snapshot_identifier) != "",
    trimspace(var.restore_redis_snapshot_name) != "",
  ])
  restore_external_credentials_absent = nonsensitive(alltrue([
    trimspace(var.sendgrid_api_key) == "",
    trimspace(var.openrouter_api_key) == "",
    trimspace(var.backup_api_key) == "",
    trimspace(var.tavily_api_key) == "",
    trimspace(var.e2b_api_key) == "",
    trimspace(var.composio_api_key) == "",
    trimspace(var.canva_client_id) == "",
    trimspace(var.canva_client_secret) == "",
    trimspace(var.browserbase_api_key) == "",
    trimspace(var.langfuse_public_key) == "",
    trimspace(var.langfuse_secret_key) == "",
    trimspace(var.sentry_dsn) == "",
    trimspace(var.stripe_secret_key) == "",
    trimspace(var.stripe_webhook_secret) == "",
    trimspace(var.google_client_id) == "",
    trimspace(var.google_client_secret) == "",
    trimspace(var.github_client_id) == "",
    trimspace(var.github_client_secret) == "",
    trimspace(var.slack_client_id) == "",
    trimspace(var.slack_client_secret) == "",
    trimspace(var.slack_signing_secret) == "",
    trimspace(var.microsoft_client_id) == "",
    trimspace(var.microsoft_client_secret) == "",
    trimspace(var.teams_bot_app_id) == "",
    trimspace(var.sendgrid_inbound_public_key) == "",
    trimspace(var.cognito_app_client_secret) == "",
  ]))
}

# Root-level `check` blocks only emit warnings and therefore cannot be launch
# gates. These lifecycle preconditions are intentionally apply-blocking. Their
# input stores only booleans/config metadata, never secret values.
resource "terraform_data" "production_guard" {
  input = {
    production                 = local.is_production
    bootstrap                  = var.platform_bootstrap_mode
    identity_configured        = var.auth_provider == "cognito"
    providers_configured       = nonsensitive(trimspace(var.openrouter_api_key) != "" && trimspace(var.backup_api_key) != "" && trimspace(var.e2b_api_key) != "" && trimspace(var.composio_api_key) != "" && trimspace(var.browserbase_api_key) != "")
    client_services_ready      = nonsensitive(trimspace(var.sendgrid_api_key) != "" && trimspace(var.langfuse_public_key) != "" && trimspace(var.langfuse_secret_key) != "" && trimspace(var.sentry_dsn) != "" && trimspace(var.stripe_secret_key) != "" && trimspace(var.stripe_webhook_secret) != "")
    recovery_configured        = var.backups_enabled && var.automated_restore_testing_enabled
    account_security           = var.account_security_services_enabled && var.cloudtrail_s3_data_events_enabled
    restore_rehearsal          = var.restore_rehearsal_mode
    restore_credentials_absent = local.restore_external_credentials_absent
  }

  lifecycle {
    precondition {
      condition     = !local.restore_inputs_configured || var.restore_rehearsal_mode
      error_message = "Snapshot restore inputs are accepted only when restore_rehearsal_mode=true; production databases must never be reseeded in place."
    }

    precondition {
      condition = !var.restore_rehearsal_mode || (
        !local.is_production &&
        startswith(var.environment, "restore-") &&
        trimspace(var.restore_app_db_snapshot_identifier) != "" &&
        trimspace(var.restore_openfga_db_snapshot_identifier) != "" &&
        var.domain_name == "" &&
        var.web_domain_name == "" &&
        var.api_domain_name == "" &&
        var.acm_certificate_arn == "" &&
        var.web_acm_certificate_arn == "" &&
        var.api_acm_certificate_arn == "" &&
        var.auth_provider == "dev_otp" &&
        var.demo_mode &&
        local.restore_external_credentials_absent &&
        !var.waf_enabled &&
        !var.account_security_services_enabled &&
        !var.backups_enabled
      )
      error_message = "Restore rehearsal apply blocked: use a non-production restore-* environment with both RDS snapshots, no public domains/certificates or external credentials, dev OTP, demo mode, and WAF/account-wide security/backup resources disabled."
    }

    precondition {
      condition = !local.is_production || (
        var.domain_name != "" &&
        local.web_domain_name != "" &&
        local.api_domain_name != "" &&
        local.web_certificate_arn != "" &&
        local.api_certificate_arn != "" &&
        local.web_certificate_arn == try(data.aws_acm_certificate.web_wildcard[0].arn, "") &&
        local.api_certificate_arn == try(data.aws_acm_certificate.api[0].arn, "") &&
        var.auth_provider == "cognito" &&
        var.cognito_region != "" &&
        var.cognito_user_pool_id != "" &&
        var.cognito_app_client_id != "" &&
        var.cognito_domain != ""
      )
      error_message = "Production apply blocked: issued wildcard web/API certificates, exact public domains, and complete Cognito-only authentication are required."
    }

    precondition {
      condition = !local.is_production || (
        (
          can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", trimspace(var.cognito_domain))) ||
          can(regex("^https://([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?/?$", trimspace(var.cognito_domain)))
        ) &&
        can(regex("^https://openrouter\\.ai/api/v1/?$", trimspace(var.openrouter_api_base))) &&
        can(regex("^https://([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?/?$", trimspace(var.langfuse_host))) &&
        var.access_token_expire_minutes >= 5 &&
        var.access_token_expire_minutes <= 1440
      )
      error_message = "Production apply blocked: Cognito, OpenRouter, and Langfuse credential destinations must match their safe HTTPS contracts, and access-token TTL must be 5-1440 minutes."
    }

    precondition {
      condition = !local.is_production || (
        nonsensitive(length(var.jwt_secret) >= 32) &&
        nonsensitive(can(regex("^[0-9a-fA-F]{64}$", var.vault_encryption_key))) &&
        nonsensitive(trimspace(var.openrouter_api_key) != "") &&
        nonsensitive(trimspace(var.backup_api_key) != "") &&
        !startswith(lower(trimspace(var.backup_model)), "openrouter/") &&
        nonsensitive(trimspace(var.e2b_api_key) != "") &&
        trimspace(var.e2b_template_id) != "" &&
        trimspace(var.e2b_computer_template_id) != "" &&
        var.e2b_repo_enabled &&
        trimspace(var.e2b_repo_template_id) != "" &&
        var.e2b_repo_allow_internet_access &&
        nonsensitive(trimspace(var.composio_api_key) != "") &&
        nonsensitive(trimspace(var.github_client_id) != "") &&
        nonsensitive(trimspace(var.github_client_secret) != "") &&
        nonsensitive(trimspace(var.canva_client_id) != "") &&
        nonsensitive(trimspace(var.canva_client_secret) != "") &&
        nonsensitive(trimspace(var.browserbase_api_key) != "")
      )
      error_message = "Production apply blocked: strong JWT/vault keys plus OpenRouter, an independent direct-provider LLM backup, hardened E2B execution/desktop/repository profiles, Composio, Canva Connect, and Browserbase configuration are required."
    }

    precondition {
      condition = !local.is_production || (
        var.demo_mode == false &&
        var.permissions_enforce &&
        var.per_org_daily_token_limit > 0 &&
        (var.platform_bootstrap_mode || (
          var.api_desired_count >= 2 &&
          var.api_min_count >= 2 &&
          var.api_desired_count >= var.api_min_count &&
          var.api_max_count >= var.api_desired_count &&
          var.web_desired_count >= 2 &&
          var.web_min_count >= 2 &&
          var.web_desired_count >= var.web_min_count &&
          var.web_max_count >= var.web_desired_count &&
          var.worker_desired_count >= 2 &&
          var.worker_min_count >= 2 &&
          var.worker_desired_count >= var.worker_min_count &&
          var.worker_max_count >= var.worker_desired_count &&
          var.openfga_desired_count >= 2 &&
          can(regex("^[0-9a-f]{8}$", var.api_image_tag)) &&
          can(regex("^[0-9a-f]{8}$", var.web_image_tag))
        )) &&
        var.db_multi_az &&
        var.openfga_db_multi_az &&
        var.db_backup_retention_days == 35 &&
        var.redis_num_cache_clusters >= 2 &&
        var.redis_automatic_failover_enabled &&
        var.redis_multi_az_enabled &&
        var.redis_snapshot_retention_days == 35
      )
      error_message = "Production apply blocked: live providers, exact 8-character Git SHA image tags, enforced authorization, finite tenant budgets, and multi-AZ/HA service and datastore settings are required outside bootstrap mode."
    }

    precondition {
      condition = !local.is_production || (
        nonsensitive(trimspace(var.sendgrid_api_key) != "") &&
        trimspace(var.notification_from_email) != "" &&
        nonsensitive(trimspace(var.langfuse_public_key) != "") &&
        nonsensitive(trimspace(var.langfuse_secret_key) != "") &&
        nonsensitive(trimspace(var.sentry_dsn) != "") &&
        nonsensitive(trimspace(var.stripe_secret_key) != "") &&
        nonsensitive(trimspace(var.stripe_webhook_secret) != "") &&
        trimspace(var.stripe_price_pro) != "" &&
        trimspace(var.stripe_price_enterprise) != ""
      )
      error_message = "Production apply blocked: transactional email, Langfuse, Sentry, and complete Stripe billing configuration are required."
    }

    precondition {
      condition = !local.is_production || (
        var.waf_enabled &&
        trimspace(var.operations_alarm_email) != "" &&
        var.monthly_cost_budget_usd > 0 &&
        var.application_log_retention_days >= 90 &&
        var.web_log_retention_days >= 30 &&
        var.waf_log_retention_days >= 90 &&
        var.account_security_services_enabled &&
        var.cloudtrail_s3_data_events_enabled &&
        var.audit_log_retention_days >= 365 &&
        var.backups_enabled &&
        var.backup_copy_region != var.aws_region &&
        var.backup_pitr_retention_days == 35 &&
        var.backup_copy_retention_days >= 365 &&
        var.automated_restore_testing_enabled
      )
      error_message = "Production apply blocked: WAF, account security services, an operations route and budget, durable audit/application logs, 35-day PITR, cross-Region copies, and automated restore testing are required."
    }
  }
}
