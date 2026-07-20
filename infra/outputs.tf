output "api_alb_dns" {
  description = "API ALB DNS name — point api.yourdomain.com CNAME here"
  value       = aws_lb.api.dns_name
}

output "web_alb_dns" {
  description = "Web ALB DNS name — point app.yourdomain.com CNAME here"
  value       = aws_lb.web.dns_name
}

output "tenant_web_dns_record" {
  description = "Create a wildcard CNAME (*.<domain_name>) pointing at this web ALB DNS name."
  value       = var.domain_name != "" ? "*.${var.domain_name} -> ${aws_lb.web.dns_name}" : null
}

output "ecr_api_url" {
  description = "ECR repository URL for the API image"
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_web_url" {
  description = "ECR repository URL for the web image"
  value       = aws_ecr_repository.web.repository_url
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "aws_region" {
  value = var.aws_region
}

output "api_service_name" {
  value = aws_ecs_service.api.name
}

output "web_service_name" {
  value = aws_ecs_service.web.name
}

output "worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "worker_task_definition_family" {
  value = aws_ecs_task_definition.worker.family
}

output "migrate_task_definition_family" {
  description = "Task definition family for the one-off migration task"
  value       = aws_ecs_task_definition.migrate.family
}

output "openfga_migrate_task_definition_family" {
  description = "Task definition family for the one-off OpenFGA datastore migration."
  value       = aws_ecs_task_definition.openfga_migrate.family
}

output "openfga_security_group_id" {
  description = "Security group used by the one-off OpenFGA migration task."
  value       = aws_security_group.openfga.id
}

output "api_security_group_id" {
  description = "Security group used by the one-off Chronos application migration task."
  value       = aws_security_group.api.id
}

output "s3_artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "rds_endpoint" {
  description = "RDS Postgres endpoint (host only)"
  value       = aws_db_instance.main.address
  sensitive   = true
}

output "openfga_rds_endpoint" {
  description = "Dedicated OpenFGA RDS PostgreSQL endpoint (host only)."
  value       = aws_db_instance.openfga.address
  sensitive   = true
}

output "redis_primary_endpoint" {
  description = "ElastiCache Redis primary endpoint"
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
  sensitive   = true
}

output "database_url_secret_arn" {
  description = "ARN of the Secrets Manager secret for DATABASE_URL"
  value       = aws_secretsmanager_secret.database_url.arn
}

output "redis_url_secret_arn" {
  description = "ARN of the Secrets Manager secret for REDIS_URL"
  value       = aws_secretsmanager_secret.redis_url.arn
}

output "openfga_database_url_secret_arn" {
  description = "ARN of the Region-specific dedicated OpenFGA DATABASE_URL secret."
  value       = aws_secretsmanager_secret.openfga_database_url.arn
}

output "github_deploy_role_arn" {
  description = "IAM role ARN for GitHub Actions OIDC deploys"
  value       = var.github_org != "" ? aws_iam_role.github_deploy[0].arn : null
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "operations_sns_topic_arn" {
  description = "Incident notification topic. Verify the email subscription is Confirmed before launch."
  value       = aws_sns_topic.operations.arn
}

output "operations_dashboard_name" {
  value = aws_cloudwatch_dashboard.operations.dashboard_name
}

output "audit_log_bucket_name" {
  description = "COMPLIANCE-locked CloudTrail evidence bucket."
  value       = var.account_security_services_enabled ? aws_s3_bucket.audit_logs[0].bucket : null
}

output "config_log_bucket_name" {
  description = "KMS-encrypted, versioned AWS Config delivery bucket. AWS Config does not support Object Lock default retention."
  value       = var.account_security_services_enabled ? aws_s3_bucket.config_logs[0].bucket : null
}

output "cloudtrail_name" {
  value = var.account_security_services_enabled ? aws_cloudtrail.management[0].name : null
}

output "access_analyzer_arn" {
  value = var.account_security_services_enabled ? aws_accessanalyzer_analyzer.main[0].arn : null
}

output "primary_backup_vault_name" {
  value = var.backups_enabled ? aws_backup_vault.primary[0].name : null
}

output "cross_region_backup_vault_name" {
  value = var.backups_enabled ? aws_backup_vault.dr[0].name : null
}

output "restore_testing_plan_name" {
  description = "Monthly AWS Backup restore-test plan; inspect every run and complete the separate application-level rehearsal in the DR runbook."
  value       = var.backups_enabled && var.automated_restore_testing_enabled ? aws_backup_restore_testing_plan.monthly[0].name : null
}

output "restore_rehearsal_active" {
  description = "True only for the fail-closed, internal restore rehearsal stack. It is not approval to cut over production traffic."
  value       = var.restore_rehearsal_mode
}

output "restore_rehearsal_access" {
  description = "Internal-only endpoints and operator CIDRs for restore evidence collection. Empty CIDRs intentionally make the ALBs unreachable."
  value = var.restore_rehearsal_mode ? {
    api_internal_dns = aws_lb.api.dns_name
    web_internal_dns = aws_lb.web.dns_name
    ingress_cidrs    = var.restore_rehearsal_ingress_cidrs
  } : null
}

output "platform_bootstrap_active" {
  description = "Must be false before production traffic is enabled."
  value       = var.platform_bootstrap_mode
}
