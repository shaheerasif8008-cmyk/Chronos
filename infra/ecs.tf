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
  retention_in_days = var.application_log_retention_days
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${local.prefix}/web"
  retention_in_days = var.web_log_retention_days
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.prefix}/worker"
  retention_in_days = var.application_log_retention_days
}

resource "aws_cloudwatch_log_group" "openfga" {
  name              = "/ecs/${local.prefix}/openfga"
  retention_in_days = var.application_log_retention_days
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/ecs/${local.prefix}/migrate"
  retention_in_days = var.application_log_retention_days
}

# Keep the API, queue worker, and one-off migration task on the same validated
# production configuration. Settings is imported by every entrypoint, so a
# partial environment can otherwise make only one of the three fail at boot.
locals {
  runtime_environment = [
    { name = "ENVIRONMENT", value = "production" },
    { name = "ORG_ID", value = var.org_id },
    { name = "REGION", value = var.auth_region },
    { name = "BASE_DOMAIN", value = var.domain_name },
    { name = "AUTH_PROVIDER", value = var.auth_provider },
    { name = "FRONTEND_BASE_URL", value = local.web_origin },
    { name = "TERMS_URL", value = var.terms_url },
    { name = "PRIVACY_URL", value = var.privacy_url },
    { name = "SUPPORT_URL", value = var.support_url },
    { name = "STATUS_URL", value = var.status_url },
    { name = "ARTIFACT_SHARE_TTL_HOURS", value = tostring(var.artifact_share_ttl_hours) },
    { name = "OAUTH_CALLBACK_BASE_URL", value = local.api_origin },
    { name = "COMPOSIO_CALLBACK_BASE_URL", value = local.api_origin },
    { name = "COMPOSIO_ENTITY_SCOPE", value = var.composio_entity_scope },
    { name = "BROWSERBASE_OPERATOR_ENABLED", value = "true" },
    { name = "BROWSERBASE_PROJECT_ID", value = var.browserbase_project_id },
    { name = "BROWSERBASE_REGION", value = var.browserbase_region },
    { name = "BROWSERBASE_SESSION_TIMEOUT_SECONDS", value = tostring(var.browserbase_session_timeout_seconds) },
    { name = "GOOGLE_REDIRECT_URI", value = "${local.api_origin}/connectors/gmail/oauth-callback" },
    { name = "COGNITO_REGION", value = var.cognito_region },
    { name = "COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
    { name = "COGNITO_APP_CLIENT_ID", value = var.cognito_app_client_id },
    { name = "COGNITO_DOMAIN", value = var.cognito_domain },
    { name = "COGNITO_ISSUER_URL", value = var.cognito_issuer_url },
    { name = "COGNITO_JWKS_URL", value = var.cognito_jwks_url },
    { name = "SSO_ENDPOINT_HOST_ALLOWLIST", value = var.sso_endpoint_host_allowlist },
    { name = "COGNITO_CALLBACK_URL", value = "${local.web_origin}/login/callback" },
    { name = "COGNITO_AUTO_PROVISION_MEMBERS", value = tostring(var.cognito_auto_provision_members) },
    { name = "ACCESS_TOKEN_EXPIRE_MINUTES", value = tostring(var.access_token_expire_minutes) },
    { name = "ENFORCE_ORG_BOUND_TOKENS", value = "true" },
    { name = "DB_SSL_MODE", value = "require" },
    { name = "OBJECT_STORAGE_BACKEND", value = "s3" },
    { name = "AWS_S3_BUCKET", value = aws_s3_bucket.artifacts.bucket },
    { name = "AWS_S3_REGION", value = var.aws_region },
    { name = "AWS_S3_ENDPOINT", value = "" },
    { name = "OPENFGA_API_URL", value = "http://${aws_service_discovery_service.openfga.name}.${aws_service_discovery_private_dns_namespace.main.name}:8080" },
    { name = "PERMISSIONS_ENFORCE", value = tostring(var.permissions_enforce) },
    { name = "DEMO_MODE", value = tostring(var.demo_mode) },
    { name = "TASK_RUNNER_MAX_CONCURRENCY", value = tostring(var.task_runner_max_concurrency) },
    { name = "TASK_RUNNER_MAX_ATTEMPTS", value = tostring(var.task_runner_max_attempts) },
    { name = "TASK_RUNNER_TIMEOUT_SECONDS", value = tostring(var.task_runner_timeout_seconds) },
    { name = "E2B_TEMPLATE_ID", value = var.e2b_template_id },
    { name = "E2B_SANDBOX_TIMEOUT_SECONDS", value = tostring(var.e2b_sandbox_timeout_seconds) },
    { name = "E2B_ALLOW_INTERNET_ACCESS", value = "false" },
    { name = "E2B_COMPUTER_ALLOW_INTERNET_ACCESS", value = tostring(var.e2b_computer_allow_internet_access) },
    { name = "E2B_COMPUTER_EGRESS_ALLOWLIST", value = var.e2b_computer_egress_allowlist },
    { name = "E2B_COMPUTER_TEMPLATE_ID", value = var.e2b_computer_template_id },
    { name = "E2B_COMPUTER_IDLE_TIMEOUT_SECONDS", value = tostring(var.e2b_computer_idle_timeout_seconds) },
    { name = "E2B_COMPUTER_MAX_SESSION_SECONDS", value = tostring(var.e2b_computer_max_session_seconds) },
    { name = "E2B_COMPUTER_MAX_ACTIVE_PER_MEMBER", value = tostring(var.e2b_computer_max_active_per_member) },
    { name = "E2B_COMPUTER_MAX_ACTIVE_PER_ORG", value = tostring(var.e2b_computer_max_active_per_org) },
    { name = "E2B_COMPUTER_SCREEN_WIDTH", value = tostring(var.e2b_computer_screen_width) },
    { name = "E2B_COMPUTER_SCREEN_HEIGHT", value = tostring(var.e2b_computer_screen_height) },
    { name = "E2B_REPO_ENABLED", value = tostring(var.e2b_repo_enabled) },
    { name = "E2B_REPO_TEMPLATE_ID", value = var.e2b_repo_template_id },
    { name = "E2B_REPO_ALLOW_INTERNET_ACCESS", value = tostring(var.e2b_repo_allow_internet_access) },
    { name = "E2B_REPO_EGRESS_ALLOWLIST", value = var.e2b_repo_egress_allowlist },
    { name = "E2B_REPO_TIMEOUT_SECONDS", value = tostring(var.e2b_repo_timeout_seconds) },
    { name = "E2B_REPO_COMMAND_TIMEOUT_SECONDS", value = tostring(var.e2b_repo_command_timeout_seconds) },
    { name = "E2B_REPO_MAX_SNAPSHOT_BYTES", value = tostring(var.e2b_repo_max_snapshot_bytes) },
    { name = "E2B_REPO_MAX_WORKSPACES_PER_ORG", value = tostring(var.e2b_repo_max_workspaces_per_org) },
    { name = "E2B_REPO_MAX_WORKSPACES_PER_TASK", value = tostring(var.e2b_repo_max_workspaces_per_task) },
    { name = "MALWARE_SCAN_REQUIRED", value = "true" },
    { name = "CLAMAV_HOST", value = "127.0.0.1" },
    { name = "CLAMAV_PORT", value = "3310" },
    { name = "CLAMAV_TIMEOUT_SECONDS", value = "20" },
    { name = "CLAMAV_MAX_BYTES", value = "52428800" },
    { name = "CLAMAV_MAX_SIGNATURE_AGE_HOURS", value = "48" },
    { name = "OPENROUTER_MODEL", value = var.openrouter_model },
    { name = "AGENT_MODEL", value = var.agent_model },
    { name = "FAST_MODEL", value = var.fast_model },
    { name = "BACKUP_MODEL", value = var.backup_model },
    { name = "OPENROUTER_API_BASE", value = var.openrouter_api_base },
    { name = "EMBEDDING_MODEL", value = var.embedding_model },
    { name = "EMBEDDING_DIMENSIONS", value = tostring(var.embedding_dimensions) },
    { name = "VISION_MODEL", value = var.vision_model },
    { name = "IMAGE_MODEL", value = var.image_model },
    { name = "STT_MODEL", value = var.stt_model },
    { name = "TTS_MODEL", value = var.tts_model },
    { name = "PER_ORG_DAILY_TOKEN_LIMIT", value = tostring(var.per_org_daily_token_limit) },
    { name = "STRIPE_PRICE_PRO", value = var.stripe_price_pro },
    { name = "STRIPE_PRICE_ENTERPRISE", value = var.stripe_price_enterprise },
    { name = "NOTIFICATION_FROM_EMAIL", value = var.notification_from_email },
    { name = "LANGFUSE_HOST", value = var.langfuse_host },
  ]

  runtime_secrets = concat(local.task_secrets, [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
  ])
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
    name              = "api"
    image             = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
    essential         = true
    stopTimeout       = 90
    cpu               = 768
    memoryReservation = 1024

    dependsOn = [{
      containerName = "clamav"
      condition     = "HEALTHY"
    }]

    portMappings = [{ containerPort = 8000, protocol = "tcp" }]

    linuxParameters = {
      initProcessEnabled = true
    }

    environment = local.runtime_environment

    # Secrets are fetched from Secrets Manager at task startup — values never
    # appear in task definition JSON or CloudWatch logs.
    secrets = local.runtime_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "api"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -sf http://localhost:8000/health/live || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 60
    }
    }, {
    name              = "clamav"
    image             = var.clamav_image
    essential         = true
    stopTimeout       = 60
    cpu               = 256
    memoryReservation = 1536

    portMappings = [{ containerPort = 3310, protocol = "tcp" }]

    linuxParameters = {
      initProcessEnabled = true
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "clamav"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "clamdscan --ping=1 >/dev/null 2>&1 || exit 1"]
      interval    = 30
      timeout     = 10
      retries     = 5
      startPeriod = 180
    }
  }])

  lifecycle {
    precondition {
      condition     = !local.is_production || var.auth_provider == "cognito"
      error_message = "Production ECS tasks require auth_provider=cognito; dev_otp and both are rejected by the application startup guard."
    }
  }
}

# ── Web task definition ───────────────────────────────────────────────────────
resource "aws_ecs_task_definition" "web" {
  family                   = "${local.prefix}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web_cpu
  memory                   = var.web_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_restricted_task.arn

  container_definitions = jsonencode([{
    name      = "web"
    image     = "${aws_ecr_repository.web.repository_url}:${var.web_image_tag}"
    essential = true

    portMappings = [{ containerPort = 3000, protocol = "tcp" }]

    linuxParameters = {
      initProcessEnabled = true
    }

    environment = [
      { name = "NODE_ENV", value = "production" },
      { name = "NEXT_PUBLIC_API_BASE_URL", value = local.api_origin },
    ]

    secrets = nonsensitive(trimspace(var.sentry_dsn) != "") ? [{
      name      = "SENTRY_DSN"
      valueFrom = aws_secretsmanager_secret.app["sentry_dsn"].arn
    }] : []

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

# ── Connector queue worker task definition ───────────────────────────────────
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
    essential   = true
    command     = ["python", "-m", "connectors.worker_main"]
    stopTimeout = 60

    linuxParameters = {
      initProcessEnabled = true
    }

    environment = local.runtime_environment
    secrets     = local.runtime_secrets

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.worker.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "worker"
      }
    }
  }])
}

# ── OpenFGA task definition ───────────────────────────────────────────────────
resource "aws_ecs_task_definition" "openfga_migrate" {
  family                   = "${local.prefix}-openfga-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_restricted_task.arn

  # OpenFGA explicitly documents datastore migration as a one-off step before
  # the server starts. Do not put this container beside every service replica:
  # two fresh tasks would race the same DDL and every restart would rerun it.
  container_definitions = jsonencode([{
    name      = "openfga-migrate"
    image     = var.openfga_image
    essential = true
    command   = ["migrate"]

    environment = [
      { name = "OPENFGA_DATASTORE_ENGINE", value = "postgres" },
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
        awslogs-stream-prefix = "migrate"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "openfga" {
  family                   = "${local.prefix}-openfga"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.openfga_cpu
  memory                   = var.openfga_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_restricted_task.arn

  container_definitions = jsonencode([{
    name        = "openfga"
    image       = var.openfga_image
    essential   = true
    command     = ["run"]
    stopTimeout = 60

    portMappings = [
      { containerPort = 8080, protocol = "tcp" },
      { containerPort = 8081, protocol = "tcp" },
    ]

    environment = [
      { name = "OPENFGA_DATASTORE_ENGINE", value = "postgres" },
      { name = "OPENFGA_DATASTORE_MAX_OPEN_CONNS", value = tostring(var.openfga_datastore_max_open_conns) },
      { name = "OPENFGA_DATASTORE_MIN_OPEN_CONNS", value = tostring(var.openfga_datastore_min_open_conns) },
      { name = "OPENFGA_DATASTORE_MAX_IDLE_CONNS", value = tostring(var.openfga_datastore_max_idle_conns) },
      { name = "OPENFGA_DATASTORE_MIN_IDLE_CONNS", value = tostring(var.openfga_datastore_min_idle_conns) },
      { name = "OPENFGA_DATASTORE_CONN_MAX_IDLE_TIME", value = var.openfga_datastore_conn_max_idle_time },
      { name = "OPENFGA_DATASTORE_CONN_MAX_LIFETIME", value = var.openfga_datastore_conn_max_lifetime },
      { name = "OPENFGA_DATASTORE_METRICS_ENABLED", value = "true" },
      { name = "OPENFGA_METRICS_ENABLED", value = "true" },
      { name = "OPENFGA_METRICS_ENABLE_RPC_HISTOGRAMS", value = "true" },
      { name = "OPENFGA_LOG_FORMAT", value = "json" },
      { name = "OPENFGA_LOG_LEVEL", value = "info" },
      { name = "OPENFGA_LOG_TIMESTAMP_FORMAT", value = "ISO8601" },
      { name = "OPENFGA_PLAYGROUND_ENABLED", value = "false" },
      { name = "OPENFGA_AUTHN_METHOD", value = "preshared" },
    ]

    secrets = [
      {
        name      = "OPENFGA_DATASTORE_URI"
        valueFrom = aws_secretsmanager_secret.openfga_database_url.arn
      },
      {
        name      = "OPENFGA_AUTHN_PRESHARED_KEYS"
        valueFrom = aws_secretsmanager_secret.app["openfga_api_token"].arn
      },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.openfga.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "openfga"
      }
    }

    # The pinned official image includes grpc_health_probe. Its health endpoint
    # verifies datastore connectivity, not merely that the Go process exists.
    healthCheck = {
      command     = ["CMD", "/usr/local/bin/grpc_health_probe", "-addr=:8081"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 45
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

    environment = local.runtime_environment
    secrets     = local.runtime_secrets

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
  name                          = "${local.prefix}-api"
  cluster                       = aws_ecs_cluster.main.id
  task_definition               = aws_ecs_task_definition.api.arn
  desired_count                 = local.api_service_count
  launch_type                   = "FARGATE"
  platform_version              = "LATEST"
  availability_zone_rebalancing = "ENABLED"
  enable_ecs_managed_tags       = true
  propagate_tags                = "TASK_DEFINITION"
  # The essential ClamAV sidecar may need several minutes on a cold task to
  # load/update signatures before ECS is allowed to start the API container.
  health_check_grace_period_seconds = 300

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

  alarms {
    alarm_names = [
      aws_cloudwatch_metric_alarm.api_target_5xx.alarm_name,
      aws_cloudwatch_metric_alarm.api_unhealthy_hosts.alarm_name,
    ]
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  lifecycle {
    # Deployments register image-pinned task definitions outside Terraform.
    # Desired count remains managed here so the bootstrap-mode 0 -> production
    # baseline transition is deterministic; autoscaling may adjust it between
    # reviewed Terraform applies.
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.api_http]
}

resource "aws_ecs_service" "web" {
  name                              = "${local.prefix}-web"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.web.arn
  desired_count                     = local.web_service_count
  launch_type                       = "FARGATE"
  platform_version                  = "LATEST"
  availability_zone_rebalancing     = "ENABLED"
  enable_ecs_managed_tags           = true
  propagate_tags                    = "TASK_DEFINITION"
  health_check_grace_period_seconds = 60

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

  alarms {
    alarm_names = [
      aws_cloudwatch_metric_alarm.web_target_5xx.alarm_name,
      aws_cloudwatch_metric_alarm.web_unhealthy_hosts.alarm_name,
    ]
    enable   = true
    rollback = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [aws_lb_listener.web_http]
}

resource "aws_ecs_service" "worker" {
  name                          = "${local.prefix}-worker"
  cluster                       = aws_ecs_cluster.main.id
  task_definition               = aws_ecs_task_definition.worker.arn
  desired_count                 = local.worker_service_count
  launch_type                   = "FARGATE"
  platform_version              = "LATEST"
  availability_zone_rebalancing = "ENABLED"
  enable_ecs_managed_tags       = true
  propagate_tags                = "TASK_DEFINITION"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.worker.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  alarms {
    alarm_names = [aws_cloudwatch_metric_alarm.worker_log_errors.alarm_name]
    enable      = true
    rollback    = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  lifecycle {
    ignore_changes = [task_definition]
  }
}

resource "aws_ecs_service" "openfga" {
  name                          = "${local.prefix}-openfga"
  cluster                       = aws_ecs_cluster.main.id
  task_definition               = aws_ecs_task_definition.openfga.arn
  desired_count                 = local.openfga_service_count
  launch_type                   = "FARGATE"
  platform_version              = "LATEST"
  availability_zone_rebalancing = "ENABLED"
  enable_ecs_managed_tags       = true
  propagate_tags                = "TASK_DEFINITION"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.openfga.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.openfga.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  alarms {
    alarm_names = [aws_cloudwatch_metric_alarm.openfga_log_errors.alarm_name]
    enable      = true
    rollback    = true
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 100

  lifecycle {
    ignore_changes = [task_definition]
  }
}

# ── Auto-scaling for the API service ─────────────────────────────────────────
resource "aws_appautoscaling_target" "api" {
  max_capacity       = var.platform_bootstrap_mode ? 1 : var.api_max_count
  min_capacity       = var.platform_bootstrap_mode ? 0 : var.api_min_count
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

resource "aws_appautoscaling_policy" "api_memory" {
  name               = "${local.prefix}-api-memory-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension
  service_namespace  = aws_appautoscaling_target.api.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 75
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageMemoryUtilization"
    }
  }
}

resource "aws_appautoscaling_target" "web" {
  max_capacity       = var.platform_bootstrap_mode ? 1 : var.web_max_count
  min_capacity       = var.platform_bootstrap_mode ? 0 : var.web_min_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.web.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "web_cpu" {
  name               = "${local.prefix}-web-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.web.resource_id
  scalable_dimension = aws_appautoscaling_target.web.scalable_dimension
  service_namespace  = aws_appautoscaling_target.web.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 65
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = var.platform_bootstrap_mode ? 1 : var.worker_max_count
  min_capacity       = var.platform_bootstrap_mode ? 0 : var.worker_min_count
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_cpu" {
  name               = "${local.prefix}-worker-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 65
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
# Terraform writes usable versions as part of the same apply that creates RDS
# and ElastiCache. ECS must never start against an empty secret shell.

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${local.prefix}/database_url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${local.prefix}/redis_url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = var.database_url != "" ? var.database_url : format(
    "postgresql+asyncpg://%s:%s@%s:5432/%s",
    var.db_username,
    urlencode(random_password.db.result),
    aws_db_instance.main.address,
    var.db_name,
  )
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id = aws_secretsmanager_secret.redis_url.id
  secret_string = var.redis_url != "" ? var.redis_url : format(
    "rediss://:%s@%s:6379/0",
    random_password.redis_auth.result,
    aws_elasticache_replication_group.main.primary_endpoint_address,
  )
}

resource "aws_secretsmanager_secret" "openfga_database_url" {
  name                    = "${local.prefix}/openfga_database_url"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "openfga_database_url" {
  secret_id     = aws_secretsmanager_secret.openfga_database_url.id
  secret_string = "postgres://${var.openfga_db_username}:${urlencode(random_password.openfga_db.result)}@${aws_db_instance.openfga.address}:5432/${var.openfga_db_name}?sslmode=require"
}
