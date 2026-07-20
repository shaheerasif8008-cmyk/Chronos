# Regional WAF protection for both ALBs. API body-oriented managed rules that
# commonly false-positive on legitimate chat/code payloads stay in count mode;
# reputation, known-bad inputs, headers/query/path rules, and explicit rate
# controls still block. Review counted-rule samples before promoting any body
# override to block.

resource "aws_wafv2_web_acl" "api" {
  count = var.waf_enabled ? 1 : 0
  name  = "${local.prefix}-api"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "aws-ip-reputation"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-api-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-known-bad-inputs"
    priority = 20
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-api-known-bad"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-common"
    priority = 30
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"

        # Large prompts, source code, and artifact bodies are expected API
        # inputs. Count these two body signatures for review instead of making
        # WAF a lossy product-content filter.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            count {}
          }
        }
        rule_action_override {
          name = "CrossSiteScripting_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-api-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-sqli"
    priority = 35
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesSQLiRuleSet"
        vendor_name = "AWS"

        # Chat prompts and source files are arbitrary text. Keep body SQLi in
        # count/review while query, cookie, and URI-path signatures still block.
        rule_action_override {
          name = "SQLi_BODY"
          action_to_use {
            count {}
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-api-sqli"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "auth-rate-limit"
    priority = 40
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
        limit                 = var.waf_auth_rate_limit

        scope_down_statement {
          byte_match_statement {
            positional_constraint = "STARTS_WITH"
            search_string         = "/auth/"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-api-auth-rate"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "api-rate-limit"
    priority = 50
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
        limit                 = var.waf_api_rate_limit
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-api-rate"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.prefix}-api-waf"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl" "web" {
  count = var.waf_enabled ? 1 : 0
  name  = "${local.prefix}-web"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "aws-ip-reputation"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-web-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-known-bad-inputs"
    priority = 20
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-web-known-bad"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-common"
    priority = 30
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-web-common"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-sqli"
    priority = 35
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesSQLiRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-web-sqli"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "web-rate-limit"
    priority = 40
    action {
      block {}
    }
    statement {
      rate_based_statement {
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
        limit                 = var.waf_web_rate_limit
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.prefix}-web-rate"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.prefix}-web-waf"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "api" {
  count        = var.waf_enabled ? 1 : 0
  resource_arn = aws_lb.api.arn
  web_acl_arn  = aws_wafv2_web_acl.api[0].arn
}

resource "aws_wafv2_web_acl_association" "web" {
  count        = var.waf_enabled ? 1 : 0
  resource_arn = aws_lb.web.arn
  web_acl_arn  = aws_wafv2_web_acl.web[0].arn
}

resource "aws_cloudwatch_log_group" "waf_api" {
  count             = var.waf_enabled ? 1 : 0
  name              = "aws-waf-logs-${local.prefix}-api"
  retention_in_days = var.waf_log_retention_days
}

resource "aws_cloudwatch_log_group" "waf_web" {
  count             = var.waf_enabled ? 1 : 0
  name              = "aws-waf-logs-${local.prefix}-web"
  retention_in_days = var.waf_log_retention_days
}

resource "aws_wafv2_web_acl_logging_configuration" "api" {
  count                   = var.waf_enabled ? 1 : 0
  resource_arn            = aws_wafv2_web_acl.api[0].arn
  log_destination_configs = [aws_cloudwatch_log_group.waf_api[0].arn]

  logging_filter {
    default_behavior = "DROP"
    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"
      condition {
        action_condition { action = "BLOCK" }
      }
      # Required to review the deliberate API body-rule overrides before
      # deciding whether any can safely be promoted from count to block.
      condition {
        action_condition { action = "COUNT" }
      }
    }
  }

  redacted_fields {
    single_header { name = "authorization" }
  }
  redacted_fields {
    single_header { name = "cookie" }
  }
  redacted_fields {
    single_header { name = "x-api-key" }
  }
}

resource "aws_wafv2_web_acl_logging_configuration" "web" {
  count                   = var.waf_enabled ? 1 : 0
  resource_arn            = aws_wafv2_web_acl.web[0].arn
  log_destination_configs = [aws_cloudwatch_log_group.waf_web[0].arn]

  logging_filter {
    default_behavior = "DROP"
    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"
      condition {
        action_condition { action = "BLOCK" }
      }
    }
  }

  redacted_fields {
    single_header { name = "cookie" }
  }
}

resource "aws_cloudwatch_metric_alarm" "waf_api_blocks" {
  count               = var.waf_enabled ? 1 : 0
  alarm_name          = "${local.prefix}-waf-api-block-spike"
  alarm_description   = "AWS WAF blocked at least 100 API requests in five minutes."
  namespace           = "AWS/WAFV2"
  metric_name         = "BlockedRequests"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 100
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    WebACL = aws_wafv2_web_acl.api[0].name
    Region = var.aws_region
    Rule   = "ALL"
  }
}

resource "aws_cloudwatch_metric_alarm" "waf_web_blocks" {
  count               = var.waf_enabled ? 1 : 0
  alarm_name          = "${local.prefix}-waf-web-block-spike"
  alarm_description   = "AWS WAF blocked at least 100 web requests in five minutes."
  namespace           = "AWS/WAFV2"
  metric_name         = "BlockedRequests"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  statistic           = "Sum"
  threshold           = 100
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_actions
  ok_actions          = local.alarm_actions
  dimensions = {
    WebACL = aws_wafv2_web_acl.web[0].name
    Region = var.aws_region
    Rule   = "ALL"
  }
}
