resource "aws_ecr_repository" "api" {
  name                 = "${local.prefix}-api"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "web" {
  name                 = "${local.prefix}-web"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Pre-create the DR repositories so replicated images retain the same immutable
# tag policy as primary. Registry replication alone creates destination
# repositories with service defaults and does not copy repository settings.
resource "aws_ecr_repository" "api_dr" {
  provider             = aws.dr
  name                 = aws_ecr_repository.api.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "web_dr" {
  provider             = aws.dr
  name                 = aws_ecr_repository.web.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_replication_configuration" "dr" {
  count = var.backups_enabled ? 1 : 0

  replication_configuration {
    rule {
      destination {
        region      = var.backup_copy_region
        registry_id = data.aws_caller_identity.current.account_id
      }

      repository_filter {
        filter      = "${local.prefix}-"
        filter_type = "PREFIX_MATCH"
      }
    }
  }

  depends_on = [
    aws_ecr_repository.api_dr,
    aws_ecr_repository.web_dr,
  ]
}

# Keep the last 10 images to bound storage costs.
resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "web" {
  repository = aws_ecr_repository.web.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "api_dr" {
  provider   = aws.dr
  repository = aws_ecr_repository.api_dr.name
  policy     = aws_ecr_lifecycle_policy.api.policy
}

resource "aws_ecr_lifecycle_policy" "web_dr" {
  provider   = aws.dr
  repository = aws_ecr_repository.web_dr.name
  policy     = aws_ecr_lifecycle_policy.web.policy
}
