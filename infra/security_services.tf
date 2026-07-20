# Account-level production security controls. These services are deliberately
# managed with the application stack so a green ECS deployment cannot be
# mistaken for a launch-ready AWS account.

locals {
  audit_trail_name          = "${local.prefix}-management"
  audit_trail_arn           = "arn:aws:cloudtrail:${var.aws_region}:${data.aws_caller_identity.current.account_id}:trail/${local.audit_trail_name}"
  cloudtrail_log_group_name = "/aws/cloudtrail/${local.prefix}"
  cloudtrail_log_group_arn  = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:${local.cloudtrail_log_group_name}"
  security_hub_standards = toset([
    "arn:aws:securityhub:${var.aws_region}::standards/aws-foundational-security-best-practices/v/1.0.0",
    "arn:aws:securityhub:${var.aws_region}::standards/cis-aws-foundations-benchmark/v/5.0.0",
    "arn:aws:securityhub:${var.aws_region}::standards/ai-security-best-practices/v/1.0.0",
  ])
}

resource "aws_kms_key" "audit" {
  count                   = var.account_security_services_enabled ? 1 : 0
  description             = "${local.prefix} CloudTrail and AWS Config audit evidence"
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
        Sid       = "CloudTrailEncrypt"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "kms:GenerateDataKey*"
        Resource  = "*"
        Condition = {
          StringEquals = {
            "aws:SourceArn" = local.audit_trail_arn
          }
          StringLike = {
            "kms:EncryptionContext:aws:cloudtrail:arn" = "arn:aws:cloudtrail:*:${data.aws_caller_identity.current.account_id}:trail/*"
          }
        }
      },
      {
        Sid       = "CloudTrailDescribe"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "kms:DescribeKey"
        Resource  = "*"
        Condition = {
          StringEquals = {
            "aws:SourceArn" = local.audit_trail_arn
          }
        }
      },
      {
        Sid       = "ConfigEncrypt"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = ["kms:GenerateDataKey*", "kms:Decrypt", "kms:DescribeKey"]
        Resource  = "*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:config:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      },
      {
        Sid       = "CloudWatchLogsEncrypt"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnEquals = {
            "kms:EncryptionContext:aws:logs:arn" = local.cloudtrail_log_group_arn
          }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "audit" {
  count         = var.account_security_services_enabled ? 1 : 0
  name          = "alias/${local.prefix}-audit"
  target_key_id = aws_kms_key.audit[0].key_id
}

resource "aws_s3_bucket" "audit_logs" {
  count               = var.account_security_services_enabled ? 1 : 0
  bucket              = "${local.prefix}-audit-${data.aws_caller_identity.current.account_id}"
  force_destroy       = false
  object_lock_enabled = true
}

resource "aws_s3_bucket_ownership_controls" "audit_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.audit_logs[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "audit_logs" {
  count                   = var.account_security_services_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.audit_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "audit_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.audit_logs[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.audit_logs[0].id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.audit[0].arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.audit_logs[0].id

  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.audit_log_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.audit_logs]
}

resource "aws_s3_bucket_lifecycle_configuration" "audit_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.audit_logs[0].id

  rule {
    id     = "retain-and-archive-audit-evidence"
    status = "Enabled"
    filter {
      prefix = ""
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = max(var.audit_log_retention_days, 2557)
    }
    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER_IR"
    }
    noncurrent_version_expiration {
      noncurrent_days = max(var.audit_log_retention_days, 2557)
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_object_lock_configuration.audit_logs]
}

resource "aws_s3_bucket_policy" "audit_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.audit_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.audit_logs[0].arn,
          "${aws_s3_bucket.audit_logs[0].arn}/*",
        ]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "CloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.audit_logs[0].arn
        Condition = { StringEquals = { "aws:SourceArn" = local.audit_trail_arn } }
      },
      {
        Sid       = "CloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.audit_logs[0].arn}/cloudtrail/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = {
            "aws:SourceArn" = local.audit_trail_arn
            "s3:x-amz-acl"  = "bucket-owner-full-control"
          }
        }
      },
    ]
  })
}

# AWS Config explicitly rejects delivery channels that target a bucket with an
# Object Lock default-retention rule. Keep CloudTrail's WORM evidence isolated
# and give Config a separate encrypted, versioned, non-destructible bucket.
resource "aws_s3_bucket" "config_logs" {
  count         = var.account_security_services_enabled ? 1 : 0
  bucket        = "${local.prefix}-config-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_ownership_controls" "config_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.config_logs[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "config_logs" {
  count                   = var.account_security_services_enabled ? 1 : 0
  bucket                  = aws_s3_bucket.config_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "config_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.config_logs[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.config_logs[0].id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.audit[0].arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "config_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.config_logs[0].id

  rule {
    id     = "retain-and-archive-config-evidence"
    status = "Enabled"
    filter {
      prefix = ""
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration {
      days = max(var.audit_log_retention_days, 2557)
    }
    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER_IR"
    }
    noncurrent_version_expiration {
      noncurrent_days = max(var.audit_log_retention_days, 2557)
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.config_logs]
}

resource "aws_s3_bucket_policy" "config_logs" {
  count  = var.account_security_services_enabled ? 1 : 0
  bucket = aws_s3_bucket.config_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.config_logs[0].arn,
          "${aws_s3_bucket.config_logs[0].arn}/*",
        ]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "ConfigBucketRead"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = ["s3:GetBucketAcl", "s3:ListBucket"]
        Resource  = aws_s3_bucket.config_logs[0].arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:config:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*" }
        }
      },
      {
        Sid       = "ConfigWrite"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.config_logs[0].arn}/config/AWSLogs/${data.aws_caller_identity.current.account_id}/Config/*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
            "s3:x-amz-acl"      = "bucket-owner-full-control"
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:config:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
          }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "cloudtrail" {
  count             = var.account_security_services_enabled ? 1 : 0
  name              = local.cloudtrail_log_group_name
  retention_in_days = var.audit_log_retention_days
  kms_key_id        = aws_kms_key.audit[0].arn
}

resource "aws_iam_role" "cloudtrail_logs" {
  count = var.account_security_services_enabled ? 1 : 0
  name  = "${local.prefix}-cloudtrail-logs"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
          "aws:SourceArn"     = local.audit_trail_arn
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "cloudtrail_logs" {
  count = var.account_security_services_enabled ? 1 : 0
  name  = "cloudwatch-log-delivery"
  role  = aws_iam_role.cloudtrail_logs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*"
    }]
  })
}

resource "aws_cloudtrail" "management" {
  count                         = var.account_security_services_enabled ? 1 : 0
  name                          = local.audit_trail_name
  s3_bucket_name                = aws_s3_bucket.audit_logs[0].id
  s3_key_prefix                 = "cloudtrail"
  include_global_service_events = true
  is_multi_region_trail         = true
  enable_log_file_validation    = true
  enable_logging                = true
  kms_key_id                    = aws_kms_key.audit[0].arn
  cloud_watch_logs_group_arn    = "${aws_cloudwatch_log_group.cloudtrail[0].arn}:*"
  cloud_watch_logs_role_arn     = aws_iam_role.cloudtrail_logs[0].arn

  event_selector {
    read_write_type           = "All"
    include_management_events = true
    dynamic "data_resource" {
      for_each = var.cloudtrail_s3_data_events_enabled ? [1] : []
      content {
        type   = "AWS::S3::Object"
        values = ["${aws_s3_bucket.artifacts.arn}/"]
      }
    }
  }

  dynamic "insight_selector" {
    for_each = var.cloudtrail_insights_enabled ? ["ApiCallRateInsight", "ApiErrorRateInsight"] : []
    content {
      insight_type = insight_selector.value
    }
  }

  depends_on = [
    aws_iam_role_policy.cloudtrail_logs,
    aws_s3_bucket_policy.audit_logs,
    aws_s3_bucket_object_lock_configuration.audit_logs,
  ]
}

resource "aws_cloudwatch_log_metric_filter" "unauthorized_api_calls" {
  count          = var.account_security_services_enabled ? 1 : 0
  name           = "${local.prefix}-unauthorized-api-calls"
  log_group_name = aws_cloudwatch_log_group.cloudtrail[0].name
  pattern        = "{ ($.errorCode = \"*UnauthorizedOperation\") || ($.errorCode = \"AccessDenied*\") }"
  metric_transformation {
    name      = "UnauthorizedApiCalls"
    namespace = "Chronos/${var.environment}/Security"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "root_activity" {
  count          = var.account_security_services_enabled ? 1 : 0
  name           = "${local.prefix}-root-activity"
  log_group_name = aws_cloudwatch_log_group.cloudtrail[0].name
  pattern        = "{ $.userIdentity.type = \"Root\" && $.userIdentity.invokedBy NOT EXISTS && $.eventType != \"AwsServiceEvent\" }"
  metric_transformation {
    name      = "RootAccountActivity"
    namespace = "Chronos/${var.environment}/Security"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "console_login_without_mfa" {
  count          = var.account_security_services_enabled ? 1 : 0
  name           = "${local.prefix}-console-login-without-mfa"
  log_group_name = aws_cloudwatch_log_group.cloudtrail[0].name
  pattern        = "{ ($.eventName = \"ConsoleLogin\") && ($.additionalEventData.MFAUsed != \"Yes\") }"
  metric_transformation {
    name      = "ConsoleLoginWithoutMfa"
    namespace = "Chronos/${var.environment}/Security"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "cloudtrail_configuration_changes" {
  count          = var.account_security_services_enabled ? 1 : 0
  name           = "${local.prefix}-cloudtrail-configuration-changes"
  log_group_name = aws_cloudwatch_log_group.cloudtrail[0].name
  pattern        = "{ ($.eventSource = \"cloudtrail.amazonaws.com\") && (($.eventName = \"CreateTrail\") || ($.eventName = \"UpdateTrail\") || ($.eventName = \"DeleteTrail\") || ($.eventName = \"StartLogging\") || ($.eventName = \"StopLogging\") || ($.eventName = \"PutEventSelectors\") || ($.eventName = \"PutInsightSelectors\")) }"
  metric_transformation {
    name      = "CloudTrailConfigurationChanges"
    namespace = "Chronos/${var.environment}/Security"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "unauthorized_api_calls" {
  count               = var.account_security_services_enabled ? 1 : 0
  alarm_name          = "${local.prefix}-unauthorized-api-calls"
  alarm_description   = "Repeated denied AWS API calls can indicate credential misuse or policy drift."
  namespace           = "Chronos/${var.environment}/Security"
  metric_name         = "UnauthorizedApiCalls"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 5
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "root_activity" {
  count               = var.account_security_services_enabled ? 1 : 0
  alarm_name          = "${local.prefix}-root-account-activity"
  alarm_description   = "Any direct root-account API activity requires immediate review."
  namespace           = "Chronos/${var.environment}/Security"
  metric_name         = "RootAccountActivity"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 60
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "console_login_without_mfa" {
  count               = var.account_security_services_enabled ? 1 : 0
  alarm_name          = "${local.prefix}-console-login-without-mfa"
  alarm_description   = "AWS console login without MFA requires immediate investigation."
  namespace           = "Chronos/${var.environment}/Security"
  metric_name         = "ConsoleLoginWithoutMfa"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 60
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "cloudtrail_configuration_changes" {
  count               = var.account_security_services_enabled ? 1 : 0
  alarm_name          = "${local.prefix}-cloudtrail-configuration-changes"
  alarm_description   = "Any CloudTrail configuration or logging-state change requires review."
  namespace           = "Chronos/${var.environment}/Security"
  metric_name         = "CloudTrailConfigurationChanges"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 1
  period              = 60
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
}

resource "aws_iam_role" "config" {
  count = var.account_security_services_enabled ? 1 : 0
  name  = "${local.prefix}-aws-config"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "config.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = {
          "aws:SourceAccount" = data.aws_caller_identity.current.account_id
        }
        ArnLike = {
          "aws:SourceArn" = "arn:aws:config:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "config" {
  count      = var.account_security_services_enabled ? 1 : 0
  role       = aws_iam_role.config[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWS_ConfigRole"
}

resource "aws_iam_role_policy" "config_delivery" {
  count = var.account_security_services_enabled ? 1 : 0
  name  = "audit-bucket-delivery"
  role  = aws_iam_role.config[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketAcl", "s3:ListBucket"]
        Resource = aws_s3_bucket.config_logs[0].arn
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.config_logs[0].arn}/config/AWSLogs/${data.aws_caller_identity.current.account_id}/Config/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey*", "kms:Decrypt", "kms:DescribeKey"]
        Resource = aws_kms_key.audit[0].arn
      },
    ]
  })
}

resource "aws_config_configuration_recorder" "main" {
  count    = var.account_security_services_enabled ? 1 : 0
  name     = "${local.prefix}-all-resources"
  role_arn = aws_iam_role.config[0].arn

  recording_group {
    all_supported                 = true
    include_global_resource_types = true
  }
}

resource "aws_config_delivery_channel" "main" {
  count          = var.account_security_services_enabled ? 1 : 0
  name           = "${local.prefix}-audit"
  s3_bucket_name = aws_s3_bucket.config_logs[0].bucket
  s3_key_prefix  = "config"
  s3_kms_key_arn = aws_kms_key.audit[0].arn

  snapshot_delivery_properties {
    delivery_frequency = "Six_Hours"
  }

  depends_on = [
    aws_iam_role_policy.config_delivery,
    aws_s3_bucket_policy.config_logs,
    aws_s3_bucket_server_side_encryption_configuration.config_logs,
  ]
}

resource "aws_config_configuration_recorder_status" "main" {
  count      = var.account_security_services_enabled ? 1 : 0
  name       = aws_config_configuration_recorder.main[0].name
  is_enabled = true
  depends_on = [aws_config_delivery_channel.main]
}

resource "aws_guardduty_detector" "main" {
  count                        = var.account_security_services_enabled ? 1 : 0
  enable                       = true
  finding_publishing_frequency = "FIFTEEN_MINUTES"

  datasources {
    s3_logs {
      enable = true
    }
    malware_protection {
      scan_ec2_instance_with_findings {
        ebs_volumes {
          enable = true
        }
      }
    }
  }
}

resource "aws_securityhub_account" "main" {
  count                     = var.account_security_services_enabled ? 1 : 0
  enable_default_standards  = false
  control_finding_generator = "SECURITY_CONTROL"
  auto_enable_controls      = true

  depends_on = [aws_config_configuration_recorder_status.main]
}

resource "aws_securityhub_standards_subscription" "main" {
  for_each      = var.account_security_services_enabled ? local.security_hub_standards : toset([])
  standards_arn = each.value
  depends_on    = [aws_securityhub_account.main]
}

resource "aws_securityhub_finding_aggregator" "main" {
  count        = var.account_security_services_enabled ? 1 : 0
  linking_mode = "ALL_REGIONS"
  depends_on   = [aws_securityhub_account.main]
}

resource "aws_inspector2_enabler" "main" {
  count          = var.account_security_services_enabled ? 1 : 0
  account_ids    = [data.aws_caller_identity.current.account_id]
  resource_types = ["EC2", "ECR", "LAMBDA", "LAMBDA_CODE"]
}

resource "aws_accessanalyzer_analyzer" "main" {
  count         = var.account_security_services_enabled ? 1 : 0
  analyzer_name = "${local.prefix}-external-access"
  type          = "ACCOUNT"
}

# Detection services are only operational controls when high-impact findings
# reach the on-call route. EventBridge delivers the original finding payload so
# responders retain the finding ARN, affected resource, and remediation detail.
resource "aws_cloudwatch_event_rule" "guardduty_findings" {
  count       = var.account_security_services_enabled ? 1 : 0
  name        = "${local.prefix}-guardduty-high-findings"
  description = "Route high and critical GuardDuty findings to operations."
  event_pattern = jsonencode({
    source      = ["aws.guardduty"]
    detail-type = ["GuardDuty Finding"]
    detail = {
      severity = [{ numeric = [">=", 7] }]
    }
  })
}

resource "aws_cloudwatch_event_target" "guardduty_findings" {
  count     = var.account_security_services_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.guardduty_findings[0].name
  target_id = "operations-sns"
  arn       = aws_sns_topic.operations.arn
}

resource "aws_cloudwatch_event_rule" "securityhub_findings" {
  count       = var.account_security_services_enabled ? 1 : 0
  name        = "${local.prefix}-securityhub-high-findings"
  description = "Route active high and critical Security Hub CSPM findings to operations."
  event_pattern = jsonencode({
    source      = ["aws.securityhub"]
    detail-type = ["Security Hub Findings - Imported"]
    detail = {
      findings = {
        Severity = {
          Label = ["HIGH", "CRITICAL"]
        }
        RecordState = ["ACTIVE"]
        Workflow = {
          Status = ["NEW", "NOTIFIED"]
        }
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "securityhub_findings" {
  count     = var.account_security_services_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.securityhub_findings[0].name
  target_id = "operations-sns"
  arn       = aws_sns_topic.operations.arn
}

resource "aws_cloudwatch_event_rule" "inspector_findings" {
  count       = var.account_security_services_enabled ? 1 : 0
  name        = "${local.prefix}-inspector-high-findings"
  description = "Route active high and critical Inspector findings to operations."
  event_pattern = jsonencode({
    source      = ["aws.inspector2"]
    detail-type = ["Inspector2 Finding"]
    detail = {
      severity = ["HIGH", "CRITICAL"]
      status   = ["ACTIVE"]
    }
  })
}

resource "aws_cloudwatch_event_target" "inspector_findings" {
  count     = var.account_security_services_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.inspector_findings[0].name
  target_id = "operations-sns"
  arn       = aws_sns_topic.operations.arn
}

resource "aws_cloudwatch_event_rule" "config_delivery_failures" {
  count       = var.account_security_services_enabled ? 1 : 0
  name        = "${local.prefix}-config-delivery-failures"
  description = "Route failed AWS Config snapshot/history deliveries to operations."
  event_pattern = jsonencode({
    source = ["aws.config"]
    detail-type = [
      "Config Configuration Snapshot Delivery Status",
      "Config Configuration History Delivery Status",
    ]
    detail = {
      messageType = [
        "ConfigurationSnapshotDeliveryFailed",
        "ConfigurationHistoryDeliveryFailed",
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "config_delivery_failures" {
  count     = var.account_security_services_enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.config_delivery_failures[0].name
  target_id = "operations-sns"
  arn       = aws_sns_topic.operations.arn
}
