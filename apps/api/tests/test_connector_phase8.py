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

FRAMEWORK_OPTIONAL_CATALOG_APPS = {"remote_mcp"}
CUSTOM_ACTION_CATALOG_APPS = {
    "custom_http": {"discover_schema", "request"},
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
async def test_every_catalog_app_seeds_framework_actions_and_policy():
    from connectors.oauth_apps import available_apps
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors

    repo = InMemoryConnectorRepository()
    await seed_builtin_connectors(repo)
    apps = {app["id"]: app for app in available_apps()}

    assert PHASE8_BUILT_INS.issubset(apps)

    for provider, app in apps.items():
        if provider in FRAMEWORK_OPTIONAL_CATALOG_APPS:
            continue
        connector = await repo.get_connector(provider, tenant_id="default")
        assert connector is not None, f"{provider} must be seedable as a framework connector"
        actions = await repo.list_actions(provider)
        action_names = {action["name"] for action in actions}
        expected_custom_actions = CUSTOM_ACTION_CATALOG_APPS.get(provider)
        if expected_custom_actions:
            assert expected_custom_actions.issubset(action_names), f"{provider} missing custom actions"
            continue

        assert {"search", "read", "write"}.issubset(action_names), f"{provider} missing read/search/write actions"
        assert any(action["risk_level"] != "read" or action["approval_required"] for action in actions), (
            f"{provider} must model write/risky actions for approval policy"
        )
        assert connector["auth_type"] == app["auth_type"]
