# A single encrypted incident route receives CloudWatch alarms, ECS deployment
# failures, AWS Backup failures, and ElastiCache EventBridge notifications. The
# email subscription remains PendingConfirmation until an operator confirms it;
# that confirmation is a required launch-gate check in the production runbook.

locals {
  operations_topic_arn = "arn:aws:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${local.prefix}-operations"
}

resource "aws_kms_key" "operations" {
  description             = "${local.prefix} encrypted operations notifications"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountRootAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        # EventBridge-to-encrypted-SNS does not support SourceArn/SourceAccount
        # conditions on the KMS statement. Access remains limited to the AWS
        # service principal and this one key.
        Sid       = "EventBridgePublish"
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchAlarmPublish"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:*" }
        }
      },
      {
        Sid       = "SNSMessageEncryption"
        Effect    = "Allow"
        Principal = { Service = "sns.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt"]
        Resource  = "*"
        Condition = {
          StringEquals = { "kms:EncryptionContext:aws:sns:topicArn" = local.operations_topic_arn }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "operations" {
  name          = "alias/${local.prefix}-operations"
  target_key_id = aws_kms_key.operations.key_id
}

resource "aws_sns_topic" "operations" {
  name              = "${local.prefix}-operations"
  kms_master_key_id = aws_kms_key.operations.arn
}

resource "aws_sns_topic_subscription" "operations_email" {
  count     = trimspace(var.operations_alarm_email) != "" ? 1 : 0
  topic_arn = aws_sns_topic.operations.arn
  protocol  = "email"
  endpoint  = var.operations_alarm_email
}

resource "aws_sns_topic_policy" "operations" {
  arn = aws_sns_topic.operations.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountOwnerAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "SNS:*"
        Resource  = aws_sns_topic.operations.arn
      },
      {
        Sid       = "AWSServicePublish"
        Effect    = "Allow"
        Principal = { Service = ["events.amazonaws.com", "cloudwatch.amazonaws.com"] }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.operations.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      },
    ]
  })
}

resource "aws_budgets_budget" "monthly" {
  count        = var.monthly_cost_budget_usd > 0 && trimspace(var.operations_alarm_email) != "" ? 1 : 0
  name         = "${local.prefix}-monthly-cost"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_cost_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit             = false
    include_discount           = true
    include_other_subscription = true
    include_recurring          = true
    include_refund             = false
    include_subscription       = true
    include_support            = true
    include_tax                = true
    include_upfront            = true
    use_amortized              = true
    use_blended                = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.operations_alarm_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.operations_alarm_email]
  }
}

locals {
  alarm_actions = [aws_sns_topic.operations.arn]
  ecs_services = {
    api     = "${local.prefix}-api"
    web     = "${local.prefix}-web"
    worker  = "${local.prefix}-worker"
    openfga = "${local.prefix}-openfga"
  }
  ecs_minimum_running_tasks = var.platform_bootstrap_mode ? {} : {
    api     = var.api_min_count
    web     = var.web_min_count
    worker  = var.worker_min_count
    openfga = var.openfga_desired_count
  }
  rds_instances = {
    app     = aws_db_instance.main.identifier
    openfga = aws_db_instance.openfga.identifier
  }
  redis_member_clusters = tolist(aws_elasticache_replication_group.main.member_clusters)
}

resource "aws_cloudwatch_log_metric_filter" "api_errors" {
  name           = "${local.prefix}-api-errors"
  log_group_name = aws_cloudwatch_log_group.api.name
  pattern        = "?ERROR ?Error ?CRITICAL ?Traceback"

  metric_transformation {
    name          = "ApiLogErrors"
    namespace     = "Chronos/${var.environment}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "worker_errors" {
  name           = "${local.prefix}-worker-errors"
  log_group_name = aws_cloudwatch_log_group.worker.name
  pattern        = "?ERROR ?Error ?CRITICAL ?Traceback"

  metric_transformation {
    name          = "WorkerLogErrors"
    namespace     = "Chronos/${var.environment}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "openfga_errors" {
  name           = "${local.prefix}-openfga-errors"
  log_group_name = aws_cloudwatch_log_group.openfga.name
  pattern        = "?ERROR ?Error ?CRITICAL ?panic"

  metric_transformation {
    name          = "OpenFGALogErrors"
    namespace     = "Chronos/${var.environment}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "api_log_errors" {
  alarm_name          = "${local.prefix}-api-log-errors"
  alarm_description   = "API emitted at least five error-level log events in five minutes. Runbook: docs/PRODUCTION_OPERATIONS.md"
  namespace           = "Chronos/${var.environment}"
  metric_name         = "ApiLogErrors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 5
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "worker_log_errors" {
  alarm_name          = "${local.prefix}-worker-log-errors"
  alarm_description   = "Connector worker emitted an error-level event."
  namespace           = "Chronos/${var.environment}"
  metric_name         = "WorkerLogErrors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "openfga_log_errors" {
  alarm_name          = "${local.prefix}-openfga-log-errors"
  alarm_description   = "OpenFGA emitted an error or panic event."
  namespace           = "Chronos/${var.environment}"
  metric_name         = "OpenFGALogErrors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "api_target_5xx" {
  alarm_name          = "${local.prefix}-api-target-5xx"
  alarm_description   = "API targets returned repeated HTTP 5xx responses. Also participates in ECS deployment rollback."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 5
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "web_target_5xx" {
  alarm_name          = "${local.prefix}-web-target-5xx"
  alarm_description   = "Web targets returned repeated HTTP 5xx responses. Also participates in ECS deployment rollback."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 10
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    LoadBalancer = aws_lb.web.arn_suffix
    TargetGroup  = aws_lb_target_group.web.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "api_unhealthy_hosts" {
  alarm_name          = "${local.prefix}-api-unhealthy-hosts"
  alarm_description   = "At least one API target failed readiness in two consecutive periods."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Maximum"
  threshold           = 1
  period              = 60
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "web_unhealthy_hosts" {
  alarm_name          = "${local.prefix}-web-unhealthy-hosts"
  alarm_description   = "At least one web target failed health checks in two consecutive periods."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Maximum"
  threshold           = 1
  period              = 60
  evaluation_periods  = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    LoadBalancer = aws_lb.web.arn_suffix
    TargetGroup  = aws_lb_target_group.web.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "api_latency" {
  alarm_name          = "${local.prefix}-api-p95-latency"
  alarm_description   = "API target p95 exceeded two seconds for ten minutes."
  namespace           = "AWS/ApplicationELB"
  metric_name         = "TargetResponseTime"
  comparison_operator = "GreaterThanThreshold"
  extended_statistic  = "p95"
  threshold           = 2
  period              = 60
  evaluation_periods  = 10
  datapoints_to_alarm = 8
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_cpu_high" {
  for_each            = local.ecs_services
  alarm_name          = "${local.prefix}-${each.key}-cpu-high"
  alarm_description   = "${each.key} ECS average CPU exceeded 85 percent for fifteen minutes."
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  comparison_operator = "GreaterThanThreshold"
  statistic           = "Average"
  threshold           = 85
  period              = 300
  evaluation_periods  = 3
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_memory_high" {
  for_each            = local.ecs_services
  alarm_name          = "${local.prefix}-${each.key}-memory-high"
  alarm_description   = "${each.key} ECS average memory exceeded 85 percent for fifteen minutes."
  namespace           = "AWS/ECS"
  metric_name         = "MemoryUtilization"
  comparison_operator = "GreaterThanThreshold"
  statistic           = "Average"
  threshold           = 85
  period              = 300
  evaluation_periods  = 3
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = each.value
  }
}

resource "aws_cloudwatch_metric_alarm" "ecs_running_tasks_low" {
  for_each            = local.ecs_minimum_running_tasks
  alarm_name          = "${local.prefix}-${each.key}-running-tasks-low"
  alarm_description   = "${each.key} has fewer than its production minimum of ${each.value} running tasks."
  namespace           = "ECS/ContainerInsights"
  metric_name         = "RunningTaskCount"
  comparison_operator = "LessThanThreshold"
  statistic           = "Minimum"
  threshold           = each.value
  period              = 60
  evaluation_periods  = 2
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = local.ecs_services[each.key]
  }
}

resource "aws_cloudwatch_metric_alarm" "rds_cpu_high" {
  for_each            = local.rds_instances
  alarm_name          = "${local.prefix}-${each.key}-rds-cpu-high"
  alarm_description   = "${each.key} PostgreSQL CPU exceeded 80 percent for fifteen minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  comparison_operator = "GreaterThanThreshold"
  statistic           = "Average"
  threshold           = 80
  period              = 300
  evaluation_periods  = 3
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions          = { DBInstanceIdentifier = each.value }
}

resource "aws_cloudwatch_metric_alarm" "rds_storage_low" {
  for_each            = local.rds_instances
  alarm_name          = "${local.prefix}-${each.key}-rds-free-storage-low"
  alarm_description   = "${each.key} PostgreSQL has less than 10 GiB free before storage autoscaling."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  comparison_operator = "LessThanThreshold"
  statistic           = "Minimum"
  threshold           = 10737418240
  period              = 300
  evaluation_periods  = 2
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions          = { DBInstanceIdentifier = each.value }
}

resource "aws_cloudwatch_metric_alarm" "rds_memory_low" {
  for_each            = local.rds_instances
  alarm_name          = "${local.prefix}-${each.key}-rds-free-memory-low"
  alarm_description   = "${each.key} PostgreSQL has less than 256 MiB freeable memory."
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  comparison_operator = "LessThanThreshold"
  statistic           = "Minimum"
  threshold           = 268435456
  period              = 300
  evaluation_periods  = 3
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions          = { DBInstanceIdentifier = each.value }
}

resource "aws_cloudwatch_metric_alarm" "redis_cpu_high" {
  count               = var.redis_num_cache_clusters
  alarm_name          = "${local.prefix}-redis-${count.index + 1}-host-cpu-high"
  alarm_description   = "Redis node host CPU exceeded 45 percent. The production node class has two vCPUs, so host CPU can saturate before a generic 80 percent threshold."
  namespace           = "AWS/ElastiCache"
  metric_name         = "CPUUtilization"
  comparison_operator = "GreaterThanThreshold"
  statistic           = "Average"
  threshold           = 45
  period              = 300
  evaluation_periods  = 3
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    CacheClusterId = local.redis_member_clusters[count.index]
    CacheNodeId    = "0001"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_memory_low" {
  count               = var.redis_num_cache_clusters
  alarm_name          = "${local.prefix}-redis-${count.index + 1}-memory-high"
  alarm_description   = "Redis node memory counted for eviction exceeded 80 percent. noeviction converts exhaustion into a visible write failure instead of silently dropping coordination state."
  namespace           = "AWS/ElastiCache"
  metric_name         = "DatabaseMemoryUsageCountedForEvictPercentage"
  comparison_operator = "GreaterThanThreshold"
  statistic           = "Maximum"
  threshold           = 80
  period              = 300
  evaluation_periods  = 3
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    CacheClusterId = local.redis_member_clusters[count.index]
    CacheNodeId    = "0001"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_evictions" {
  count               = var.redis_num_cache_clusters
  alarm_name          = "${local.prefix}-redis-${count.index + 1}-evictions"
  alarm_description   = "Redis reported an eviction; production noeviction policy or parameter application must be investigated immediately."
  namespace           = "AWS/ElastiCache"
  metric_name         = "Evictions"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 60
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    CacheClusterId = local.redis_member_clusters[count.index]
    CacheNodeId    = "0001"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_replication_lag" {
  count               = max(var.redis_num_cache_clusters - 1, 0)
  alarm_name          = "${local.prefix}-redis-replica-${count.index + 1}-lag"
  alarm_description   = "Redis replica lag exceeded two seconds for five minutes."
  namespace           = "AWS/ElastiCache"
  metric_name         = "ReplicationLag"
  comparison_operator = "GreaterThanThreshold"
  statistic           = "Maximum"
  threshold           = 2
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  treat_missing_data  = "breaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    CacheClusterId = local.redis_member_clusters[count.index + 1]
    CacheNodeId    = "0001"
  }
}

resource "aws_cloudwatch_metric_alarm" "redis_authentication_failures" {
  count               = var.redis_num_cache_clusters
  alarm_name          = "${local.prefix}-redis-${count.index + 1}-authentication-failures"
  alarm_description   = "Redis received an authentication failure."
  namespace           = "AWS/ElastiCache"
  metric_name         = "AuthenticationFailures"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    CacheClusterId = local.redis_member_clusters[count.index]
    CacheNodeId    = "0001"
  }
}

resource "aws_cloudwatch_metric_alarm" "nat_port_errors" {
  count               = length(aws_nat_gateway.main)
  alarm_name          = "${local.prefix}-nat-${count.index}-port-errors"
  namespace           = "AWS/NATGateway"
  metric_name         = "ErrorPortAllocation"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions          = { NatGatewayId = aws_nat_gateway.main[count.index].id }
}

resource "aws_cloudwatch_event_rule" "ecs_deployment_failed" {
  name        = "${local.prefix}-ecs-deployment-failed"
  description = "Route ECS circuit-breaker deployment failures to operations."
  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Deployment State Change"]
    detail = {
      eventName = ["SERVICE_DEPLOYMENT_FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "ecs_deployment_failed" {
  rule = aws_cloudwatch_event_rule.ecs_deployment_failed.name
  arn  = aws_sns_topic.operations.arn
}

resource "aws_cloudwatch_event_rule" "backup_failed" {
  name        = "${local.prefix}-backup-failed"
  description = "Route failed backup and copy jobs to operations."
  event_pattern = jsonencode({
    source = ["aws.backup"]
    "detail-type" = [
      "Backup Job State Change",
      "Copy Job State Change",
    ]
    detail = {
      state = ["FAILED", "ABORTED", "EXPIRED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "backup_failed" {
  rule = aws_cloudwatch_event_rule.backup_failed.name
  arn  = aws_sns_topic.operations.arn
}

resource "aws_cloudwatch_event_rule" "restore_failed" {
  name        = "${local.prefix}-restore-failed"
  description = "Route failed manual and restore-testing jobs to operations."
  event_pattern = jsonencode({
    source        = ["aws.backup"]
    "detail-type" = ["Restore Job State Change"]
    detail = {
      status = ["FAILED", "ABORTED", "EXPIRED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "restore_failed" {
  rule = aws_cloudwatch_event_rule.restore_failed.name
  arn  = aws_sns_topic.operations.arn
}

resource "aws_cloudwatch_event_rule" "elasticache_failures" {
  name        = "${local.prefix}-elasticache-failures"
  description = "Route native ElastiCache failure and capacity events without attaching an unsupported encrypted SNS topic to the cluster."
  event_pattern = jsonencode({
    source = ["aws.elasticache"]
    "detail-type" = [
      "Cache Creation Failed",
      "Snapshot Creation Failed",
      "Cache Update Failed",
      "Cache Limit Approaching",
      "Snapshot Export Failed",
      "Snapshot Copy Failed",
    ]
  })
}

resource "aws_cloudwatch_event_target" "elasticache_failures" {
  rule = aws_cloudwatch_event_rule.elasticache_failures.name
  arn  = aws_sns_topic.operations.arn
}

resource "aws_cloudwatch_dashboard" "operations" {
  dashboard_name = "${local.prefix}-operations"
  dashboard_body = jsonencode({
    start          = "-PT6H"
    periodOverride = "inherit"
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 2
        properties = {
          markdown = "# Chronos ${var.environment}\nIncident and recovery procedures: `docs/PRODUCTION_OPERATIONS.md` and `docs/DISASTER_RECOVERY_RUNBOOK.md`."
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 2
        width  = 12
        height = 6
        properties = {
          title   = "ALB requests and target 5xx"
          region  = var.aws_region
          view    = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.api.arn_suffix, { stat = "Sum", label = "API requests" }],
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", aws_lb.api.arn_suffix, "TargetGroup", aws_lb_target_group.api.arn_suffix, { stat = "Sum", label = "API 5xx" }],
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.web.arn_suffix, { stat = "Sum", label = "Web requests" }],
            ["AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", "LoadBalancer", aws_lb.web.arn_suffix, "TargetGroup", aws_lb_target_group.web.arn_suffix, { stat = "Sum", label = "Web 5xx" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 2
        width  = 12
        height = 6
        properties = {
          title  = "API latency"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.api.arn_suffix, "TargetGroup", aws_lb_target_group.api.arn_suffix, { stat = "p95", label = "API p95" }],
            ["AWS/ApplicationELB", "TargetResponseTime", "LoadBalancer", aws_lb.api.arn_suffix, "TargetGroup", aws_lb_target_group.api.arn_suffix, { stat = "p50", label = "API p50" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 8
        width  = 12
        height = 6
        properties = {
          title  = "ECS CPU"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [for key, service in local.ecs_services : [
            "AWS/ECS", "CPUUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", service, { stat = "Average", label = key }
          ]]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 8
        width  = 12
        height = 6
        properties = {
          title  = "ECS memory"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [for key, service in local.ecs_services : [
            "AWS/ECS", "MemoryUtilization", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", service, { stat = "Average", label = key }
          ]]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 14
        width  = 12
        height = 6
        properties = {
          title  = "RDS"
          region = var.aws_region
          view   = "timeSeries"
          metrics = concat(
            [for key, instance in local.rds_instances :
              ["AWS/RDS", "CPUUtilization", "DBInstanceIdentifier", instance, { stat = "Average", label = "${key} CPU %" }]
            ],
            [for key, instance in local.rds_instances :
              ["AWS/RDS", "DatabaseConnections", "DBInstanceIdentifier", instance, { stat = "Average", label = "${key} connections", yAxis = "right" }]
            ],
          )
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 14
        width  = 12
        height = 6
        properties = {
          title  = "Application errors"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [
            ["Chronos/${var.environment}", "ApiLogErrors", { stat = "Sum", label = "API" }],
            ["Chronos/${var.environment}", "WorkerLogErrors", { stat = "Sum", label = "Worker" }],
            ["Chronos/${var.environment}", "OpenFGALogErrors", { stat = "Sum", label = "OpenFGA" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 20
        width  = 12
        height = 6
        properties = {
          title  = "ECS running tasks"
          region = var.aws_region
          view   = "timeSeries"
          metrics = [for key, service in local.ecs_services : [
            "ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.main.name, "ServiceName", service, { stat = "Minimum", label = key }
          ]]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 20
        width  = 12
        height = 6
        properties = {
          title  = "Redis memory and replication"
          region = var.aws_region
          view   = "timeSeries"
          metrics = concat(
            [for index, cluster in local.redis_member_clusters :
              ["AWS/ElastiCache", "DatabaseMemoryUsageCountedForEvictPercentage", "CacheClusterId", cluster, "CacheNodeId", "0001", { stat = "Maximum", label = "node ${index + 1} memory %" }]
            ],
            [for index, cluster in slice(local.redis_member_clusters, 1, length(local.redis_member_clusters)) :
              ["AWS/ElastiCache", "ReplicationLag", "CacheClusterId", cluster, "CacheNodeId", "0001", { stat = "Maximum", label = "replica ${index + 1} lag", yAxis = "right" }]
            ],
          )
        }
      },
    ]
  })
}
