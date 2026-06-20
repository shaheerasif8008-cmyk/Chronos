variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (prod, staging)"
  type        = string
  default     = "prod"
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
  description = "Root domain (e.g. cognisiatech.com). Leave empty to skip Route 53 + ACM."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN for HTTPS. Required when domain_name is set."
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
  default     = 2048
}

variable "web_cpu" {
  type    = number
  default = 512
}

variable "web_memory" {
  type    = number
  default = 1024
}

variable "openfga_cpu" {
  type    = number
  default = 256
}

variable "openfga_memory" {
  type    = number
  default = 512
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
  default = 1
}

variable "api_max_count" {
  type    = number
  default = 10
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
  description = "Cognito hosted-UI domain prefix (e.g. chronos-prod) or full domain URL. Required when auth_provider is cognito/both."
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

variable "backup_api_key" {
  description = "Anthropic / OpenAI API key as LLM fallback"
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
  description = "LLM model to use as API key fallback"
  type        = string
  default     = ""
}

variable "openrouter_model" {
  description = "Primary OpenRouter model identifier"
  type        = string
  default     = ""
}

variable "agent_model" {
  description = "Model identifier for autonomous agent calls"
  type        = string
  default     = ""
}

variable "openrouter_api_base" {
  description = "OpenRouter API base URL"
  type        = string
  default     = "https://openrouter.ai/api/v1"
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
  default     = ""
}

variable "vision_model" {
  description = "Vision-capable model for OCR (empty = disabled)"
  type        = string
  default     = ""
}

variable "image_model" {
  description = "Image generation model (empty = disabled)"
  type        = string
  default     = ""
}

variable "stt_model" {
  description = "Speech-to-text model (empty = disabled)"
  type        = string
  default     = ""
}

variable "tts_model" {
  description = "Text-to-speech model (empty = disabled)"
  type        = string
  default     = ""
}

variable "demo_mode" {
  description = "Use fixture connector data instead of real OAuth connectors"
  type        = bool
  default     = true
}

variable "access_token_expire_minutes" {
  description = "JWT access token TTL in minutes"
  type        = number
  default     = 60
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
  default     = false
}
