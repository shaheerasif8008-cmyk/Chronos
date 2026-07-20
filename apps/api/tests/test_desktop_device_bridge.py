from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.config import settings
from core.desktop_bridge import (
    DesktopBridgeError,
    DesktopBridgeService,
    MemoryDesktopBridgeStore,
    _pair_code_hash,
    command_signing_message,
    result_signing_message,
    reveal_command_secret,
)


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(settings, "vault_encryption_key", "71" * 32)
    monkeypatch.setattr(settings, "jwt_secret", "desktop-bridge-test-jwt-secret")
    store = MemoryDesktopBridgeStore()
    service = DesktopBridgeService(store)

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_audit", no_audit)
    return service, store


async def _paired(
    service: DesktopBridgeService,
    *,
    organization_id: str = "org-a",
    member_id: str = "member-a",
    name: str = "Alice's Mac",
):
    code = await service.create_pair_code(
        organization_id=organization_id, member_id=member_id
    )
    credentials = await service.pair_device(
        pair_code=code["pair_code"],
        name=name,
        platform="macos",
        client_version="1.0.0",
        capabilities={"folder_bridge": True},
    )
    device = await service.authenticate(
        credentials["device_token"], expected_device_id=credentials["device_id"]
    )
    return code, credentials, device


async def _grant(service: DesktopBridgeService, device: dict, *, client_id: str = "client-grant-1"):
    return await service.register_grant(
        device,
        client_grant_id=client_id,
        display_name="Client Files",
    )


def _result_signature(secret: bytes, *, device_id: str, command_id: str, nonce: str, status: str, raw: bytes, error_code=None):
    return hmac.new(
        secret,
        result_signing_message(
            command_id=command_id,
            device_id=device_id,
            nonce=nonce,
            status=status,
            error_code=error_code,
            result=raw,
        ),
        hashlib.sha256,
    ).hexdigest()


@pytest.mark.asyncio
async def test_pairing_stores_only_hashes_and_tenant_bound_encrypted_secret(bridge):
    service, store = bridge
    _code, credentials, device = await _paired(service)

    stored = store.devices[credentials["device_id"]]
    assert credentials["device_token"] not in repr(stored)
    assert credentials["command_secret_b64"] not in repr(stored)
    assert stored["token_hash"] == hashlib.sha256(
        credentials["device_token"].encode()
    ).hexdigest()
    assert stored["encrypted_command_secret"].startswith("enc:v1:")
    secret = base64.b64decode(credentials["command_secret_b64"])
    assert reveal_command_secret(
        stored["encrypted_command_secret"],
        organization_id="org-a",
        device_id=credentials["device_id"],
    ) == secret
    with pytest.raises(DesktopBridgeError, match="could not be decrypted"):
        reveal_command_secret(
            stored["encrypted_command_secret"],
            organization_id="org-b",
            device_id=credentials["device_id"],
        )
    assert device["organization_id"] == "org-a"
    assert device["member_id"] == "member-a"


@pytest.mark.asyncio
async def test_pair_code_is_short_lived_one_time_and_never_stored_plaintext(bridge):
    service, store = bridge
    code = await service.create_pair_code(organization_id="org-a", member_id="member-a")
    assert code["pair_code"] not in repr(store.pair_codes)
    assert _pair_code_hash(code["pair_code"]) in store.pair_codes

    await service.pair_device(
        pair_code=code["pair_code"], name="Mac", platform="macos", client_version=None, capabilities={}
    )
    with pytest.raises(DesktopBridgeError) as reused:
        await service.pair_device(
            pair_code=code["pair_code"], name="Mac 2", platform="macos", client_version=None, capabilities={}
        )
    assert reused.value.code == "invalid_pair_code"

    expired = await service.create_pair_code(organization_id="org-a", member_id="member-a")
    store.pair_codes[_pair_code_hash(expired["pair_code"])]["expires_at"] -= timedelta(hours=1)
    with pytest.raises(DesktopBridgeError) as stale:
        await service.pair_device(
            pair_code=expired["pair_code"], name="Old Mac", platform="macos", client_version=None, capabilities={}
        )
    assert stale.value.code == "expired_pair_code"


@pytest.mark.asyncio
async def test_device_token_auth_is_scoped_revocable_and_cross_device_safe(bridge):
    service, _store = bridge
    _code_a, cred_a, device_a = await _paired(service, name="Mac A")
    _code_b, cred_b, _device_b = await _paired(service, name="Mac B")

    with pytest.raises(DesktopBridgeError) as bad:
        await service.authenticate("chd_" + "x" * 64)
    assert bad.value.status_code == 401
    with pytest.raises(DesktopBridgeError) as mismatch:
        await service.authenticate(
            cred_a["device_token"], expected_device_id=cred_b["device_id"]
        )
    assert mismatch.value.status_code == 403

    await service.disconnect(device_a)
    with pytest.raises(DesktopBridgeError) as revoked:
        await service.authenticate(cred_a["device_token"])
    assert revoked.value.status_code == 401


@pytest.mark.asyncio
async def test_grants_never_retain_absolute_client_paths_and_are_member_isolated(bridge):
    service, store = bridge
    _code_a, _cred_a, device_a = await _paired(service)
    grant = await _grant(service, device_a)
    stored = store.grants[grant["id"]]
    assert stored["folder_path"] is None
    assert stored["client_grant_id"] == "client-grant-1"
    assert stored["folder_display_name"] == "Client Files"
    assert "/Users/" not in repr(stored)

    with pytest.raises(DesktopBridgeError) as absolute_name:
        await service.register_grant(
            device_a,
            client_grant_id="second",
            display_name="/Users/alice/Client Files",
        )
    assert absolute_name.value.code == "invalid_display_name"

    with pytest.raises(DesktopBridgeError) as cross_member:
        await service.enqueue(
            organization_id="org-a",
            member_id="member-b",
            grant_id=grant["id"],
            command_type="list_files",
            payload={"path": "."},
        )
    assert cross_member.value.status_code == 404
    with pytest.raises(DesktopBridgeError) as cross_org:
        await service.enqueue(
            organization_id="org-b",
            member_id="member-a",
            grant_id=grant["id"],
            command_type="list_files",
            payload={"path": "."},
        )
    assert cross_org.value.status_code == 404


@pytest.mark.asyncio
async def test_command_envelope_and_signed_result_are_exact_idempotent_and_replay_safe(bridge):
    service, _store = bridge
    _code, credentials, device = await _paired(service)
    grant = await _grant(service, device)
    queued = await service.enqueue(
        organization_id="org-a",
        member_id="member-a",
        grant_id=grant["id"],
        command_type="read_file",
        payload={"path": "reports/q2.txt"},
    )
    envelopes = await service.lease(device)
    assert len(envelopes) == 1
    envelope = envelopes[0]
    payload = base64.b64decode(envelope["payload_b64"])
    decoded = json.loads(payload)
    assert decoded["client_grant_id"] == "client-grant-1"
    assert decoded["grant_id"] == grant["id"]

    secret = base64.b64decode(credentials["command_secret_b64"])
    expected_command_signature = hmac.new(
        secret,
        command_signing_message(
            command_id=envelope["command_id"],
            device_id=envelope["device_id"],
            nonce=envelope["nonce"],
            command_type=envelope["command_type"],
            expires_at=envelope["expires_at"],
            payload=payload,
        ),
        hashlib.sha256,
    ).hexdigest()
    assert envelope["signature"] == expected_command_signature

    result = json.dumps({"status": "success", "content": "hello"}, separators=(",", ":")).encode()
    nonce = "result-nonce-1"
    signature = _result_signature(
        secret,
        device_id=device["id"],
        command_id=queued["command_id"],
        nonce=nonce,
        status="succeeded",
        raw=result,
    )
    with pytest.raises(DesktopBridgeError) as bad_signature:
        await service.submit_result(
            device,
            command_id=queued["command_id"],
            nonce=nonce,
            status="succeeded",
            error_code=None,
            result_b64=base64.b64encode(result).decode(),
            signature="0" * 64,
        )
    assert bad_signature.value.code == "invalid_result_signature"

    accepted = await service.submit_result(
        device,
        command_id=queued["command_id"],
        nonce=nonce,
        status="succeeded",
        error_code=None,
        result_b64=base64.b64encode(result).decode(),
        signature=signature,
    )
    assert accepted == {
        "command_id": queued["command_id"],
        "status": "succeeded",
        "accepted": True,
        "idempotent": False,
    }
    repeated = await service.submit_result(
        device,
        command_id=queued["command_id"],
        nonce=nonce,
        status="succeeded",
        error_code=None,
        result_b64=base64.b64encode(result).decode(),
        signature=signature,
    )
    assert repeated["idempotent"] is True
    state = await service.command_result(queued["command_id"])
    assert state["result"]["content"] == "hello"

    second = await service.enqueue(
        organization_id="org-a",
        member_id="member-a",
        grant_id=grant["id"],
        command_type="list_files",
        payload={"path": "."},
    )
    await service.lease(device)
    second_raw = b"{}"
    second_signature = _result_signature(
        secret,
        device_id=device["id"],
        command_id=second["command_id"],
        nonce=nonce,
        status="succeeded",
        raw=second_raw,
    )
    with pytest.raises(DesktopBridgeError) as replay:
        await service.submit_result(
            device,
            command_id=second["command_id"],
            nonce=nonce,
            status="succeeded",
            error_code=None,
            result_b64=base64.b64encode(second_raw).decode(),
            signature=second_signature,
        )
    assert replay.value.code == "result_nonce_reused"


@pytest.mark.asyncio
async def test_expired_lease_retries_then_expires_at_attempt_limit(bridge):
    service, store = bridge
    _code, _credentials, device = await _paired(service)
    grant = await _grant(service, device)
    queued = await service.enqueue(
        organization_id="org-a",
        member_id="member-a",
        grant_id=grant["id"],
        command_type="list_files",
        payload={"path": "."},
    )
    first = (await service.lease(device, lease_seconds=10))[0]
    assert first["attempt"] == 1

    command = store.commands[queued["command_id"]]
    command["lease_expires_at"] -= timedelta(seconds=20)
    second = (await service.lease(device, lease_seconds=10))[0]
    assert second["attempt"] == 2
    command["lease_expires_at"] -= timedelta(seconds=20)
    third = (await service.lease(device, lease_seconds=10))[0]
    assert third["attempt"] == 3
    command["lease_expires_at"] -= timedelta(seconds=20)
    assert await service.lease(device, lease_seconds=10) == []
    assert (await service.command_result(queued["command_id"]))["status"] == "expired"


@pytest.mark.asyncio
async def test_device_revoke_cancels_commands_and_revokes_grants(bridge):
    service, store = bridge
    _code, _credentials, device = await _paired(service)
    grant = await _grant(service, device)
    queued = await service.enqueue(
        organization_id="org-a",
        member_id="member-a",
        grant_id=grant["id"],
        command_type="open_app",
        payload={"app": "TextEdit"},
    )
    revoked = await service.revoke_device(
        device_id=device["id"],
        organization_id="org-a",
        member_id="member-a",
        actor_id="member-a",
    )
    assert revoked["status"] == "revoked"
    assert store.commands[queued["command_id"]]["status"] == "cancelled"
    assert store.grants[grant["id"]]["status"] == "revoked"


@pytest.mark.asyncio
async def test_notify_is_narrow_targeted_and_signed_like_every_other_command(bridge):
    service, store = bridge
    _code_a, credentials_a, device_a = await _paired(service)
    await _paired(
        service,
        organization_id="org-a",
        member_id="member-b",
        name="Bob's Mac",
    )
    await _paired(
        service,
        organization_id="org-b",
        member_id="member-a",
        name="Other tenant Mac",
    )

    queued = await service.enqueue_notification(
        organization_id="org-a",
        member_id="member-a",
        title="Task completed",
        body="Your scheduled report is ready.",
        category="success",
    )
    assert queued == {"status": "queued", "queued": 1}
    matching = [row for row in store.commands.values() if row["command_type"] == "notify"]
    assert len(matching) == 1
    assert json.loads(matching[0]["payload"]) == {
        "title": "Task completed",
        "body": "Your scheduled report is ready.",
        "category": "success",
    }
    assert matching[0]["grant_id"] is None

    envelope = (await service.lease(device_a))[0]
    assert envelope["command_type"] == "notify"
    payload = base64.b64decode(envelope["payload_b64"])
    secret = base64.b64decode(credentials_a["command_secret_b64"])
    assert envelope["signature"] == hmac.new(
        secret,
        command_signing_message(
            command_id=envelope["command_id"],
            device_id=device_a["id"],
            nonce=envelope["nonce"],
            command_type="notify",
            expires_at=envelope["expires_at"],
            payload=payload,
        ),
        hashlib.sha256,
    ).hexdigest()

    with pytest.raises(DesktopBridgeError) as unsafe_title:
        await service.enqueue_notification(
            organization_id="org-a",
            member_id="member-a",
            title="x" * 121,
            body="ok",
            category="info",
        )
    assert unsafe_title.value.code == "invalid_notification_title"
    with pytest.raises(DesktopBridgeError) as unsafe_category:
        await service.enqueue_notification(
            organization_id="org-a",
            member_id="member-a",
            title="Safe",
            body="ok",
            category="info\nopen-app",
        )
    assert unsafe_category.value.code == "invalid_notification_category"


@pytest.mark.asyncio
async def test_production_local_computer_routes_to_device_not_api_host(bridge, monkeypatch):
    service, store = bridge
    _code, _credentials, device = await _paired(service)
    grant = await _grant(service, device)

    from connectors.computer import ComputerConnector
    from core import desktop_bridge as bridge_module

    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(bridge_module, "desktop_bridge", service)
    connector = ComputerConnector()

    async def host_execution_must_not_run(*_args, **_kwargs):
        raise AssertionError("production local computer reached API-host execution")

    monkeypatch.setattr(connector, "_execute_local", host_execution_must_not_run)
    result = await connector.execute(
        "local_computer.list_files",
        {
            "grant_id": grant["id"],
            "path": "reports",
            "bridge_wait_seconds": 0,
            "__org_id": "org-a",
            "__member_id": "member-a",
        },
    )
    assert result.data["status"] == "queued"
    assert result.data["host_execution"] is False
    assert result.data["execution_boundary"] == "authenticated_desktop_device"
    command = store.commands[result.data["command_id"]]
    assert command["command_type"] == "list_files"
    assert json.loads(command["payload"])["path"] == "reports"

    with pytest.raises(PermissionError):
        await connector.execute(
            "local_computer.read_file",
            {
                "grant_id": grant["id"],
                "path": "../outside.txt",
                "bridge_wait_seconds": 0,
                "__org_id": "org-a",
                "__member_id": "member-a",
            },
        )


def test_migration_reserves_0051_and_never_adds_a_required_client_path():
    source = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0051_desktop_device_bridge.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0051_desktop_device_bridge"' in source
    assert 'down_revision = "0050_retention_controls"' in source
    assert 'op.alter_column("local_computer_grants", "folder_path", existing_type=sa.Text(), nullable=True)' in source
    assert 'sa.Column("token_hash"' in source
    assert 'sa.Column("encrypted_command_secret"' in source


@pytest.mark.asyncio
async def test_device_and_web_http_apis_enforce_their_distinct_auth_boundaries(bridge, monkeypatch):
    service, _store = bridge
    from core.auth import get_current_member
    from core.models import Member
    from routers import desktop_devices as router_module

    monkeypatch.setattr(router_module, "desktop_bridge", service)

    async def allow(*_args, **_kwargs):
        return True

    monkeypatch.setattr(router_module.permissions, "check", allow)
    app = FastAPI()
    app.include_router(router_module.router)
    current = {
        "member": Member(
            id="member-a",
            organization_id="org-a",
            email="alice@example.com",
            role="user",
        )
    }

    async def member_override():
        return current["member"]

    app.dependency_overrides[get_current_member] = member_override
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://api.example.test"
    ) as client:
        code_response = await client.post("/desktop-devices/pair-codes")
        assert code_response.status_code == 201
        pair_response = await client.post(
            "/desktop-devices/pair",
            json={
                "pair_code": code_response.json()["pair_code"],
                "name": "Alice's Mac",
                "platform": "macos",
                "client_version": "1.0",
                "capabilities": {"folder_bridge": True},
            },
        )
        assert pair_response.status_code == 201
        credentials = pair_response.json()
        device_id = credentials["device_id"]
        auth = {"Authorization": f"Bearer {credentials['device_token']}"}

        assert (
            await client.post(
                f"/desktop-devices/{device_id}/grants",
                json={"client_grant_id": "opaque-1", "display_name": "Client Files"},
            )
        ).status_code == 401
        grant_response = await client.post(
            f"/desktop-devices/{device_id}/grants",
            headers=auth,
            json={"client_grant_id": "opaque-1", "display_name": "Client Files"},
        )
        assert grant_response.status_code == 201
        grant = grant_response.json()
        assert "folder_path" not in grant

        listed = await client.get("/desktop-devices/")
        assert listed.status_code == 200
        assert [device["id"] for device in listed.json()] == [device_id]
        assert "token_hash" not in repr(listed.json())
        assert "encrypted_command_secret" not in repr(listed.json())

        current["member"] = Member(
            id="member-b",
            organization_id="org-b",
            email="bob@example.com",
            role="owner",
        )
        assert (await client.get("/desktop-devices/")).json() == []
        cross_revoke = await client.post(f"/desktop-devices/{device_id}/revoke")
        assert cross_revoke.status_code == 404

        current["member"] = Member(
            id="member-a",
            organization_id="org-a",
            email="alice@example.com",
            role="user",
        )
        revoked = await client.post(
            f"/desktop-devices/{device_id}/grants/opaque-1/revoke", headers=auth
        )
        assert revoked.status_code == 200
        assert revoked.json()["status"] == "revoked"
        cannot_revive = await client.post(
            f"/desktop-devices/{device_id}/grants",
            headers=auth,
            json={"client_grant_id": "opaque-1", "display_name": "Client Files"},
        )
        assert cannot_revive.status_code == 409
        assert cannot_revive.json()["detail"]["code"] == "grant_revoked"

        second_grant = await client.post(
            f"/desktop-devices/{device_id}/grants",
            headers=auth,
            json={"client_grant_id": "opaque-2", "display_name": "Reports"},
        )
        web_revoked = await client.post(
            f"/desktop-devices/grants/{second_grant.json()['id']}/revoke"
        )
        assert web_revoked.status_code == 200
        assert web_revoked.json()["status"] == "revoked"
        assert web_revoked.json()["revocation_command_id"]


@pytest.mark.asyncio
async def test_poll_result_and_disconnect_http_contract(bridge, monkeypatch):
    service, _store = bridge
    from routers import desktop_devices as router_module

    monkeypatch.setattr(router_module, "desktop_bridge", service)
    app = FastAPI()
    app.include_router(router_module.router)

    _code, credentials, device = await _paired(service)
    grant = await _grant(service, device)
    queued = await service.enqueue(
        organization_id="org-a",
        member_id="member-a",
        grant_id=grant["id"],
        command_type="exec",
        payload={"command": "printf ok", "timeout_seconds": 10},
    )
    auth = {"Authorization": f"Device {credentials['device_token']}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://api.example.test"
    ) as client:
        poll = await client.post(
            f"/desktop-devices/{device['id']}/commands/poll",
            headers=auth,
            json={"limit": 1, "lease_seconds": 30},
        )
        assert poll.status_code == 200
        envelope = poll.json()["commands"][0]
        assert envelope["command_id"] == queued["command_id"]
        assert envelope["command_type"] == "exec"

        raw = b'{"status":"success","stdout":"ok"}'
        nonce = "http-result-nonce"
        secret = base64.b64decode(credentials["command_secret_b64"])
        signature = _result_signature(
            secret,
            device_id=device["id"],
            command_id=queued["command_id"],
            nonce=nonce,
            status="succeeded",
            raw=raw,
        )
        result = await client.post(
            f"/desktop-devices/{device['id']}/commands/{queued['command_id']}/result",
            headers=auth,
            json={
                "nonce": nonce,
                "status": "succeeded",
                "error_code": None,
                "result_b64": base64.b64encode(raw).decode(),
                "signature": signature,
            },
        )
        assert result.status_code == 200
        assert result.json()["accepted"] is True

        disconnected = await client.post(
            f"/desktop-devices/{device['id']}/disconnect", headers=auth
        )
        assert disconnected.status_code == 200
        assert disconnected.json()["status"] == "revoked"
        after = await client.post(
            f"/desktop-devices/{device['id']}/heartbeat", headers=auth, json={}
        )
        assert after.status_code == 401
