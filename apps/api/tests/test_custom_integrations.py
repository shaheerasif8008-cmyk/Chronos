from __future__ import annotations

import hashlib
import hmac
import time

import pytest


def test_custom_http_actions_derive_non_bypassable_risk_and_approval():
    from core.custom_integrations import normalize_action_definition

    read_action = normalize_action_definition(
        {"name": "list_items", "method": "GET", "path": "/v1/items", "request_schema": {"type": "object"}}
    )
    write_action = normalize_action_definition(
        {"name": "create_item", "method": "POST", "path": "/v1/items", "request_schema": {"type": "object"}}
    )
    delete_action = normalize_action_definition(
        {"name": "delete_item", "method": "DELETE", "path": "/v1/items/{id}", "request_schema": {"type": "object"}}
    )

    assert (read_action["risk_level"], read_action["approval_required"]) == ("read", False)
    assert (write_action["risk_level"], write_action["approval_required"]) == ("write", True)
    assert (delete_action["risk_level"], delete_action["approval_required"]) == ("destructive", True)


@pytest.mark.parametrize(
    "path",
    ["https://other.example/path", "//other.example/path", "/v1/../admin", "/v1/items?secret=x", "/v1\\items"],
)
def test_custom_http_action_paths_cannot_escape_configured_origin(path: str):
    from core.custom_integrations import CustomIntegrationError, normalize_action_definition

    with pytest.raises(CustomIntegrationError):
        normalize_action_definition(
            {"name": "unsafe", "method": "GET", "path": path, "request_schema": {"type": "object"}}
        )


def test_custom_http_action_path_parameters_are_percent_encoded():
    from core.custom_integrations import _format_action_path

    assert _format_action_path("/v1/items/{id}", {"path_params": {"id": "../../admin?x=1"}}) == "/v1/items/..%2F..%2Fadmin%3Fx%3D1"


def test_timestamped_webhook_hmac_rejects_tamper_and_stale_requests():
    from core.custom_integrations import CustomIntegrationError, _verify_webhook_signature

    secret = "whsec_test"
    payload = b'{"event":"created"}'
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + payload, hashlib.sha256).hexdigest()

    _verify_webhook_signature(
        secret=secret,
        timestamp=timestamp,
        signature=f"v1={signature}",
        payload=payload,
    )
    with pytest.raises(CustomIntegrationError, match="signature"):
        _verify_webhook_signature(
            secret=secret,
            timestamp=timestamp,
            signature=f"v1={signature}",
            payload=b'{"event":"changed"}',
        )
    with pytest.raises(CustomIntegrationError, match="five-minute"):
        _verify_webhook_signature(
            secret=secret,
            timestamp=timestamp,
            signature=f"v1={signature}",
            payload=payload,
            now=int(timestamp) + 301,
        )


def test_dynamic_adapter_registry_only_claims_generated_custom_http_ids():
    from connectors.framework.adapters import adapter_registry
    from core.custom_integrations import TenantCustomHTTPAdapter

    adapters = adapter_registry()

    assert isinstance(adapters.get("custom_http_0123456789abcdef"), TenantCustomHTTPAdapter)
    assert adapters.get("custom_http") is not None  # Built-in catalog definition.
    assert adapters.get("custom_fake") is None


@pytest.mark.asyncio
async def test_webhook_workflow_dispatch_is_idempotent_per_event_and_trigger():
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.seed import seed_builtin_connectors
    from connectors.framework.workflows import WorkflowRuntime

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    await seed_builtin_connectors(repo)
    runtime = WorkflowRuntime(repo, queue)
    await runtime.create_workflow(
        tenant_id="org-a",
        workspace_id="default",
        employee_id="employee-1",
        user_id="member-1",
        name="Signed webhook workflow",
        steps=[
            {
                "id": "capture",
                "tool_name": "internal_echo__echo",
                "arguments": {"message": "capture"},
            }
        ],
        triggers=[
            {
                "trigger_type": "webhook",
                "source": "webhook:public-id",
                "event_type": "lead.created",
            }
        ],
    )

    first = await runtime.dispatch_event(
        tenant_id="org-a",
        source="webhook:public-id",
        event_type="lead.created",
        payload={"lead": {"id": "1"}},
        persisted_payload={"webhook_event_id": "evt-1", "payload_digest": "digest"},
        idempotency_key="evt-1",
    )
    second = await runtime.dispatch_event(
        tenant_id="org-a",
        source="webhook:public-id",
        event_type="lead.created",
        payload={"lead": {"id": "1"}},
        persisted_payload={"webhook_event_id": "evt-1", "payload_digest": "digest"},
        idempotency_key="evt-1",
    )

    assert first[0]["id"] == second[0]["id"]
    assert len(repo.workflow_runs) == 1
    stored = next(iter(repo.workflow_runs.values()))
    assert stored["trigger_payload"] == {"webhook_event_id": "evt-1", "payload_digest": "digest"}
    assert queue._queue.qsize() == 1


def test_custom_integration_audit_redaction_covers_signing_and_auth_fields():
    from core.audit_redaction import REDACTED, redact

    assert redact(
        {
            "auth_header": "Authorization",
            "auth_token": "Bearer secret",
            "signing_secret": "whsec_secret",
            "payload_digest": "safe-hash",
        }
    ) == {
        "auth_header": "Authorization",
        "auth_token": REDACTED,
        "signing_secret": REDACTED,
        "payload_digest": "safe-hash",
    }
