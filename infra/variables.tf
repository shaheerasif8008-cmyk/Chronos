variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment. Names beginning with prod receive every production safety gate (for example prod and prod-dr)."
  type        = string
  default     = "prod"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,18}[a-z0-9]$", var.environment))
    error_message = "environment must be a lowercase 3-20 character slug."
  }
}

variable "app_name" {
  description = "Application name prefix for all resources"
  type        = string
  default     = "chronos"
}

# ── Networking ────────────────────────────────────────────────────────────────
variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "availability_zones" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}

# ── Domain ────────────────────────────────────────────────────────────────────
variable "domain_name" {
  description = "Root domain used to derive app.<domain> and api.<domain>. DNS and ACM are managed outside this module."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "Optional wildcard/shared ACM certificate fallback. Prefer the separate web/api certificate variables."
  type        = string
  default     = ""
}

variable "web_domain_name" {
  description = "Public web hostname. Defaults to app.<domain_name>."
  type        = string
  default     = ""
}

variable "api_domain_name" {
  description = "Public API hostname. Defaults to api.<domain_name>."
  type        = string
  default     = ""
}

variable "web_acm_certificate_arn" {
  description = "ACM certificate in aws_region that covers web_domain_name and *.<domain_name> tenant hosts."
  type        = string
  default     = ""
}

variable "api_acm_certificate_arn" {
  description = "ACM certificate in aws_region that covers api_domain_name."
  type        = string
  default     = ""
}

# ── ECS ───────────────────────────────────────────────────────────────────────
variable "api_cpu" {
  description = "Fargate vCPU units for the API service (1024 = 1 vCPU)"
  type        = number
  default     = 1024
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the API service"
  type        = number
  default     = 3072

  validation {
    condition     = var.api_memory >= 3072
    error_message = "api_memory must be at least 3072 MiB so the API and required ClamAV sidecar cannot contend below their reservations."
  }
}

variable "clamav_image" {
  description = "Pinned official ClamAV image used by the private API sidecar."
  type        = string
  default     = "clamav/clamav:1.4.5-debian13-slim"

  validation {
    condition     = can(regex("^clamav/clamav:[0-9]+\\.[0-9]+\\.[0-9]+-[A-Za-z0-9._-]+$", var.clamav_image))
    error_message = "clamav_image must use an exact semantic-version tag and must never use latest or a floating major/minor tag."
  }
}

variable "web_cpu" {
  type    = number
  default = 512
}

variable "web_memory" {
  type    = number
  default = 1024
}

variable "worker_cpu" {
  description = "Fargate CPU units for the connector queue worker."
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "Fargate memory in MiB for the connector queue worker."
  type        = number
  default     = 1024
}

variable "worker_desired_count" {
  description = "Number of connector queue workers. Keep at least two in production."
  type        = number
  default     = 2
}

variable "openfga_cpu" {
  type    = number
  default = 256
}

variable "openfga_memory" {
  type    = number
  default = 512
}

variable "openfga_image" {
  description = "Pinned stable OpenFGA image used by both migration and server containers."
  type        = string
  default     = "openfga/openfga:v1.15.1"

  validation {
    condition     = can(regex("^openfga/openfga:v[0-9]+\\.[0-9]+\\.[0-9]+$", var.openfga_image))
    error_message = "openfga_image must use an exact semantic-version tag, never latest or a floating major/minor tag."
  }
}

variable "openfga_desired_count" {
  description = "Number of stateless OpenFGA replicas. Production requires at least two."
  type        = number
  default     = 2

  validation {
    condition     = var.openfga_desired_count >= 2
    error_message = "openfga_desired_count must be at least 2 for availability. The separate platform_bootstrap_mode safely suppresses tasks during first provisioning."
  }
}

variable "openfga_datastore_max_open_conns" {
  description = "Maximum PostgreSQL connections per OpenFGA replica."
  type        = number
  default     = 10
}

variable "openfga_datastore_min_open_conns" {
  description = "Warm PostgreSQL connections maintained per OpenFGA replica."
  type        = number
  default     = 2
}

variable "openfga_datastore_max_idle_conns" {
  description = "Maximum idle PostgreSQL connections per OpenFGA replica."
  type        = number
  default     = 5
}

variable "openfga_datastore_min_idle_conns" {
  description = "Minimum idle PostgreSQL connections per OpenFGA replica."
  type        = number
  default     = 2
}

variable "openfga_datastore_conn_max_idle_time" {
  description = "Maximum idle lifetime for an OpenFGA PostgreSQL connection."
  type        = string
  default     = "5m"
}

variable "openfga_datastore_conn_max_lifetime" {
  description = "Maximum total lifetime for an OpenFGA PostgreSQL connection."
  type        = string
  default     = "30m"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "web_desired_count" {
  type    = number
  default = 2
}

variable "api_min_count" {
  type    = number
  default = 2
}

variable "api_max_count" {
  type    = number
  default = 10
}

variable "web_min_count" {
  description = "Minimum web tasks maintained by Application Auto Scaling."
  type        = number
  default     = 2
}

variable "web_max_count" {
  description = "Maximum web tasks maintained by Application Auto Scaling."
  type        = number
  default     = 6
}

variable "worker_min_count" {
  description = "Minimum connector worker tasks maintained by Application Auto Scaling."
  type        = number
  default     = 2
}

variable "worker_max_count" {
  description = "Maximum connector worker tasks maintained by Application Auto Scaling."
  type        = number
  default     = 10
}

# ── RDS ───────────────────────────────────────────────────────────────────────
variable "db_instance_class" {
  type    = string
  default = "db.t3.medium"
}

variable "db_allocated_storage" {
  type    = number
  default = 50
}

variable "db_name" {
  type    = string
  default = "chronos"
}

variable "db_username" {
  type    = string
  default = "chronos"
}

variable "db_multi_az" {
  description = "Enable Multi-AZ for RDS (recommended for prod)"
  type        = bool
  default     = true
}

variable "db_engine_version" {
  description = "PostgreSQL major version. Pin the major and let RDS apply tested minor upgrades during the maintenance window."
  type        = string
  default     = "15"

  validation {
    condition     = can(regex("^[0-9]+$", var.db_engine_version))
    error_message = "db_engine_version must be a PostgreSQL major version such as 15."
  }
}

variable "db_backup_retention_days" {
  description = "Native RDS point-in-time recovery retention. Production requires the AWS maximum of 35 days."
  type        = number
  default     = 35

  validation {
    condition     = var.db_backup_retention_days >= 7 && var.db_backup_retention_days <= 35
    error_message = "db_backup_retention_days must be between 7 and 35."
  }
}

variable "openfga_db_instance_class" {
  description = "Dedicated OpenFGA PostgreSQL instance class. Isolation prevents authorization traffic and schema operations from contending with the application database."
  type        = string
  default     = "db.t4g.small"
}

variable "openfga_db_allocated_storage" {
  description = "Initial gp3 storage in GiB for the dedicated OpenFGA datastore."
  type        = number
  default     = 20

  validation {
    condition     = var.openfga_db_allocated_storage >= 20
    error_message = "openfga_db_allocated_storage must be at least 20 GiB."
  }
}

variable "openfga_db_name" {
  description = "Database name created on the dedicated OpenFGA RDS instance."
  type        = string
  default     = "openfga"
}

variable "openfga_db_username" {
  description = "Master username for the dedicated OpenFGA RDS instance."
  type        = string
  default     = "openfga"
}

variable "openfga_db_multi_az" {
  description = "Keep the authorization datastore available through an Availability Zone failure."
  type        = bool
  default     = true
}

# ── ElastiCache ───────────────────────────────────────────────────────────────
variable "redis_node_type" {
  type    = string
  default = "cache.t3.medium"
}

variable "redis_num_cache_clusters" {
  description = "Number of Redis replicas (1 = primary only, 2+ = primary + replicas)"
  type        = number
  default     = 2
}

variable "redis_automatic_failover_enabled" {
  description = "Enable automatic failover for Redis. Some free-plan accounts must keep this disabled even with replicas."
  type        = bool
  default     = true
}

variable "redis_multi_az_enabled" {
  description = "Enable Redis Multi-AZ. Some free-plan accounts must keep this disabled even with replicas."
  type        = bool
  default     = true
}

variable "redis_snapshot_retention_days" {
  description = "Daily ElastiCache snapshot retention. Production uses the service maximum of 35 days."
  type        = number
  default     = 35

  validation {
    condition     = var.redis_snapshot_retention_days >= 1 && var.redis_snapshot_retention_days <= 35
    error_message = "redis_snapshot_retention_days must be between 1 and 35."
  }
}

# ── Edge protection / operations ─────────────────────────────────────────────
variable "waf_enabled" {
  description = "Attach regional AWS WAF protection packs to both public ALBs. Required in production."
  type        = bool
  default     = true
}

variable "waf_api_rate_limit" {
  description = "Maximum API requests from one IP in a five-minute evaluation window."
  type        = number
  default     = 6000

  validation {
    condition     = var.waf_api_rate_limit >= 100
    error_message = "waf_api_rate_limit must be at least 100 requests per five minutes."
  }
}

variable "waf_auth_rate_limit" {
  description = "Maximum /auth requests from one IP in a five-minute evaluation window."
  type        = number
  default     = 1000

  validation {
    condition     = var.waf_auth_rate_limit >= 100
    error_message = "waf_auth_rate_limit must be at least 100 requests per five minutes so shared client NATs are not self-denied."
  }
}

variable "waf_web_rate_limit" {
  description = "Maximum web requests from one IP in a five-minute evaluation window."
  type        = number
  default     = 10000

  validation {
    condition     = var.waf_web_rate_limit >= 100
    error_message = "waf_web_rate_limit must be at least 100 requests per five minutes."
  }
}

variable "operations_alarm_email" {
  description = "24x7 operations mailbox subscribed to production alarms. The subscription must be confirmed after apply."
  type        = string
  default     = ""

  validation {
    condition     = var.operations_alarm_email == "" || can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.operations_alarm_email))
    error_message = "operations_alarm_email must be empty or a valid email address."
  }
}

variable "monthly_cost_budget_usd" {
  description = "Monthly AWS cost budget for this account. Production requires an explicit non-zero ceiling."
  type        = number
  default     = 1000

  validation {
    condition     = var.monthly_cost_budget_usd >= 0
    error_message = "monthly_cost_budget_usd cannot be negative."
  }
}

variable "application_log_retention_days" {
  description = "CloudWatch retention for API, worker, OpenFGA, and migration logs."
  type        = number
  default     = 90
}

variable "web_log_retention_days" {
  description = "CloudWatch retention for web logs."
  type        = number
  default     = 30
}

variable "waf_log_retention_days" {
  description = "CloudWatch retention for redacted AWS WAF request logs."
  type        = number
  default     = 90
}

variable "account_security_services_enabled" {
  description = "Enable CloudTrail, Config, GuardDuty, Security Hub, Inspector, and IAM Access Analyzer. Required in production."
  type        = bool
  default     = true
}

variable "audit_log_retention_days" {
  description = "Immutable S3 and CloudWatch retention for AWS account audit evidence. Values follow CloudWatch Logs' supported retention periods."
  type        = number
  default     = 365

  validation {
    condition     = contains([365, 400, 545, 731, 1096, 1827, 2192, 2557], var.audit_log_retention_days)
    error_message = "audit_log_retention_days must be a supported CloudWatch retention value from one through seven years: 365, 400, 545, 731, 1096, 1827, 2192, or 2557."
  }
}

variable "cloudtrail_s3_data_events_enabled" {
  description = "Record object-level access to the Chronos artifact bucket. Required in production."
  type        = bool
  default     = true
}

variable "cloudtrail_insights_enabled" {
  description = "Enable CloudTrail API call-rate and error-rate Insights."
  type        = bool
  default     = true
}

# ── Backup / disaster recovery ───────────────────────────────────────────────
variable "backups_enabled" {
  description = "Enable governed AWS Backup PITR and restore testing. Required in production."
  type        = bool
  default     = true
}

variable "backup_copy_region" {
  description = "Second AWS Region that receives immutable daily backup copies and replicated secrets."
  type        = string
  default     = "us-west-2"
}

variable "backup_pitr_retention_days" {
  description = "Retention for the in-region continuous recovery point. AWS Backup PITR supports at most 35 days."
  type        = number
  default     = 35

  validation {
    condition     = var.backup_pitr_retention_days >= 7 && var.backup_pitr_retention_days <= 35
    error_message = "backup_pitr_retention_days must be between 7 and 35."
  }
}

variable "backup_copy_retention_days" {
  description = "Retention for daily cross-Region snapshot copies."
  type        = number
  default     = 365

  validation {
    condition     = var.backup_copy_retention_days >= 35 && var.backup_copy_retention_days <= 3650
    error_message = "backup_copy_retention_days must be between 35 days and 10 years."
  }
}

variable "automated_restore_testing_enabled" {
  description = "Run an AWS Backup RDS restore test monthly and automatically clean up the temporary database."
  type        = bool
  default     = true
}

variable "restore_rehearsal_mode" {
  description = "Build a quarantined restore-* environment from explicit snapshots. This is never a production cutover switch."
  type        = bool
  default     = false
}

variable "restore_app_db_snapshot_identifier" {
  description = "RDS DB snapshot identifier or ARN used only to seed the quarantined Chronos application database. Keep the value pinned after creation."
  type        = string
  default     = ""

  validation {
    condition     = var.restore_app_db_snapshot_identifier == "" || !can(regex("[[:space:]]", var.restore_app_db_snapshot_identifier))
    error_message = "restore_app_db_snapshot_identifier cannot contain whitespace."
  }
}

variable "restore_openfga_db_snapshot_identifier" {
  description = "RDS DB snapshot identifier or ARN used only to seed the quarantined OpenFGA datastore. Keep the value pinned after creation."
  type        = string
  default     = ""

  validation {
    condition     = var.restore_openfga_db_snapshot_identifier == "" || !can(regex("[[:space:]]", var.restore_openfga_db_snapshot_identifier))
    error_message = "restore_openfga_db_snapshot_identifier cannot contain whitespace."
  }
}

variable "restore_redis_snapshot_name" {
  description = "Optional same-Region ElastiCache snapshot used to seed the quarantined Redis replication group. Cross-Region snapshot export/import remains an operator step."
  type        = string
  default     = ""

  validation {
    condition     = var.restore_redis_snapshot_name == "" || can(regex("^[A-Za-z][A-Za-z0-9-]{0,254}$", var.restore_redis_snapshot_name))
    error_message = "restore_redis_snapshot_name must be a valid ElastiCache snapshot name."
  }
}

variable "restore_rehearsal_ingress_cidrs" {
  description = "Private operator CIDRs allowed to reach the internal rehearsal ALBs. Empty is the fail-closed default. Never use 0.0.0.0/0."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.restore_rehearsal_ingress_cidrs :
      can(cidrnetmask(cidr)) && cidr != "0.0.0.0/0"
    ])
    error_message = "restore_rehearsal_ingress_cidrs must contain valid restricted IPv4 CIDRs and cannot include 0.0.0.0/0."
  }
}

variable "platform_bootstrap_mode" {
  description = "Temporary first-deploy mode that provisions dependencies/ECR with zero service tasks so images and datastore migrations can be installed safely. Never leave enabled."
  type        = bool
  default     = false
}

# ── ECR images ───────────────────────────────────────────────────────────────
variable "api_image_tag" {
  description = "Docker image tag for the API service"
  type        = string
  default     = "latest"
}

variable "web_image_tag" {
  description = "Docker image tag for the web service"
  type        = string
  default     = "latest"
}

# ── Authentication ────────────────────────────────────────────────────────────
variable "auth_provider" {
  description = "Auth mode: 'cognito' (Cognito only), 'both' (Cognito + dev OTP fallback), or 'dev_otp'. Must be 'cognito' or 'both' for the Cognito login button to appear."
  type        = string
  default     = "cognito"

  validation {
    condition     = contains(["cognito", "both", "dev_otp"], var.auth_provider)
    error_message = "auth_provider must be one of: cognito, both, dev_otp."
  }
}

variable "cognito_user_pool_id" {
  description = "Cognito User Pool ID (e.g. us-east-1_xxxxxxxxx). Required when auth_provider is cognito/both."
  type        = string
  default     = ""
}

variable "cognito_app_client_id" {
  description = "Cognito app client ID. Required when auth_provider is cognito/both."
  type        = string
  default     = ""
}

variable "cognito_app_client_secret" {
  description = "Cognito app client secret. Leave empty if the app client has no secret."
  type        = string
  sensitive   = true
  default     = ""
}

variable "cognito_domain" {
  description = "Cognito hosted-UI lowercase domain prefix (for the AWS-managed domain) or credential-safe HTTPS custom-domain origin."
  type        = string
  default     = ""

  validation {
    condition = !startswith(lower(var.environment), "prod") || (
      can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", trimspace(var.cognito_domain))) ||
      can(regex("^https://([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?/?$", trimspace(var.cognito_domain)))
    )
    error_message = "cognito_domain must be a lowercase Cognito prefix or an HTTPS custom-domain origin without credentials, port, path, query, or fragment."
  }
}

variable "cognito_issuer_url" {
  description = "Optional exact Cognito token issuer override for custom-domain or multi-region validation. Blank uses the regional user-pool issuer."
  type        = string
  default     = ""
}

variable "cognito_jwks_url" {
  description = "Optional Cognito JWKS URL override. Blank derives /.well-known/jwks.json from the effective issuer."
  type        = string
  default     = ""
}

variable "sso_endpoint_host_allowlist" {
  description = "Comma-separated extra OIDC endpoint hosts allowed for manually configured cross-host token/JWKS endpoints."
  type        = string
  default     = ""
}

# ── App secrets (injected as Secrets Manager secrets) ─────────────────────────
variable "jwt_secret" {
  description = "JWT signing secret (min 32 chars)"
  type        = string
  sensitive   = true
}

variable "vault_encryption_key" {
  description = "64-char hex key for AES-256-GCM credential vault (openssl rand -hex 32)"
  type        = string
  sensitive   = true
}

variable "admin_email" {
  description = "Initial admin email — used by seed.py on first deploy"
  type        = string
}

variable "sendgrid_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "openrouter_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "e2b_api_key" {
  description = "E2B API key for isolated production computer/code execution."
  type        = string
  sensitive   = true
  default     = ""
}

variable "e2b_template_id" {
  description = "Optional hardened E2B template containing approved data/document dependencies."
  type        = string
  default     = ""
}

variable "e2b_sandbox_timeout_seconds" {
  description = "Maximum lifetime of an isolated E2B execution sandbox."
  type        = number
  default     = 1800
}

variable "e2b_computer_allow_internet_access" {
  description = "Allow outbound internet only for user-consented cloud-computer sessions."
  type        = bool
  default     = true
}

variable "e2b_computer_egress_allowlist" {
  description = "Comma-separated organization ceiling for exact domains approved in computer-session consent."
  type        = string
  default     = ""
}

variable "e2b_computer_template_id" {
  description = "Dedicated hardened E2B desktop template containing Xvfb, XFCE, scrot, and xdotool."
  type        = string
  default     = ""
}

variable "e2b_computer_idle_timeout_seconds" {
  description = "Idle interval before E2B auto-pauses a resumable cloud computer."
  type        = number
  default     = 900
  validation {
    condition     = var.e2b_computer_idle_timeout_seconds >= 300 && var.e2b_computer_idle_timeout_seconds <= 3600
    error_message = "e2b_computer_idle_timeout_seconds must be between 300 and 3600."
  }
}

variable "e2b_computer_max_session_seconds" {
  description = "Maximum user-consent window before the cloud computer is destroyed."
  type        = number
  default     = 14400
  validation {
    condition     = var.e2b_computer_max_session_seconds >= var.e2b_computer_idle_timeout_seconds && var.e2b_computer_max_session_seconds <= 86400
    error_message = "e2b_computer_max_session_seconds must be between the idle timeout and 86400."
  }
}

variable "e2b_computer_max_active_per_member" {
  description = "Maximum active or paused cloud computers for one member."
  type        = number
  default     = 2
  validation {
    condition     = var.e2b_computer_max_active_per_member >= 1 && var.e2b_computer_max_active_per_member <= 10
    error_message = "e2b_computer_max_active_per_member must be between 1 and 10."
  }
}

variable "e2b_computer_max_active_per_org" {
  description = "Maximum active or paused cloud computers for one organization."
  type        = number
  default     = 20
  validation {
    condition     = var.e2b_computer_max_active_per_org >= var.e2b_computer_max_active_per_member && var.e2b_computer_max_active_per_org <= 100
    error_message = "e2b_computer_max_active_per_org must be between the per-member limit and 100."
  }
}

variable "e2b_computer_screen_width" {
  description = "Cloud-computer desktop width in pixels."
  type        = number
  default     = 1280
  validation {
    condition     = var.e2b_computer_screen_width >= 800 && var.e2b_computer_screen_width <= 1920
    error_message = "e2b_computer_screen_width must be between 800 and 1920."
  }
}

variable "e2b_computer_screen_height" {
  description = "Cloud-computer desktop height in pixels."
  type        = number
  default     = 800
  validation {
    condition     = var.e2b_computer_screen_height >= 600 && var.e2b_computer_screen_height <= 1200
    error_message = "e2b_computer_screen_height must be between 600 and 1200."
  }
}

variable "e2b_repo_enabled" {
  description = "Enable persistent isolated E2B repository workspaces."
  type        = bool
  default     = false
}

variable "e2b_repo_template_id" {
  description = "Hardened E2B template containing git, Python, and pytest for repository workspaces."
  type        = string
  default     = ""
}

variable "e2b_repo_allow_internet_access" {
  description = "Allow Git/package egress only for the dedicated E2B repository profile."
  type        = bool
  default     = false
}

variable "e2b_repo_egress_allowlist" {
  description = "Comma-separated E2B allow_out domains for Git and package operations."
  type        = string
  default     = ""
}

variable "e2b_repo_timeout_seconds" {
  description = "Reconnectable E2B repository sandbox lifetime, extended on each leased operation."
  type        = number
  default     = 21600

  validation {
    condition     = var.e2b_repo_timeout_seconds >= 300 && var.e2b_repo_timeout_seconds <= 21600
    error_message = "e2b_repo_timeout_seconds must be between 300 and 21600."
  }
}

variable "e2b_repo_command_timeout_seconds" {
  description = "Maximum duration of one isolated repository command."
  type        = number
  default     = 300

  validation {
    condition     = var.e2b_repo_command_timeout_seconds >= 10 && var.e2b_repo_command_timeout_seconds <= 600
    error_message = "e2b_repo_command_timeout_seconds must be between 10 and 600."
  }
}

variable "e2b_repo_max_snapshot_bytes" {
  description = "Maximum compressed S3 snapshot size for one repository workspace."
  type        = number
  default     = 52428800

  validation {
    condition     = var.e2b_repo_max_snapshot_bytes >= 1048576 && var.e2b_repo_max_snapshot_bytes <= 104857600
    error_message = "e2b_repo_max_snapshot_bytes must be between 1 MiB and 100 MiB."
  }
}

variable "e2b_repo_max_workspaces_per_org" {
  description = "Maximum non-closed persistent repository workspaces per tenant."
  type        = number
  default     = 50
}

variable "e2b_repo_max_workspaces_per_task" {
  description = "Maximum non-closed persistent repository workspaces per task."
  type        = number
  default     = 3
}

variable "composio_api_key" {
  description = "Composio managed-auth key for production SaaS connectors."
  type        = string
  sensitive   = true
  default     = ""
}

variable "composio_entity_scope" {
  description = "Composio connection ownership: member for per-user OAuth, or org for a shared organization connection."
  type        = string
  default     = "member"

  validation {
    condition     = contains(["member", "org"], var.composio_entity_scope)
    error_message = "composio_entity_scope must be member or org."
  }
}

variable "canva_client_id" {
  description = "Canva Connect OAuth client ID. Canva uses Chronos's dedicated governed connector and is not routed through Composio."
  type        = string
  sensitive   = true
  default     = ""
}

variable "canva_client_secret" {
  description = "Canva Connect OAuth client secret."
  type        = string
  sensitive   = true
  default     = ""
}

variable "browserbase_api_key" {
  description = "Browserbase key for live browser search/operator capability."
  type        = string
  sensitive   = true
  default     = ""
}

variable "browserbase_project_id" {
  description = "Browserbase project used for encrypted Contexts and multi-replica remote operator sessions."
  type        = string
  default     = ""

  validation {
    condition     = !startswith(lower(var.environment), "prod") || trimspace(var.browserbase_project_id) != ""
    error_message = "browserbase_project_id is required for production environments."
  }
}

variable "browserbase_region" {
  description = "Browserbase region for remote browser operator sessions."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = contains(["us-west-2", "us-east-1", "eu-central-1", "ap-southeast-1"], var.browserbase_region)
    error_message = "browserbase_region must be a supported Browserbase region."
  }
}

variable "browserbase_session_timeout_seconds" {
  description = "Maximum lifetime for a reconnectable Browserbase operator session."
  type        = number
  default     = 14400

  validation {
    condition     = var.browserbase_session_timeout_seconds >= 60 && var.browserbase_session_timeout_seconds <= 21600
    error_message = "browserbase_session_timeout_seconds must be between 60 and 21600."
  }
}

variable "backup_api_key" {
  description = "Direct Anthropic API key for independent LLM failover"
  type        = string
  sensitive   = true
  default     = ""
}

variable "tavily_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "langfuse_public_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "langfuse_secret_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "sentry_dsn" {
  type      = string
  sensitive = true
  default   = ""
}

variable "stripe_secret_key" {
  description = "Stripe server API key. Empty keeps billing truthfully disabled."
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_webhook_secret" {
  description = "Stripe webhook signing secret."
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_price_pro" {
  description = "Stripe price ID for the Pro plan."
  type        = string
  default     = ""
}

variable "stripe_price_enterprise" {
  description = "Stripe price ID for the Enterprise plan."
  type        = string
  default     = ""
}

variable "notification_from_email" {
  description = "Verified sender used for transactional notifications."
  type        = string
  default     = ""
}

variable "langfuse_host" {
  description = "Credential-safe HTTPS Langfuse Cloud or self-hosted origin."
  type        = string
  default     = "https://cloud.langfuse.com"

  validation {
    condition     = !startswith(lower(var.environment), "prod") || can(regex("^https://([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\\.)+[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?/?$", trimspace(var.langfuse_host)))
    error_message = "langfuse_host must be an HTTPS DNS origin without credentials, port, path, query, or fragment."
  }
}

# ── GitHub OIDC (for the deploy role) ────────────────────────────────────────
variable "github_org" {
  description = "GitHub org/user that owns the repo (e.g. shaheerasif8008-cmyk). Leave empty to skip OIDC role."
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "GitHub repository name (e.g. Chronos)"
  type        = string
  default     = "Chronos"
}

variable "github_oidc_provider_arn" {
  description = "Existing GitHub Actions OIDC provider ARN. Leave empty only in a new account where Terraform should create it."
  type        = string
  default     = ""
}

# ═══════════════════════════════════════════════════════════════════════════════
# Runtime / app configuration (sourced from .env)
# ═══════════════════════════════════════════════════════════════════════════════

variable "org_id" {
  description = "Default organization ID"
  type        = string
  default     = "default"
}

variable "auth_region" {
  description = "Region label for auth/org scoping"
  type        = string
  default     = "us"
}

variable "database_url" {
  description = "PostgreSQL connection string (overrides auto-built RDS URL when set)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "redis_url" {
  description = "Redis connection string (overrides auto-built ElastiCache URL when set)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "local_llm_base_url" {
  description = "Base URL for local LLM (Ollama etc.)"
  type        = string
  default     = "http://localhost:11434"
}

variable "local_llm_model" {
  description = "Default local LLM model name"
  type        = string
  default     = "llama3"
}

variable "local_llm_timeout_seconds" {
  description = "Timeout for local LLM requests"
  type        = number
  default     = 0.1
}

variable "task_runner_timeout_seconds" {
  description = "Max duration for a single task run"
  type        = number
  default     = 1800
}

variable "task_runner_max_concurrency" {
  description = "Max concurrent task executions per runner"
  type        = number
  default     = 4
}

variable "task_runner_max_attempts" {
  description = "Retry attempts for failed steps"
  type        = number
  default     = 2
}

variable "backup_model" {
  description = "Direct-provider model used when OpenRouter is unavailable"
  type        = string
  default     = "anthropic/claude-sonnet-5"

  validation {
    condition     = trimspace(var.backup_model) != "" && !startswith(lower(trimspace(var.backup_model)), "openrouter/")
    error_message = "backup_model must be a non-empty direct-provider model, not an OpenRouter route."
  }
}

variable "openrouter_model" {
  description = "Primary OpenRouter model identifier"
  type        = string
  default     = "openrouter/openai/gpt-5.4-mini"
}

variable "agent_model" {
  description = "Model identifier for autonomous agent calls"
  type        = string
  default     = "openrouter/deepseek/deepseek-v4-pro"
}

variable "openrouter_api_base" {
  description = "Credential-safe production OpenRouter API base URL."
  type        = string
  default     = "https://openrouter.ai/api/v1"

  validation {
    condition     = !startswith(lower(var.environment), "prod") || can(regex("^https://openrouter\\.ai/api/v1/?$", trimspace(var.openrouter_api_base)))
    error_message = "openrouter_api_base must be https://openrouter.ai/api/v1 so the OpenRouter key cannot be sent to another origin."
  }
}

variable "embedding_model" {
  description = "Model used for generating text embeddings"
  type        = string
  default     = "google/gemini-embedding-2"
}

variable "embedding_dimensions" {
  description = "Output dimensions for embedding vectors"
  type        = number
  default     = 1536
}

variable "fast_model" {
  description = "Cheap/fast model for routing and extraction"
  type        = string
  default     = "openrouter/openai/gpt-5.4-nano"
}

variable "vision_model" {
  description = "Vision-capable model for OCR"
  type        = string
  default     = "openrouter/openai/gpt-4o-mini"
}

variable "image_model" {
  description = "Image generation and full-image editing model"
  type        = string
  default     = "openrouter/google/gemini-3.1-flash-image"
}

variable "stt_model" {
  description = "Speech-to-text model"
  type        = string
  default     = "openrouter/openai/gpt-4o-mini-transcribe"
}

variable "tts_model" {
  description = "Text-to-speech model"
  type        = string
  default     = "openrouter/x-ai/grok-voice-tts-1.0"
}

variable "demo_mode" {
  description = "Use fixture connector data instead of real OAuth connectors"
  type        = bool
  default     = false
}

variable "per_org_daily_token_limit" {
  description = "Hard daily token ceiling per organization. Zero is rejected for production plans."
  type        = number
  default     = 2000000

  validation {
    condition     = var.per_org_daily_token_limit > 0
    error_message = "per_org_daily_token_limit must be greater than zero for production."
  }
}

variable "access_token_expire_minutes" {
  description = "JWT access token TTL in minutes; production permits 5 minutes through 24 hours."
  type        = number
  default     = 60

  validation {
    condition     = !startswith(lower(var.environment), "prod") || (var.access_token_expire_minutes >= 5 && var.access_token_expire_minutes <= 1440)
    error_message = "access_token_expire_minutes must be between 5 and 1440."
  }
}

variable "cognito_region" {
  description = "AWS region for Cognito user pool"
  type        = string
  default     = "us-east-1"
}

variable "cognito_callback_url" {
  description = "Cognito OAuth callback URL"
  type        = string
  default     = "http://localhost:3000/login/callback"
}

variable "cognito_auto_provision_members" {
  description = "Auto-create member records on first Cognito login"
  type        = bool
  default     = true
}

variable "terms_url" {
  description = "HTTPS URL of the owner-approved Chronos Terms of Service."
  type        = string
  validation {
    condition     = can(regex("^https://", var.terms_url))
    error_message = "terms_url must be an approved HTTPS URL."
  }
}

variable "privacy_url" {
  description = "HTTPS URL of the owner-approved Chronos privacy notice."
  type        = string
  validation {
    condition     = can(regex("^https://", var.privacy_url))
    error_message = "privacy_url must be an approved HTTPS URL."
  }
}

variable "support_url" {
  description = "HTTPS URL of the monitored client support channel."
  type        = string
  validation {
    condition     = can(regex("^https://", var.support_url))
    error_message = "support_url must use HTTPS."
  }
}

variable "status_url" {
  description = "HTTPS URL of the public service-status page."
  type        = string
  validation {
    condition     = can(regex("^https://", var.status_url))
    error_message = "status_url must use HTTPS."
  }
}

variable "artifact_share_ttl_hours" {
  description = "Maximum public artifact bearer-link lifetime in hours."
  type        = number
  default     = 168
  validation {
    condition     = var.artifact_share_ttl_hours >= 1 && var.artifact_share_ttl_hours <= 720
    error_message = "artifact_share_ttl_hours must be between 1 and 720."
  }
}

variable "google_client_id" {
  description = "Google OAuth2 client ID for Gmail/Calendar/Drive connectors"
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_client_secret" {
  description = "Google OAuth2 client secret"
  type        = string
  sensitive   = true
  default     = ""
}

variable "slack_client_id" {
  description = "Slack OAuth client ID for workspace-scoped agent publications."
  type        = string
  sensitive   = true
  default     = ""
}

variable "slack_client_secret" {
  description = "Slack OAuth client secret for workspace-scoped agent publications."
  type        = string
  sensitive   = true
  default     = ""
}

variable "slack_signing_secret" {
  description = "Slack signing secret used to verify public agent event callbacks."
  type        = string
  sensitive   = true
  default     = ""
}

variable "microsoft_client_id" {
  description = "Microsoft OAuth client ID used by Teams and Microsoft connectors."
  type        = string
  sensitive   = true
  default     = ""
}

variable "microsoft_client_secret" {
  description = "Microsoft OAuth client secret used by Teams and Microsoft connectors."
  type        = string
  sensitive   = true
  default     = ""
}

variable "teams_bot_app_id" {
  description = "Microsoft Bot Framework application ID accepted by the Teams publication webhook."
  type        = string
  sensitive   = true
  default     = ""
}

variable "sendgrid_inbound_public_key" {
  description = "PEM ECDSA public key used to verify SendGrid Inbound Parse webhook signatures."
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_client_id" {
  description = "GitHub OAuth App client ID used for member-scoped private repository imports."
  type        = string
  sensitive   = true
  default     = ""
}

variable "github_client_secret" {
  description = "GitHub OAuth App client secret used for member-scoped private repository imports."
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_redirect_uri" {
  description = "Google OAuth2 redirect URI"
  type        = string
  default     = "http://localhost:8000/connectors/gmail/oauth-callback"
}

variable "object_storage_backend" {
  description = "Object storage backend (s3 or local)"
  type        = string
  default     = "s3"
}

variable "aws_s3_bucket" {
  description = "S3 bucket for object storage"
  type        = string
  default     = "chronos-dev"
}

variable "aws_s3_region" {
  description = "AWS region for the S3 bucket"
  type        = string
  default     = "us-east-1"
}

variable "aws_s3_endpoint" {
  description = "Custom S3 endpoint (for MinIO or compatible). Empty = default AWS endpoint."
  type        = string
  default     = ""
}

variable "aws_access_key_id" {
  description = "AWS access key for S3"
  type        = string
  sensitive   = true
  default     = ""
}

variable "aws_secret_access_key" {
  description = "AWS secret key for S3"
  type        = string
  sensitive   = true
  default     = ""
}

variable "aws_session_token" {
  description = "AWS session token (for temp credentials)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "openfga_api_url" {
  description = "OpenFGA API URL for permission enforcement"
  type        = string
  default     = ""
}

variable "permissions_enforce" {
  description = "Enable real OpenFGA permission checks (vs allow-all stub)"
  type        = bool
  default     = true
}
