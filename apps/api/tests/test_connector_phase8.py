from __future__ import annotations

import pytest


PHASE8_BUILT_INS = {
    "gmail",
    "google_drive",
    "google_calendar",
    "slack",
    "github",
    "notion",
    "linear",
    "hubspot",
    "airtable",
    "jira",
    "outlook",
    "teams",
    "sharepoint_onedrive",
    "salesforce",
    "stripe",
    "webhooks",
    "custom_http",
    "remote_mcp",
}


def test_phase8_catalog_covers_required_built_ins_with_policy_metadata():
    from connectors.oauth_apps import available_apps

    apps = {app["id"]: app for app in available_apps()}

    assert PHASE8_BUILT_INS.issubset(apps)
    for provider in PHASE8_BUILT_INS:
        app = apps[provider]
        assert app["actions"], f"{provider} must expose catalog actions"
        assert app["risk_levels"], f"{provider} must expose risk levels"
        assert "policy" in app and app["policy"], f"{provider} must expose policy text"
        assert "auth_type" in app
        assert "sync_supported" in app


@pytest.mark.asyncio
async def test_phase8_app_connectors_seed_framework_actions_and_policy():
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)

    for provider in PHASE8_BUILT_INS - {"remote_mcp", "custom_http"}:
        connector = await repo.get_connector(provider, tenant_id="default")
        assert connector is not None, f"{provider} must be seedable as a framework connector"
        actions = await repo.list_actions(provider)
        action_names = {action["name"] for action in actions}
        assert {"search", "read"}.issubset(action_names), f"{provider} missing read/search actions"
        assert any(action["risk_level"] != "read" or action["approval_required"] for action in actions), (
            f"{provider} must model write/risky actions for approval policy"
        )

    custom = await repo.get_connector("custom_http", tenant_id="default")
    assert custom is not None
    custom_actions = {action["name"] for action in await repo.list_actions("custom_http")}
    assert {"request", "discover_schema"}.issubset(custom_actions)
