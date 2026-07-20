locals {
  restore_app_db     = trimspace(var.restore_app_db_snapshot_identifier) != ""
  restore_openfga_db = trimspace(var.restore_openfga_db_snapshot_identifier) != ""
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.prefix}-db-subnet-group"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "postgres" {
  name   = "${local.prefix}-pg${var.db_engine_version}"
  family = "postgres${var.db_engine_version}"

  # Needed for pgvector to load on RDS.
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements"
    apply_method = "pending-reboot"
  }
}

resource "aws_iam_role" "rds_enhanced_monitoring" {
  name = "${local.prefix}-rds-enhanced-monitoring"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "rds_enhanced_monitoring" {
  role       = aws_iam_role.rds_enhanced_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "random_password" "db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "random_password" "openfga_db" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "db_password" {
  name                    = "${local.prefix}/rds/password"
  recovery_window_in_days = 7

  dynamic "replica" {
    for_each = var.backups_enabled ? [var.backup_copy_region] : []
    content {
      region     = replica.value
      kms_key_id = aws_kms_key.dr[0].arn
    }
  }
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id     = aws_secretsmanager_secret.db_password.id
  secret_string = random_password.db.result
}

resource "aws_secretsmanager_secret" "openfga_db_password" {
  name                    = "${local.prefix}/openfga/rds/password"
  recovery_window_in_days = 7

  dynamic "replica" {
    for_each = var.backups_enabled ? [var.backup_copy_region] : []
    content {
      region     = replica.value
      kms_key_id = aws_kms_key.dr[0].arn
    }
  }
}

resource "aws_secretsmanager_secret_version" "openfga_db_password" {
  secret_id     = aws_secretsmanager_secret.openfga_db_password.id
  secret_string = random_password.openfga_db.result
}

# RDS creates export log groups with infinite retention when they are absent.
# Manage them first so database logs follow the same production retention gate
# as the application and authorization service logs.
resource "aws_cloudwatch_log_group" "rds_postgresql" {
  name              = "/aws/rds/instance/${local.prefix}-postgres/postgresql"
  retention_in_days = var.application_log_retention_days
}

resource "aws_cloudwatch_log_group" "rds_upgrade" {
  name              = "/aws/rds/instance/${local.prefix}-postgres/upgrade"
  retention_in_days = var.application_log_retention_days
}

resource "aws_cloudwatch_log_group" "openfga_rds_postgresql" {
  name              = "/aws/rds/instance/${local.prefix}-openfga-postgres/postgresql"
  retention_in_days = var.application_log_retention_days
}

resource "aws_cloudwatch_log_group" "openfga_rds_upgrade" {
  name              = "/aws/rds/instance/${local.prefix}-openfga-postgres/upgrade"
  retention_in_days = var.application_log_retention_days
}

resource "aws_db_instance" "main" {
  identifier            = "${local.prefix}-postgres"
  engine                = local.restore_app_db ? null : "postgres"
  engine_version        = local.restore_app_db ? null : var.db_engine_version
  instance_class        = var.db_instance_class
  allocated_storage     = local.restore_app_db ? null : var.db_allocated_storage
  max_allocated_storage = local.restore_app_db ? null : var.db_allocated_storage * 4
  storage_type          = local.restore_app_db ? null : "gp3"
  storage_encrypted     = true
  # An encrypted snapshot retains its KMS key. Cross-Region operators must copy
  # the snapshot under an accessible Region-local key before planning a restore.
  kms_key_id          = local.restore_app_db ? null : aws_kms_key.data.arn
  snapshot_identifier = local.restore_app_db ? var.restore_app_db_snapshot_identifier : null

  # Snapshot restores inherit database/master-user names. The rehearsal
  # preflight proves they match these configured connection-string values.
  db_name  = local.restore_app_db ? null : var.db_name
  username = local.restore_app_db ? null : var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name

  multi_az                              = var.db_multi_az
  publicly_accessible                   = false
  network_type                          = "IPV4"
  backup_retention_period               = var.db_backup_retention_days
  backup_window                         = "03:00-04:00"
  maintenance_window                    = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade            = true
  allow_major_version_upgrade           = false
  deletion_protection                   = true
  delete_automated_backups              = false
  skip_final_snapshot                   = false
  final_snapshot_identifier             = "${local.prefix}-final-snapshot"
  copy_tags_to_snapshot                 = true
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_enhanced_monitoring.arn
  database_insights_mode                = "standard"
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  lifecycle {
    # snapshot_identifier is a create-only seed. Ignoring later changes avoids
    # accidentally recreating a validated rehearsal from an old recovery point.
    ignore_changes = [snapshot_identifier]
  }

  # pgvector is a Postgres extension — it's installed via CREATE EXTENSION in
  # the Alembic migration (0001_sprint1_core.py). RDS Postgres 15 ships with
  # it pre-bundled; no extra RDS option group is needed.

  depends_on = [
    aws_cloudwatch_log_group.rds_postgresql,
    aws_cloudwatch_log_group.rds_upgrade,
    aws_iam_role_policy_attachment.rds_enhanced_monitoring,
  ]
}

# OpenFGA's production guidance recommends an exclusive datastore. Keeping the
# authorization database independent prevents a report/query spike or an app
# schema operation from exhausting the security control's connection budget.
resource "aws_db_instance" "openfga" {
  identifier            = "${local.prefix}-openfga-postgres"
  engine                = local.restore_openfga_db ? null : "postgres"
  engine_version        = local.restore_openfga_db ? null : var.db_engine_version
  instance_class        = var.openfga_db_instance_class
  allocated_storage     = local.restore_openfga_db ? null : var.openfga_db_allocated_storage
  max_allocated_storage = local.restore_openfga_db ? null : var.openfga_db_allocated_storage * 4
  storage_type          = local.restore_openfga_db ? null : "gp3"
  storage_encrypted     = true
  kms_key_id            = local.restore_openfga_db ? null : aws_kms_key.data.arn
  snapshot_identifier   = local.restore_openfga_db ? var.restore_openfga_db_snapshot_identifier : null

  db_name  = local.restore_openfga_db ? null : var.openfga_db_name
  username = local.restore_openfga_db ? null : var.openfga_db_username
  password = random_password.openfga_db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.openfga_rds.id]
  parameter_group_name   = aws_db_parameter_group.postgres.name

  multi_az                              = var.openfga_db_multi_az
  publicly_accessible                   = false
  network_type                          = "IPV4"
  backup_retention_period               = var.db_backup_retention_days
  backup_window                         = "01:00-02:00"
  maintenance_window                    = "sun:02:00-sun:03:00"
  auto_minor_version_upgrade            = true
  allow_major_version_upgrade           = false
  deletion_protection                   = true
  delete_automated_backups              = false
  skip_final_snapshot                   = false
  final_snapshot_identifier             = "${local.prefix}-openfga-final-snapshot"
  copy_tags_to_snapshot                 = true
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_enhanced_monitoring.arn
  database_insights_mode                = "standard"
  performance_insights_enabled          = true
  performance_insights_retention_period = 7

  lifecycle {
    ignore_changes = [snapshot_identifier]
  }

  depends_on = [
    aws_cloudwatch_log_group.openfga_rds_postgresql,
    aws_cloudwatch_log_group.openfga_rds_upgrade,
    aws_iam_role_policy_attachment.rds_enhanced_monitoring,
  ]
}
