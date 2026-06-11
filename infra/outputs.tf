output "api_alb_dns" {
  description = "API ALB DNS name — point api.yourdomain.com CNAME here"
  value       = aws_lb.api.dns_name
}

output "web_alb_dns" {
  description = "Web ALB DNS name — point app.yourdomain.com CNAME here"
  value       = aws_lb.web.dns_name
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

output "api_service_name" {
  value = aws_ecs_service.api.name
}

output "web_service_name" {
  value = aws_ecs_service.web.name
}

output "migrate_task_definition_family" {
  description = "Task definition family for the one-off migration task"
  value       = aws_ecs_task_definition.migrate.family
}

output "s3_artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "rds_endpoint" {
  description = "RDS Postgres endpoint (host only)"
  value       = aws_db_instance.main.address
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

output "github_deploy_role_arn" {
  description = "IAM role ARN for GitHub Actions OIDC deploys"
  value       = var.github_org != "" ? aws_iam_role.github_deploy[0].arn : "not created"
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}
