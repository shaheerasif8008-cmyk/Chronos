# Data encryption, point-in-time recovery, immutable retention, cross-Region
# copies, and automated restore proof. Creating recovery points is not proof by
# itself; the monthly RDS restore test verifies that AWS can actually provision
# from one and automatically removes the temporary instance after the one-hour
# validation window.

resource "aws_kms_key" "data" {
  description             = "${local.prefix} application data and primary backup vault"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "data" {
  name          = "alias/${local.prefix}-data"
  target_key_id = aws_kms_key.data.key_id
}

resource "aws_kms_key" "dr" {
  count                   = var.backups_enabled ? 1 : 0
  provider                = aws.dr
  description             = "${local.prefix} cross-Region backups and replicated secrets"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "dr" {
  count         = var.backups_enabled ? 1 : 0
  provider      = aws.dr
  name          = "alias/${local.prefix}-dr"
  target_key_id = aws_kms_key.dr[0].key_id
}

resource "aws_backup_vault" "primary" {
  count       = var.backups_enabled ? 1 : 0
  name        = "${local.prefix}-primary"
  kms_key_arn = aws_kms_key.data.arn
}

# Governance mode is intentional: omitting changeable_for_days keeps the lock
# removable only by privileged break-glass operators. Compliance mode becomes
# irreversible after its grace period and must not be enabled by an unattended
# Terraform apply without legal approval of the retention policy.
resource "aws_backup_vault_lock_configuration" "primary" {
  count              = var.backups_enabled ? 1 : 0
  backup_vault_name  = aws_backup_vault.primary[0].name
  min_retention_days = 1
  max_retention_days = var.backup_pitr_retention_days
}

resource "aws_backup_vault" "dr" {
  count       = var.backups_enabled ? 1 : 0
  provider    = aws.dr
  name        = "${local.prefix}-cross-region"
  kms_key_arn = aws_kms_key.dr[0].arn
}

resource "aws_backup_vault_lock_configuration" "dr" {
  count              = var.backups_enabled ? 1 : 0
  provider           = aws.dr
  backup_vault_name  = aws_backup_vault.dr[0].name
  min_retention_days = 35
  max_retention_days = var.backup_copy_retention_days
}

resource "aws_iam_role" "backup" {
  count = var.backups_enabled ? 1 : 0
  name  = "${local.prefix}-backup-service"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "backup.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

locals {
  backup_managed_policies = var.backups_enabled ? toset([
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup",
    "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores",
    "arn:aws:iam::aws:policy/AWSBackupServiceRolePolicyForS3Backup",
    "arn:aws:iam::aws:policy/AWSBackupServiceRolePolicyForS3Restore",
  ]) : toset([])
}

resource "aws_iam_role_policy_attachment" "backup" {
  for_each   = local.backup_managed_policies
  role       = aws_iam_role.backup[0].name
  policy_arn = each.value
}

resource "aws_backup_plan" "critical_data" {
  count = var.backups_enabled ? 1 : 0
  name  = "${local.prefix}-critical-data"

  rule {
    rule_name                = "continuous-pitr-and-daily-dr-copy"
    target_vault_name        = aws_backup_vault.primary[0].name
    schedule                 = "cron(0 02 * * ? *)"
    start_window             = 60
    completion_window        = 720
    enable_continuous_backup = true

    lifecycle {
      delete_after = var.backup_pitr_retention_days
    }

    copy_action {
      destination_vault_arn = aws_backup_vault.dr[0].arn

      lifecycle {
        delete_after = var.backup_copy_retention_days
      }
    }

    recovery_point_tags = {
      Application  = var.app_name
      Environment  = var.environment
      RecoveryTier = "critical"
    }
  }
}

resource "aws_backup_selection" "critical_data" {
  count        = var.backups_enabled ? 1 : 0
  name         = "${local.prefix}-rds-and-artifacts"
  iam_role_arn = aws_iam_role.backup[0].arn
  plan_id      = aws_backup_plan.critical_data[0].id
  resources = [
    aws_db_instance.main.arn,
    aws_db_instance.openfga.arn,
    aws_s3_bucket.artifacts.arn,
  ]

  depends_on = [
    aws_iam_role_policy_attachment.backup,
    aws_s3_bucket_versioning.artifacts,
  ]
}

resource "aws_backup_restore_testing_plan" "monthly" {
  count                        = var.backups_enabled && var.automated_restore_testing_enabled ? 1 : 0
  name                         = "${local.prefix}-monthly-restore"
  schedule_expression          = "cron(0 08 1 * ? *)"
  schedule_expression_timezone = "Etc/UTC"
  start_window_hours           = 24

  recovery_point_selection {
    algorithm             = "LATEST_WITHIN_WINDOW"
    include_vaults        = [aws_backup_vault.primary[0].arn]
    recovery_point_types  = ["CONTINUOUS", "SNAPSHOT"]
    selection_window_days = 7
  }
}

resource "aws_backup_restore_testing_selection" "rds" {
  count                     = var.backups_enabled && var.automated_restore_testing_enabled ? 1 : 0
  name                      = "chronos_rds"
  restore_testing_plan_name = aws_backup_restore_testing_plan.monthly[0].name
  protected_resource_type   = "RDS"
  protected_resource_arns = [
    aws_db_instance.main.arn,
    aws_db_instance.openfga.arn,
  ]
  iam_role_arn            = aws_iam_role.backup[0].arn
  validation_window_hours = 1

  restore_metadata_overrides = {
    dbInstanceClass     = "db.t3.micro"
    dbSubnetGroupName   = aws_db_subnet_group.main.name
    multiAz             = "false"
    publiclyAccessible  = "false"
    vpcSecurityGroupIds = jsonencode([aws_security_group.restore_test.id])
  }

  depends_on = [aws_iam_role_policy_attachment.backup]
}
