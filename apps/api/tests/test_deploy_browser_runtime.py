from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_api_docker_image_installs_playwright_chromium_by_default():
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text()

    assert "ARG INSTALL_PLAYWRIGHT=1" in dockerfile
    assert "playwright install chromium --with-deps" in dockerfile


def test_aws_deploy_keeps_browser_runtime_enabled():
    workflow = (ROOT / ".github/workflows/deploy-aws.yml").read_text()

    assert "INSTALL_PLAYWRIGHT=1" in workflow
    assert "INSTALL_PLAYWRIGHT=0" not in workflow


def test_deploy_configs_set_api_environment_to_production():
    ecs = (ROOT / "infra/ecs.tf").read_text()
    render = (ROOT / "render.yaml").read_text()

    assert 'name = "ENVIRONMENT"' in ecs
    assert 'value = "production"' in ecs
    assert "key: ENVIRONMENT" in render
    assert "value: production" in render


def test_github_oidc_trust_is_limited_to_main_deploy_workflow():
    iam = (ROOT / "infra/iam.tf").read_text()

    assert 'repo:${var.github_org}/${var.github_repo}:*' not in iam
    assert "refs/heads/main" in iam
    assert "token.actions.githubusercontent.com:workflow_ref" in iam
    assert 'Sid      = "SecretsRead"' not in iam


def test_production_rds_has_recoverable_backups():
    rds = (ROOT / "infra/rds.tf").read_text()

    assert "backup_retention_period      = 0" not in rds
    assert "skip_final_snapshot          = true" not in rds


def test_dev_compose_ports_are_bound_to_loopback_and_no_latest_openfga():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "openfga/openfga:latest" not in compose
    assert "- \"5432:5432\"" not in compose
    assert "- \"6379:6379\"" not in compose
    assert "- \"9000:9000\"" not in compose
    assert "- \"9001:9001\"" not in compose
    assert "- \"8080:8080\"" not in compose
    assert "- \"3010:3000\"" not in compose
