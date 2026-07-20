from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_api_docker_image_uses_remote_browser_without_local_chromium_by_default():
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text()

    assert "ARG INSTALL_PLAYWRIGHT=0" in dockerfile
    assert "playwright install chromium --with-deps" in dockerfile
    assert "--no-sandbox" not in dockerfile


def test_aws_deploy_does_not_install_process_local_browser_runtime():
    workflow = (ROOT / ".github/workflows/deploy-aws.yml").read_text()

    assert "INSTALL_PLAYWRIGHT=0" in workflow
    assert "INSTALL_PLAYWRIGHT=1" not in workflow


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
    assert '"token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"' in iam
    # GitHub's OIDC subject is the enforceable branch identity for this
    # workflow. Nonexistent workflow_ref/job_workflow_ref claim keys would make
    # every assume-role request fail instead of narrowing it.
    assert "token.actions.githubusercontent.com:workflow_ref" not in iam
    assert "token.actions.githubusercontent.com:job_workflow_ref" not in iam
    assert 'Sid      = "SecretsRead"' not in iam


def test_production_rds_has_recoverable_backups():
    rds = (ROOT / "infra/rds.tf").read_text()

    assert "backup_retention_period      = 0" not in rds
    assert "skip_final_snapshot          = true" not in rds
    assert "db_backup_retention_days" in rds


def test_production_infra_has_waf_alerting_and_cross_region_restore_proof():
    main = (ROOT / "infra/main.tf").read_text()
    waf = (ROOT / "infra/waf.tf").read_text()
    monitoring = (ROOT / "infra/monitoring.tf").read_text()
    backup = (ROOT / "infra/backup.tf").read_text()

    assert 'resource "terraform_data" "production_guard"' in main
    assert "Production apply blocked" in main
    assert 'AWSManagedRulesAmazonIpReputationList' in waf
    assert 'name     = "auth-rate-limit"' in waf
    assert 'aws_wafv2_web_acl_association' in waf
    assert 'aws_cloudwatch_event_rule" "ecs_deployment_failed' in monitoring
    assert 'aws_cloudwatch_dashboard" "operations' in monitoring
    assert 'enable_continuous_backup = true' in backup
    assert 'aws_backup_vault_lock_configuration" "dr' in backup
    assert 'aws_backup_restore_testing_selection" "rds' in backup


def test_dedicated_canva_connector_credentials_are_required_and_injected():
    main = (ROOT / "infra/main.tf").read_text()
    secrets = (ROOT / "infra/secrets.tf").read_text()
    variables = (ROOT / "infra/variables.tf").read_text()

    for name in ("canva_client_id", "canva_client_secret"):
        assert f'variable "{name}"' in variables
        assert f"var.{name}" in main
        assert f"{name}" in secrets


def test_restore_rehearsal_rejects_agent_publication_credentials():
    main = (ROOT / "infra/main.tf").read_text()
    restore_guard = main.split(
        "restore_external_credentials_absent =", maxsplit=1
    )[1].split("}", maxsplit=1)[0]

    for name in (
        "slack_client_id",
        "slack_client_secret",
        "slack_signing_secret",
        "microsoft_client_id",
        "microsoft_client_secret",
        "teams_bot_app_id",
        "sendgrid_inbound_public_key",
    ):
        assert f"var.{name}" in restore_guard


def test_openfga_datastore_migration_is_one_off_not_per_replica():
    ecs = (ROOT / "infra/ecs.tf").read_text()

    assert 'resource "aws_ecs_task_definition" "openfga_migrate"' in ecs
    openfga_service_definition = ecs.split(
        'resource "aws_ecs_task_definition" "openfga"', maxsplit=1
    )[1].split(
        '# ── Migration task definition', maxsplit=1
    )[0]
    assert 'dependsOn = [{ containerName = "openfga-migrate"' not in openfga_service_definition
    assert 'name      = "openfga-migrate"' not in openfga_service_definition


def test_production_images_and_bootstrap_counts_are_terraform_enforced():
    ecr = (ROOT / "infra/ecr.tf").read_text()
    ecs = (ROOT / "infra/ecs.tf").read_text()
    deploy = (ROOT / ".github/workflows/deploy-aws.yml").read_text()

    assert ecr.count('image_tag_mutability = "IMMUTABLE"') == 4
    assert 'resource "aws_ecr_replication_configuration" "dr"' in ecr
    assert 'provider             = aws.dr' in ecr
    assert ":latest" not in deploy
    # Keeping desired_count in ignore_changes strands first-deploy services at
    # zero after platform_bootstrap_mode is disabled.
    assert "ignore_changes = [task_definition, desired_count]" not in ecs


def test_terraform_guards_credential_destinations_and_access_token_ttl():
    main = (ROOT / "infra/main.tf").read_text()
    variables = (ROOT / "infra/variables.tf").read_text()

    assert "Cognito, OpenRouter, and Langfuse credential destinations" in main
    assert 'https://openrouter\\\\.ai/api/v1' in main
    assert "var.access_token_expire_minutes >= 5" in main
    assert "var.access_token_expire_minutes <= 1440" in main
    for variable in (
        "cognito_domain",
        "openrouter_api_base",
        "langfuse_host",
        "access_token_expire_minutes",
    ):
        block = variables.split(f'variable "{variable}"', maxsplit=1)[1].split(
            "\n}", maxsplit=1
        )[0]
        assert "validation {" in block
        assert 'startswith(lower(var.environment), "prod")' in block


def test_alb_uses_readiness_while_ecs_uses_process_liveness():
    alb = (ROOT / "infra/alb.tf").read_text()
    ecs = (ROOT / "infra/ecs.tf").read_text()

    assert 'path                = "/ready"' in alb
    assert 'curl -sf http://localhost:8000/health/live' in ecs


def test_api_shutdown_is_bounded_inside_orchestrator_stop_window():
    dockerfile = (ROOT / "apps/api/Dockerfile").read_text()
    ecs = (ROOT / "infra/ecs.tf").read_text()
    render = (ROOT / "render.yaml").read_text()

    assert '"--timeout-graceful-shutdown", "45"' in dockerfile
    assert "--timeout-graceful-shutdown 45" in render
    api_definition = ecs.split(
        'resource "aws_ecs_task_definition" "api"', maxsplit=1
    )[1].split(
        '# ── Web task definition', maxsplit=1
    )[0]
    assert re.search(r"stopTimeout\s*=\s*90", api_definition)


def test_dev_compose_ports_are_bound_to_loopback_and_no_latest_openfga():
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "openfga/openfga:latest" not in compose
    assert "- \"5432:5432\"" not in compose
    assert "- \"6379:6379\"" not in compose
    assert "- \"9000:9000\"" not in compose
    assert "- \"9001:9001\"" not in compose
    assert "- \"8080:8080\"" not in compose
    assert "- \"3010:3000\"" not in compose


def test_github_actions_are_pinned_to_immutable_commits():
    workflows = ROOT / ".github/workflows"

    for path in workflows.glob("*.y*ml"):
        for line in path.read_text().splitlines():
            match = re.search(r"\buses:\s*([^\s#]+)", line)
            if match is None:
                continue
            action = match.group(1)
            assert re.search(r"@[0-9a-f]{40}$", action), (
                f"{path.relative_to(ROOT)} must pin {action!r} to a 40-character commit SHA"
            )
