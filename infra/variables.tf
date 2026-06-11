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
