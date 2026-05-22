import pytest


def test_require_admin_rejects_non_admin():
    from fastapi import HTTPException
    from core.models import Member
    from core.settings_store import require_admin

    with pytest.raises(HTTPException) as exc:
        require_admin(Member(id="member-1", organization_id="default", email="viewer@example.com", role="viewer"))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_section_enforces_org_admin(monkeypatch):
    from fastapi import HTTPException
    from core.models import Member
    from routers import settings

    async def fail_save(*args, **kwargs):
        raise AssertionError("non-admin should not persist organization settings")

    monkeypatch.setattr(settings, "save_settings_doc", fail_save)

    with pytest.raises(HTTPException) as exc:
        await settings.update_section(
            "organization",
            settings.SettingsPatch(values={"organization_name": "Blocked"}),
            Member(id="member-1", organization_id="default", email="viewer@example.com", role="viewer"),
        )

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_update_section_persists_general_settings_for_member(monkeypatch):
    from core.models import Member
    from routers import settings

    saved = {}

    async def fake_save(member, section, values):
        saved.update({"member": member.id, "section": section, "values": values})
        return {"workspace_name": values["workspace_name"]}

    monkeypatch.setattr(settings, "save_settings_doc", fake_save)

    result = await settings.update_section(
        "general",
        settings.SettingsPatch(values={"workspace_name": "Ops"}),
        Member(id="member-1", organization_id="default", email="admin@example.com", role="viewer"),
    )

    assert saved == {"member": "member-1", "section": "general", "values": {"workspace_name": "Ops"}}
    assert result == {"section": "general", "values": {"workspace_name": "Ops"}}


@pytest.mark.asyncio
async def test_settings_overview_includes_connector_health(monkeypatch):
    from core.models import Member
    from routers import settings

    async def fake_get_settings_doc(member, section):
        return dict(settings.DEFAULTS.get(section, {}))

    async def fake_current_org(member):
        return {"id": "default", "name": "Default"}

    async def fake_members(member):
        return []

    async def fake_connectors(member):
        return []

    async def fake_memory_stats(member):
        return {"active": 0, "deleted": 0}

    async def fake_check_connectors():
        return {"gmail": {"tier": "demo", "reason": "COMPOSIO_API_KEY is not set"}}

    async def fake_permission(*args, **kwargs):
        return True

    monkeypatch.setattr(settings, "get_settings_doc", fake_get_settings_doc)
    monkeypatch.setattr(settings, "_current_org", fake_current_org)
    monkeypatch.setattr(settings, "_members", fake_members)
    monkeypatch.setattr(settings, "_connectors", fake_connectors)
    monkeypatch.setattr(settings, "_memory_stats", fake_memory_stats)
    monkeypatch.setattr(settings, "check_connectors", fake_check_connectors)
    monkeypatch.setattr(settings.permissions, "check", fake_permission)

    overview = await settings.overview(
        Member(id="member-1", organization_id="default", email="admin@example.com", role="admin")
    )

    assert overview["runtime_health"]["connectors"]["gmail"]["tier"] == "demo"
