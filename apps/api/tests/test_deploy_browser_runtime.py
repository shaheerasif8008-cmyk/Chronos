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
