from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_all_third_party_workflow_actions_are_pinned_to_commit_shas():
    unpinned: list[str] = []
    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for line_number, line in enumerate(workflow.read_text().splitlines(), start=1):
            match = re.match(r"\s*-?\s*uses:\s*([^\s#]+)", line)
            if not match:
                continue
            action = match.group(1)
            if action.startswith("./"):
                continue
            if not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action):
                unpinned.append(f"{workflow.name}:{line_number}: {action}")

    assert not unpinned, "unpinned workflow actions:\n" + "\n".join(unpinned)


def test_release_gate_requires_current_reviewed_main_sha_and_exact_ci():
    gate = (ROOT / ".github/scripts/verify-reviewed-ci-sha.sh").read_text()
    deploy = (ROOT / ".github/workflows/deploy-aws.yml").read_text()

    assert 'SOURCE_SHA must be a full 40-character lowercase Git SHA' in gate
    assert 'REQUIRE_BRANCH_HEAD="${REQUIRE_BRANCH_HEAD:-false}"' in gate
    assert '.event == "push"' in gate
    assert '.conclusion == "success"' in gate
    assert '.head_repository.full_name == $repository' in gate
    assert 'reviewDecision' in gate
    assert '"APPROVED"' in gate
    assert 'EXPECTED_CI_RUN_ID' in gate

    assert 'name: Require reviewed main SHA and successful CI' in deploy
    assert 'EXPECTED_CI_RUN_ID: ${{ github.event.workflow_run.id' in deploy
    assert 'REQUIRE_BRANCH_HEAD: "true"' in deploy
    assert 'ref: ${{ needs.release-gate.outputs.source_sha }}' in deploy
    assert 'github.event.workflow_run.head_sha || github.sha' not in deploy.split(
        'build:', maxsplit=1
    )[1]


def test_aws_release_cannot_skip_migrations_and_blocks_vulnerable_images():
    deploy = (ROOT / ".github/workflows/deploy-aws.yml").read_text()
    iam = (ROOT / "infra/iam.tf").read_text()

    assert "run_migrations" not in deploy
    assert "if: github.event.inputs.bootstrap_images_only != 'true'" in deploy
    assert "needs.migrate.result == 'success'" in deploy
    assert "needs.migrate.result == 'skipped'" not in deploy
    assert 'bootstrap_images_only:' in deploy
    assert 'Services and migrations were intentionally not changed.' in deploy

    assert 'get-registry-scanning-configuration' in deploy
    assert 'start-image-scan' in deploy
    assert 'describe-image-scan-findings' in deploy
    assert 'SCAN_TYPE" == "BASIC"' in deploy
    assert 'SCAN_TYPE" == "ENHANCED"' in deploy
    assert '.imageScanFindings.imageScanCompletedAt != null' in deploy
    assert '.imageScanFindings.vulnerabilitySourceUpdatedAt != null' in deploy
    assert '.findingSeverityCounts.HIGH // 0' in deploy
    assert '.findingSeverityCounts.CRITICAL // 0' in deploy
    assert 'high > 0 || critical > 0' in deploy
    assert deploy.index('Wait for ECR scans') < deploy.index('Run migrations')

    for action in (
        "ecr:GetRegistryScanningConfiguration",
        "ecr:StartImageScan",
        "ecr:DescribeImageScanFindings",
    ):
        assert action in iam
    assert 'Sid      = "SentryBuildSecret"' in iam
    assert 'secret:${local.prefix}/sentry_dsn-*' in iam


def test_cross_industry_suite_is_deterministic_in_ci_and_live_on_exact_release_sha():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = (ROOT / ".github/workflows/deploy-aws.yml").read_text()

    assert "python -m evals.cross_industry --validate" in ci
    assert "needs: [release-gate, build, migrate]" in deploy
    assert "needs.release-gate.result == 'success'" in deploy
    assert "ref: ${{ needs.release-gate.outputs.source_sha }}" in deploy
    assert "name: Require live evaluation identity configuration" in deploy
    assert "External launch blocker" in deploy
    assert "CHRONOS_EVAL_BEARER_TOKEN" in deploy
    assert "CHRONOS_EVAL_ORG_ID" in deploy
    assert "CHRONOS_EVAL_RELEASE_SHA: ${{ needs.release-gate.outputs.source_sha }}" in deploy

    live_step = deploy.index("name: Require same-commit live cross-industry evaluation")
    for required_flag in (
        "--live",
        "--acknowledge-test-data",
        "--expected-org-id",
        "--release-sha",
        "--include-responses",
    ):
        assert required_flag in deploy[live_step:]

    revision_proof = deploy.index("Verify requested task revisions are active")
    smoke_test = deploy.index("Smoke-test the production edge")
    evidence_upload = deploy.index("Upload cross-industry evaluation evidence")
    rollback = deploy.index("Roll back services after a failed deployment")
    assert revision_proof < smoke_test < live_step < evidence_upload < rollback


def test_desktop_release_verifies_source_and_mounted_artifact_before_publish():
    desktop = (ROOT / ".github/workflows/desktop-release.yml").read_text()

    assert 'name: Require reviewed main SHA and successful CI' in desktop
    assert "REQUIRE_BRANCH_HEAD: ${{ github.event_name == 'workflow_dispatch' }}" in desktop
    assert 'ref: ${{ needs.release-gate.outputs.source_sha }}' in desktop
    assert 'swift run -c release ChronosDesktop --self-test' in desktop

    required_checks = (
        'shasum -a 256 -c',
        'hdiutil verify',
        'xcrun stapler validate',
        'hdiutil attach -readonly -nobrowse',
        'codesign --verify --deep --strict',
        'spctl --assess --type execute',
        'CFBundleShortVersionString',
        'Contents/MacOS/ChronosDesktop" --self-test',
    )
    for check in required_checks:
        assert check in desktop

    verify_position = desktop.index('Verify checksum, package, Gatekeeper')
    upload_position = desktop.index('Upload release artifact')
    publish_position = desktop.index('Publish tagged release')
    assert verify_position < upload_position < publish_position
