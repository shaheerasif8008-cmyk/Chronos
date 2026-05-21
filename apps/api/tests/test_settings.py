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
