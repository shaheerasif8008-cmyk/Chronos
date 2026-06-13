resource "aws_ecs_cluster" "main" {
  name = "${local.prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# ── CloudWatch log groups ─────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.prefix}/api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${local.prefix}/web"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "openfga" {
  name              = "/ecs/${local.prefix}/openfga"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/ecs/${local.prefix}/migrate"
  retention_in_days = 14
}

# ── API task definition ───────────────────────────────────────────────────────
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
    essential = true

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    environment = [
      { name = "ORG_ID", value = "default" },
      { name = "REGION", value = var.aws_region },
      { name = "AUTH_PROVIDER", value = var.auth_provider },
      { name = "FRONTEND_BASE_URL", value = var.domain_name != "" ? "https://${var.domain_name}" : "http://${aws_lb.web.dns_name}" },
      { name = "OAUTH_CALLBACK_BASE_URL", value = var.domain_name != "" ? "https://api.${var.domain_name}" : "http://${aws_lb.api.dns_name}" },
      { name = "COGNITO_REGION", value = var.cognito_region },
      { name = "COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
      { name = "COGNITO_APP_CLIENT_ID", value = var.cognito_app_client_id },
      { name = "COGNITO_DOMAIN", value = var.cognito_domain },
      { name = "COGNITO_CALLBACK_URL", value = var.domain_name != "" ? "https://${var.domain_name}/login/callback" : "http://${aws_lb.web.dns_name}/login/callback" },
      { name = "DB_SSL_MODE", value = "require" },
      { name = "OBJECT_STORAGE_BACKEND", value = "s3" },
      { name = "AWS_S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      { name = "AWS_S3_REGION", value = var.aws_region },
      { name = "AWS_S3_ENDPOINT", value = "" },
      { name = "OPENFGA_API_URL", value = "http://${aws_service_discovery_service.openfga.name}.${aws_service_discovery_private_dns_namespace.main.name}:8080" },
      { name = "PERMISSIONS_ENFORCE", value = "true" },
      { name = "TASK_RUNNER_MAX_CONCURRENCY", value = "4" },
      { name = "TASK_RUNNER_MAX_ATTEMPTS", value = "2" },
      { name = "TASK_RUNNER_TIMEOUT_SECONDS", value = "1800" },
      { name = "OPENROUTER_MODEL", value = "openrouter/deepseek/deepseek-v4-flash:free" },
      { name = "AGENT_MODEL", value = "openrouter/deepseek/deepseek-v4-flash:free" },
      { name = "FAST_MODEL", value = "openrouter/nvidia/nemotron-3-super-120b-a12b:free" },
      { name = "BACKUP_MODEL", value = "openrouter/minimax/minimax-m2.5:free" },
      { name = "OPENROUTER_API_BASE", value = "https://openrouter.ai/api/v1" },
      { name = "EMBEDDING_MODEL", value = "google/gemini-embedding-2" },
      { name = "EMBEDDING_DIMENSIONS", value = "1536" },
      { name = "ACCESS_TOKEN_EXPIRE_MINUTES", value = "60" },
    ]

    # Secrets are fetched from Secrets Manager at task startup — values never
    # appear in task definition JSON or CloudWatch logs.
    secrets = concat(local.task_secrets, [
      {
        name = "DATABASE_URL"
        # Build asyncpg URL from known host + secret password at deploy time.
        # The actual value is set by the deploy workflow which calls
        # `aws secretsmanager create-secret --name chronos-prod/database_url`.
        valueFrom = aws_secretsmanager_secret.database_url.arn
      },
      {
        name      = "REDIS_URL"
        valueFrom = aws_secretsmanager_secret.redis_url.arn
      },
    ])

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "api"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -sf http://localhost:8000/health || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
  }])
}

# ── Web task definition ───────────────────────────────────────────────────────
resource "aws_ecs_task_definition" "web" {
  family                   = "${local.prefix}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web_cpu
  memory                   = var.web_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "web"
    image     = "${aws_ecr_repository.web.repository_url}:${var.web_image_tag}"
    essential = true

    portMappings = [{ containerPort = 3000, protocol = "tcp" }]

    environment = [
      { name = "NODE_ENV", value = "production" },
      { name = "NEXT_PUBLIC_API_BASE_URL", value = var.domain_name != "" ? "https://api.${var.domain_name}" : "http://${aws_lb.api.dns_name}" },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.web.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "web"
      }
    }
  }])
}

# ── OpenFGA task definition ───────────────────────────────────────────────────
resource "aws_ecs_task_definition" "openfga" {
  family                   = "${local.prefix}-openfga"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.openfga_cpu
  memory                   = var.openfga_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "openfga"
    image     = "openfga/openfga:latest"
    essential = true
    command   = ["run"]

    portMappings = [{ containerPort = 8080, protocol = "tcp" }]

    environment = [
      { name = "OPENFGA_DATASTORE_ENGINE", value = "postgres" },
      { name = "OPENFGA_LOG_LEVEL", value = "warn" },
    ]

    secrets = [{
      name      = "OPENFGA_DATASTORE_URI"
      valueFrom = aws_secretsmanager_secret.openfga_database_url.arn
    }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.openfga.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "openfga"
      }
    }
  }])
}

# ── Migration task definition (one-off, run by deploy workflow) ───────────────
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.prefix}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "migrate"
    image     = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
    essential = true
    command   = ["sh", "-c", "alembic upgrade head && python seed.py --skip-if-exists"]

    environment = [
      { name = "ORG_ID", value = "default" },
      { name = "REGION", value = var.aws_region },
      { name = "OBJECT_STORAGE_BACKEND", value = "s3" },
      { name = "AWS_S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
      { name = "AWS_S3_REGION", value = var.aws_region },
      { name = "AUTH_PROVIDER", value = "dev_otp" },
      { name = "FRONTEND_BASE_URL", value = var.domain_name != "" ? "https://${var.domain_name}" : "http://${aws_lb.web.dns_name}" },
      { name = "OAUTH_CALLBACK_BASE_URL", value = var.domain_name != "" ? "https://api.${var.domain_name}" : "http://${aws_lb.api.dns_name}" },
    ]

    secrets = concat(local.task_secrets, [
      { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
    ])

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.migrate.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "migrate"
      }
    }
  }])
}

# ── ECS services ──────────────────────────────────────────────────────────────
resource "aws_ecs_service" "api" {
  name            = "${local.prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  service_registries {
    registry_arn = aws_service_discovery_service.api.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.api_http]
}

resource "aws_ecs_service" "web" {
  name            = "${local.prefix}-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = var.web_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.web.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 3000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.web_http]
}

resource "aws_ecs_service" "openfga" {
  name            = "${local.prefix}-openfga"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.openfga.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.openfga.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.openfga.arn
  }

  lifecycle {
    ignore_changes = [task_definition]
  }
}

# ── Auto-scaling for the API service ─────────────────────────────────────────
resource "aws_appautoscaling_target" "api" {
  max_capacity       = var.api_max_count
  min_capacity       = var.api_min_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.prefix}-api-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 70
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# ── Service discovery (internal DNS for container-to-container calls) ─────────
resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${local.prefix}.local"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "api" {
  name = "api"
  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"
    dns_records {
      ttl  = 10
      type = "A"
    }
  }
  health_check_custom_config { failure_threshold = 1 }
}

resource "aws_service_discovery_service" "openfga" {
  name = "openfga"
  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"
    dns_records {
      ttl  = 10
      type = "A"
    }
  }
  health_check_custom_config { failure_threshold = 1 }
}

# ── Computed connection string secrets ────────────────────────────────────────
# These are written by the deploy workflow after `terraform apply`, since they
# reference runtime values (RDS endpoint, ElastiCache endpoint). Terraform
# creates the secret shells; the workflow fills in the values.

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${local.prefix}/database_url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${local.prefix}/redis_url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "openfga_database_url" {
  name                    = "${local.prefix}/openfga_database_url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "openfga_database_url" {
  secret_id     = aws_secretsmanager_secret.openfga_database_url.id
  secret_string = "postgres://chronos:${urlencode(random_password.db.result)}@${aws_db_instance.main.address}:5432/chronos"
}
