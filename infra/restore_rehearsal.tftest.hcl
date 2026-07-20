mock_provider "aws" {}
mock_provider "random" {}

variables {
  aws_region                        = "us-west-2"
  availability_zones                = ["us-west-2a", "us-west-2b"]
  environment                       = "restore-test"
  jwt_secret                        = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  vault_encryption_key              = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef" # gitleaks:allow - non-production test fixture
  admin_email                       = "restore-admin@example.invalid"
  auth_provider                     = "dev_otp"
  terms_url                         = "https://restore.invalid/terms"
  privacy_url                       = "https://restore.invalid/privacy"
  support_url                       = "https://restore.invalid/support"
  status_url                        = "https://restore.invalid/status"
  demo_mode                         = true
  waf_enabled                       = false
  account_security_services_enabled = false
  cloudtrail_s3_data_events_enabled = false
  backups_enabled                   = false
  automated_restore_testing_enabled = false
}

run "restore_rehearsal_is_quarantined" {
  command = plan

  variables {
    restore_rehearsal_mode                 = true
    restore_app_db_snapshot_identifier     = "chronos-safe-snapshot"
    restore_openfga_db_snapshot_identifier = "openfga-safe-snapshot"
    restore_redis_snapshot_name            = "redis-safe-snapshot"
    restore_rehearsal_ingress_cidrs        = ["10.99.0.0/24"]
    platform_bootstrap_mode                = true
  }

  assert {
    condition     = aws_lb.api.internal && aws_lb.web.internal
    error_message = "Restore rehearsal ALBs must be internal."
  }

  assert {
    condition = alltrue([
      for rule in aws_security_group.alb.ingress :
      !contains(rule.cidr_blocks, "0.0.0.0/0")
    ])
    error_message = "Restore rehearsal ALBs must not allow public ingress."
  }

  assert {
    condition = alltrue([
      for rule in aws_s3_bucket_cors_configuration.artifacts.cors_rule :
      !contains(rule.allowed_origins, "*")
    ])
    error_message = "Restore rehearsal artifact CORS must not allow wildcard origins."
  }

  assert {
    condition = alltrue([
      aws_ecs_service.api.desired_count == 0,
      aws_ecs_service.web.desired_count == 0,
      aws_ecs_service.worker.desired_count == 0,
      aws_ecs_service.openfga.desired_count == 0,
    ])
    error_message = "The initial restore plan must keep every application service stopped."
  }

  assert {
    condition = alltrue([
      aws_appautoscaling_target.api.min_capacity == 0,
      aws_appautoscaling_target.web.min_capacity == 0,
      aws_appautoscaling_target.worker.min_capacity == 0,
    ])
    error_message = "Bootstrap autoscaling targets must not restart stopped rehearsal services."
  }

  assert {
    condition = (
      aws_db_instance.main.snapshot_identifier == "chronos-safe-snapshot" &&
      aws_db_instance.openfga.snapshot_identifier == "openfga-safe-snapshot" &&
      aws_elasticache_replication_group.main.snapshot_name == "redis-safe-snapshot"
    )
    error_message = "Restore resources must use the explicitly reviewed snapshot seeds."
  }
}

run "snapshot_inputs_require_rehearsal_mode" {
  command = plan

  variables {
    environment                            = "staging"
    restore_rehearsal_mode                 = false
    restore_app_db_snapshot_identifier     = "chronos-unsafe-snapshot"
    restore_openfga_db_snapshot_identifier = "openfga-unsafe-snapshot"
  }

  expect_failures = [terraform_data.production_guard]
}

run "restore_rehearsal_rejects_external_credentials" {
  command = plan

  variables {
    restore_rehearsal_mode                 = true
    restore_app_db_snapshot_identifier     = "chronos-safe-snapshot"
    restore_openfga_db_snapshot_identifier = "openfga-safe-snapshot"
    openrouter_api_key                     = "must-not-enter-a-restore-stack"
  }

  expect_failures = [terraform_data.production_guard]
}

run "restore_rehearsal_rejects_publication_credentials" {
  command = plan

  variables {
    restore_rehearsal_mode                 = true
    restore_app_db_snapshot_identifier     = "chronos-safe-snapshot"
    restore_openfga_db_snapshot_identifier = "openfga-safe-snapshot"
    slack_signing_secret                   = "must-not-enter-a-restore-stack"
  }

  expect_failures = [terraform_data.production_guard]
}

run "ordinary_nonproduction_edge_remains_public" {
  command = plan

  variables {
    environment             = "staging"
    restore_rehearsal_mode  = false
    platform_bootstrap_mode = true
  }

  assert {
    condition     = !aws_lb.api.internal && !aws_lb.web.internal
    error_message = "Restore quarantine routing must not change an ordinary environment into internal ALBs."
  }

  assert {
    condition = anytrue([
      for rule in aws_security_group.alb.ingress :
      contains(rule.cidr_blocks, "0.0.0.0/0")
    ])
    error_message = "Ordinary environments retain the existing public ALB ingress contract."
  }
}
