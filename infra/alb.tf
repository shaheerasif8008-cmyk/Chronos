# ── API ALB ───────────────────────────────────────────────────────────────────
resource "aws_lb" "api" {
  name               = "${local.prefix}-api-alb"
  internal           = var.restore_rehearsal_mode
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.restore_rehearsal_mode ? aws_subnet.private[*].id : aws_subnet.public[*].id

  enable_deletion_protection                  = local.is_production
  drop_invalid_header_fields                  = true
  desync_mitigation_mode                      = "strictest"
  enable_http2                                = true
  enable_tls_version_and_cipher_suite_headers = true
  enable_waf_fail_open                        = false
  idle_timeout                                = 300
  preserve_host_header                        = true
  xff_header_processing_mode                  = "append"

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.bucket
    prefix  = "api-alb"
    enabled = true
  }

  depends_on = [aws_s3_bucket_policy.alb_logs]
}

resource "aws_lb_target_group" "api" {
  name        = "${local.prefix}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/ready"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 30
    timeout             = 5
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "api_http" {
  count             = local.api_certificate_arn != "" ? 1 : 0
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "api_https" {
  count                                                        = local.api_certificate_arn != "" ? 1 : 0
  load_balancer_arn                                            = aws_lb.api.arn
  port                                                         = 443
  protocol                                                     = "HTTPS"
  ssl_policy                                                   = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn                                              = local.api_certificate_arn
  routing_http_response_server_enabled                         = false
  routing_http_response_strict_transport_security_header_value = "max-age=31536000; includeSubDomains; preload"
  routing_http_response_x_content_type_options_header_value    = "nosniff"
  routing_http_response_x_frame_options_header_value           = "DENY"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# HTTP-only listener for environments without a certificate yet.
resource "aws_lb_listener" "api_http_forward" {
  count             = local.api_certificate_arn == "" ? 1 : 0
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ── Web ALB ───────────────────────────────────────────────────────────────────
resource "aws_lb" "web" {
  name               = "${local.prefix}-web-alb"
  internal           = var.restore_rehearsal_mode
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.restore_rehearsal_mode ? aws_subnet.private[*].id : aws_subnet.public[*].id

  enable_deletion_protection                  = local.is_production
  drop_invalid_header_fields                  = true
  desync_mitigation_mode                      = "strictest"
  enable_http2                                = true
  enable_tls_version_and_cipher_suite_headers = true
  enable_waf_fail_open                        = false
  idle_timeout                                = 120
  preserve_host_header                        = true
  xff_header_processing_mode                  = "append"

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.bucket
    prefix  = "web-alb"
    enabled = true
  }

  depends_on = [aws_s3_bucket_policy.alb_logs]
}

resource "aws_lb_target_group" "web" {
  name        = "${local.prefix}-web-tg"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path              = "/"
    matcher           = "200-399"
    healthy_threshold = 2
    interval          = 30
  }

  deregistration_delay = 30
}

resource "aws_lb_listener" "web_http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = local.web_certificate_arn != "" ? "redirect" : "forward"

    dynamic "redirect" {
      for_each = local.web_certificate_arn != "" ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }

    dynamic "forward" {
      for_each = local.web_certificate_arn == "" ? [1] : []
      content {
        target_group {
          arn = aws_lb_target_group.web.arn
        }
      }
    }
  }
}

resource "aws_lb_listener" "web_https" {
  count                                                        = local.web_certificate_arn != "" ? 1 : 0
  load_balancer_arn                                            = aws_lb.web.arn
  port                                                         = 443
  protocol                                                     = "HTTPS"
  ssl_policy                                                   = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn                                              = local.web_certificate_arn
  routing_http_response_server_enabled                         = false
  routing_http_response_strict_transport_security_header_value = "max-age=31536000; includeSubDomains; preload"
  routing_http_response_x_content_type_options_header_value    = "nosniff"
  routing_http_response_x_frame_options_header_value           = "DENY"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# ── ALB access log bucket ─────────────────────────────────────────────────────
resource "aws_s3_bucket" "alb_logs" {
  bucket        = "${local.prefix}-alb-logs-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket                  = aws_s3_bucket.alb_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  rule {
    apply_server_side_encryption_by_default {
      # ALB access-log delivery supports SSE-S3, not SSE-KMS.
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    id     = "retain-production-access-logs"
    status = "Enabled"
    filter { prefix = "" }

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    expiration { days = 365 }

    # Versioning would otherwise retain the expired current version forever as
    # a noncurrent object and defeat the documented one-year log-retention cap.
    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "STANDARD_IA"
    }
    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }
}

resource "aws_s3_bucket_policy" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowALBLogDelivery"
        Effect    = "Allow"
        Principal = { Service = "logdelivery.elasticloadbalancing.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.alb_logs.arn}/api-alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
          "${aws_s3_bucket.alb_logs.arn}/web-alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
        ]
        Condition = {
          ArnLike = {
            "aws:SourceArn" = "arn:aws:elasticloadbalancing:${var.aws_region}:${data.aws_caller_identity.current.account_id}:loadbalancer/*"
          }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.alb_logs.arn,
          "${aws_s3_bucket.alb_logs.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })
}
