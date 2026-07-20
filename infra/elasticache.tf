resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.prefix}-redis-subnet-group"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_parameter_group" "main" {
  name   = "${local.prefix}-redis7"
  family = "redis7"

  # Queue, idempotency, rate-limit, and worker coordination keys must never be
  # silently evicted. Explicit write failures trigger alarms/recovery instead.
  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }
}

resource "random_password" "redis_auth" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "redis_auth_token" {
  name                    = "${local.prefix}/redis/auth_token"
  recovery_window_in_days = 7

  dynamic "replica" {
    for_each = var.backups_enabled ? [var.backup_copy_region] : []
    content {
      region     = replica.value
      kms_key_id = aws_kms_key.dr[0].arn
    }
  }
}

resource "aws_secretsmanager_secret_version" "redis_auth_token" {
  secret_id     = aws_secretsmanager_secret.redis_auth_token.id
  secret_string = random_password.redis_auth.result
}

resource "aws_cloudwatch_log_group" "redis_slow" {
  name              = "/aws/elasticache/${local.prefix}/slow-log"
  retention_in_days = var.application_log_retention_days
}

resource "aws_cloudwatch_log_group" "redis_engine" {
  name              = "/aws/elasticache/${local.prefix}/engine-log"
  retention_in_days = var.application_log_retention_days
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${local.prefix}-redis"
  description          = "Chronos Redis - pubsub + cache"
  snapshot_name        = trimspace(var.restore_redis_snapshot_name) != "" ? var.restore_redis_snapshot_name : null

  node_type          = var.redis_node_type
  num_cache_clusters = var.redis_num_cache_clusters
  port               = 6379

  subnet_group_name  = aws_elasticache_subnet_group.main.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  auth_token                 = random_password.redis_auth.result
  auth_token_update_strategy = "ROTATE"
  # TLS requires the Redis URL to use rediss:// — the API picks this up
  # automatically when REDIS_URL starts with rediss://.

  automatic_failover_enabled = var.redis_automatic_failover_enabled
  multi_az_enabled           = var.redis_multi_az_enabled

  engine_version             = "7.1"
  parameter_group_name       = aws_elasticache_parameter_group.main.name
  auto_minor_version_upgrade = true

  maintenance_window       = "sun:05:00-sun:06:00"
  snapshot_retention_limit = var.redis_snapshot_retention_days
  snapshot_window          = "04:00-05:00"

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis_slow.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis_engine.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "engine-log"
  }

  apply_immediately = false

  lifecycle {
    # A snapshot is only a creation seed. Retaining it in configuration and
    # ignoring later edits prevents stale-snapshot replacement after validation.
    ignore_changes = [snapshot_name]
  }
}
