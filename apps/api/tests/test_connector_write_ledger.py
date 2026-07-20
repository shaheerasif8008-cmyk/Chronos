from __future__ import annotations

from datetime import timedelta

import pytest


@pytest.fixture(autouse=True)
def _write_ledger_key(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "vault_encryption_key", "91" * 32)


async def _operation(
    repo,
    *,
    idempotency_key: str = "request-1",
    approval: str = "approval-1",
    provider_idempotency: bool = False,
    outbox_payload: dict | None = None,
):
    from core.connector_write_ledger import ConnectorWriteLedger

    return await ConnectorWriteLedger(repo).prepare(
        organization_id="org-a",
        member_id="member-a",
        task_id="task-a",
        channel="framework" if outbox_payload else "broker",
        tool="custom.write",
        provider="custom",
        risk_level="write",
        payload={"value": 1},
        approval_binding=approval,
        idempotency_key=idempotency_key,
        connector_job_id="job-a" if outbox_payload else None,
        provider_idempotency=provider_idempotency,
        outbox_payload=outbox_payload,
    )


@pytest.mark.asyncio
async def test_crash_after_provider_response_is_adopted_without_redis_or_provider_replay():
    from connectors.framework.repository import InMemoryConnectorRepository
    from core.audit_redaction import REDACTED
    from core.connector_write_ledger import ConnectorWriteLedger

    repo = InMemoryConnectorRepository()
    ledger = ConnectorWriteLedger(repo)
    operation = await _operation(repo)
    claimed = await ledger.claim(
        str(operation["id"]), organization_id="org-a", owner="worker-1"
    )
    assert claimed.kind == "dispatch"

    await ledger.record_provider_response(
        str(operation["id"]),
        organization_id="org-a",
        result={"output": {"remote_id": "obj-1"}},
        evidence={"authorization": "Bearer secret", "status_code": 201},
    )
    # Simulate a process crash before `complete()`. A fresh process adopts the
    # committed provider response instead of invoking the provider again.
    replay = await ConnectorWriteLedger(repo).claim(
        str(operation["id"]), organization_id="org-a", owner="worker-2"
    )

    assert replay.kind == "replay"
    assert replay.result == {"output": {"remote_id": "obj-1"}}
    stored = await repo.get_write_operation(str(operation["id"]), organization_id="org-a")
    assert stored["status"] == "complete"
    assert stored["provider_evidence"]["authorization"] == REDACTED


@pytest.mark.asyncio
async def test_expired_unsupported_claim_stops_for_manual_review_instead_of_retrying():
    from connectors.framework.repository import InMemoryConnectorRepository
    from core.connector_write_ledger import (
        ConnectorWriteLedger,
        ManualReviewRequired,
        utcnow,
    )

    repo = InMemoryConnectorRepository()
    ledger = ConnectorWriteLedger(repo)
    operation = await _operation(repo)
    await ledger.claim(str(operation["id"]), organization_id="org-a", owner="dead-worker")
    repo.write_operations[str(operation["id"])]["claim_expires_at"] = utcnow() - timedelta(seconds=1)

    with pytest.raises(ManualReviewRequired):
        await ledger.claim(
            str(operation["id"]), organization_id="org-a", owner="replacement-worker"
        )
    stored = await repo.get_write_operation(str(operation["id"]), organization_id="org-a")
    assert stored["status"] == "manual_review"
    assert stored["attempts"] == 1


@pytest.mark.asyncio
async def test_supported_provider_retry_reuses_one_opaque_provider_key():
    from connectors.framework.repository import InMemoryConnectorRepository
    from core.connector_write_ledger import ConnectorWriteLedger

    repo = InMemoryConnectorRepository()
    ledger = ConnectorWriteLedger(repo)
    operation = await _operation(repo, provider_idempotency=True)
    first_key = operation["provider_idempotency_key"]
    await ledger.claim(str(operation["id"]), organization_id="org-a", owner="worker-1")
    retry = await ledger.mark_ambiguous(
        str(operation["id"]), organization_id="org-a", error="read timeout"
    )
    assert retry["status"] == "retry"

    claimed = await ledger.claim(
        str(operation["id"]), organization_id="org-a", owner="worker-2"
    )
    assert claimed.operation["provider_idempotency_key"] == first_key
    assert claimed.operation["attempts"] == 2


@pytest.mark.asyncio
async def test_idempotency_binding_rejects_payload_approval_and_cross_tenant_reuse():
    from connectors.framework.repository import InMemoryConnectorRepository
    from core.connector_write_ledger import (
        ConnectorWriteLedger,
        WriteOperationConflict,
    )

    repo = InMemoryConnectorRepository()
    operation = await _operation(repo, idempotency_key="same-key", approval="approval-1")
    with pytest.raises(WriteOperationConflict):
        await _operation(repo, idempotency_key="same-key", approval="approval-2")
    with pytest.raises(WriteOperationConflict):
        await ConnectorWriteLedger(repo).claim(
            str(operation["id"]), organization_id="org-b", owner="cross-tenant"
        )


@pytest.mark.asyncio
async def test_redis_loss_rebuilds_exact_job_from_encrypted_postgres_outbox():
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from core.connector_write_ledger import recover_framework_outbox

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    payload = {
        "id": "job-a",
        "tenant_id": "org-a",
        "task_id": "task-a",
        "workspace_id": "default",
        "employee_id": "agent-a",
        "user_id": "member-a",
        "connector_id": "custom",
        "action_name": "write",
        "arguments": {"value": 1},
        "actor": {"id": "agent-a", "org_id": "org-a", "member_id": "member-a"},
    }
    operation = await _operation(repo, outbox_payload=payload)

    result = await recover_framework_outbox(repo, queue)
    recovered = await queue.dequeue(timeout_seconds=0.1)
    stored_job = await repo.get_execution_job("job-a", tenant_id="org-a")
    assert result["recovered"] == 1
    assert recovered["id"] == "job-a"
    assert recovered["arguments"] == {"value": 1}
    assert recovered["write_operation_id"] == str(operation["id"])
    assert stored_job["write_operation_id"] == str(operation["id"])


@pytest.mark.asyncio
async def test_worker_never_retries_ambiguous_provider_without_reconciliation():
    from connectors.framework.models import ConnectorResult
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.worker import ConnectorWorker

    class AmbiguousAdapter:
        calls = 0

        async def validate_credentials(self, _credentials):
            return True

        async def execute(self, _action, _args, _context):
            self.calls += 1
            return ConnectorResult(
                status="ambiguous", error="provider response stream ended"
            )

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    adapter = AmbiguousAdapter()
    payload = {
        "id": "job-a",
        "tenant_id": "org-a",
        "task_id": "task-a",
        "workspace_id": "default",
        "employee_id": "agent-a",
        "user_id": "member-a",
        "connector_id": "custom",
        "action_name": "write",
        "arguments": {"value": 1},
        "actor": {"id": "agent-a", "org_id": "org-a", "member_id": "member-a"},
        "max_attempts": 5,
    }
    operation = await _operation(repo, outbox_payload=payload)
    payload["write_operation_id"] = str(operation["id"])
    await repo.create_execution_job(
        id="job-a",
        tenant_id="org-a",
        task_id="task-a",
        workspace_id="default",
        employee_id="agent-a",
        user_id="member-a",
        connector_id="custom",
        action_name="write",
        arguments={"value": 1},
        max_attempts=5,
        write_operation_id=str(operation["id"]),
    )
    await queue.enqueue(payload)

    result = await ConnectorWorker(repo, {"custom": adapter}, queue).run_once()
    stored = await repo.get_write_operation(str(operation["id"]), organization_id="org-a")
    assert adapter.calls == 1
    assert result["status"] == "manual_review"
    assert stored["status"] == "manual_review"


def test_specialized_gmail_is_not_double_wrapped_and_generic_mutations_are():
    from core.connector_write_ledger import is_broker_connector_mutation

    assert not is_broker_connector_mutation("gmail.send", {}, composio=True)
    assert not is_broker_connector_mutation("repo.create_pr", {}, composio=False)
    assert is_broker_connector_mutation(
        "stripe.api", {"method": "POST"}, composio=False
    )
    assert not is_broker_connector_mutation(
        "stripe.api", {"method": "GET"}, composio=False
    )
    assert is_broker_connector_mutation("slack.send", {}, composio=True)
    assert is_broker_connector_mutation("twitter.post", {}, composio=False)
    assert not is_broker_connector_mutation("doc.create", {}, composio=False)


@pytest.mark.asyncio
async def test_broker_commits_provider_response_before_any_followup_and_classifies_raise():
    from core.models import ToolResult
    from core.tool_broker import _dispatch_claimed_connector_write

    events: list[str] = []

    class Ledger:
        async def record_provider_response(self, *_args, **_kwargs):
            events.append("recorded")

        async def mark_ambiguous(self, *_args, **_kwargs):
            events.append("ambiguous")
            return {"status": "manual_review"}

        async def mark_failed(self, *_args, **_kwargs):
            events.append("failed")

    async def success():
        events.append("provider_returned")
        return ToolResult(data={"remote_id": "1"}, summary="created")

    result, recorded = await _dispatch_claimed_connector_write(
        success,
        ledger=Ledger(),
        operation={"id": "operation-1"},
        organization_id="org-a",
        tool="custom.create",
    )
    assert result.summary == "created"
    assert recorded is True
    assert events == ["provider_returned", "recorded"]

    events.clear()

    async def raises():
        events.append("provider_raised")
        raise RuntimeError("socket ended")

    result, recorded = await _dispatch_claimed_connector_write(
        raises,
        ledger=Ledger(),
        operation={"id": "operation-2"},
        organization_id="org-a",
        tool="custom.create",
    )
    assert recorded is False
    assert result.data["status"] == "manual_review"
    assert events == ["provider_raised", "ambiguous"]


@pytest.mark.asyncio
async def test_worker_crash_after_response_record_is_adopted_without_second_adapter_call():
    from connectors.framework.models import ConnectorResult
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.worker import ConnectorWorker

    class Adapter:
        calls = 0

        async def validate_credentials(self, _credentials):
            return True

        async def execute(self, _action, _args, _context):
            self.calls += 1
            return ConnectorResult(status="success", output={"remote_id": "created-1"})

    class CrashingTracer:
        async def start_trace(self, **_kwargs):
            return {"id": "trace-1"}

        async def record_step(self, *_args, **_kwargs):
            raise RuntimeError("process crashed after provider response")

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    adapter = Adapter()
    payload = {
        "id": "job-a",
        "tenant_id": "org-a",
        "task_id": "task-a",
        "workspace_id": "default",
        "employee_id": "agent-a",
        "user_id": "member-a",
        "connector_id": "custom",
        "action_name": "write",
        "arguments": {"value": 1},
        "actor": {"id": "agent-a", "org_id": "org-a", "member_id": "member-a"},
    }
    operation = await _operation(repo, outbox_payload=payload)
    payload["write_operation_id"] = str(operation["id"])
    await repo.create_execution_job(
        id="job-a",
        tenant_id="org-a",
        task_id="task-a",
        workspace_id="default",
        employee_id="agent-a",
        user_id="member-a",
        connector_id="custom",
        action_name="write",
        arguments={"value": 1},
        write_operation_id=str(operation["id"]),
    )
    await queue.enqueue(dict(payload))

    with pytest.raises(RuntimeError, match="process crashed"):
        await ConnectorWorker(
            repo, {"custom": adapter}, queue, tracer=CrashingTracer()
        ).run_once()
    stored = await repo.get_write_operation(str(operation["id"]), organization_id="org-a")
    assert stored["status"] == "provider_confirmed"
    assert adapter.calls == 1

    await queue.enqueue(dict(payload))
    replay = await ConnectorWorker(repo, {"custom": adapter}, queue).run_once()
    assert replay["status"] == "success"
    assert replay["result"] == {"remote_id": "created-1"}
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_worker_adapter_exception_is_immediately_manual_review_not_leased():
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.worker import ConnectorWorker

    class RaisingAdapter:
        async def validate_credentials(self, _credentials):
            return True

        async def execute(self, _action, _args, _context):
            raise RuntimeError("connection reset after request")

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    payload = {
        "id": "job-a",
        "tenant_id": "org-a",
        "task_id": "task-a",
        "workspace_id": "default",
        "employee_id": "agent-a",
        "user_id": "member-a",
        "connector_id": "custom",
        "action_name": "write",
        "arguments": {"value": 1},
        "actor": {"id": "agent-a", "org_id": "org-a", "member_id": "member-a"},
    }
    operation = await _operation(repo, outbox_payload=payload)
    payload["write_operation_id"] = str(operation["id"])
    await repo.create_execution_job(
        id="job-a",
        tenant_id="org-a",
        task_id="task-a",
        workspace_id="default",
        employee_id="agent-a",
        user_id="member-a",
        connector_id="custom",
        action_name="write",
        arguments={"value": 1},
        write_operation_id=str(operation["id"]),
    )
    await queue.enqueue(payload)

    result = await ConnectorWorker(
        repo, {"custom": RaisingAdapter()}, queue
    ).run_once()
    stored = await repo.get_write_operation(str(operation["id"]), organization_id="org-a")
    assert result["status"] == "manual_review"
    assert stored["status"] == "manual_review"
    assert stored["claim_owner"] is None


@pytest.mark.asyncio
async def test_worker_safe_retry_passes_the_same_provider_idempotency_key():
    from connectors.framework.models import ConnectorResult
    from connectors.framework.queue import InMemoryExecutionQueue
    from connectors.framework.repository import InMemoryConnectorRepository
    from connectors.framework.worker import ConnectorWorker

    class RetryAdapter:
        keys: list[str] = []

        async def validate_credentials(self, _credentials):
            return True

        async def execute(self, _action, _args, context):
            self.keys.append(context["provider_idempotency_key"])
            if len(self.keys) == 1:
                return ConnectorResult(status="ambiguous", error="read timeout")
            return ConnectorResult(status="success", output={"remote_id": "one"})

    repo = InMemoryConnectorRepository()
    queue = InMemoryExecutionQueue()
    adapter = RetryAdapter()
    payload = {
        "id": "job-a",
        "tenant_id": "org-a",
        "task_id": "task-a",
        "workspace_id": "default",
        "employee_id": "agent-a",
        "user_id": "member-a",
        "connector_id": "stripe",
        "action_name": "write",
        "arguments": {"value": 1},
        "actor": {"id": "agent-a", "org_id": "org-a", "member_id": "member-a"},
        "max_attempts": 2,
    }
    operation = await _operation(
        repo, outbox_payload=payload, provider_idempotency=True
    )
    payload["write_operation_id"] = str(operation["id"])
    await repo.create_execution_job(
        id="job-a",
        tenant_id="org-a",
        task_id="task-a",
        workspace_id="default",
        employee_id="agent-a",
        user_id="member-a",
        connector_id="stripe",
        action_name="write",
        arguments={"value": 1},
        max_attempts=2,
        write_operation_id=str(operation["id"]),
    )
    await queue.enqueue(payload)

    result = await ConnectorWorker(repo, {"stripe": adapter}, queue).run_once()
    assert result["status"] == "success"
    assert adapter.keys == [
        operation["provider_idempotency_key"],
        operation["provider_idempotency_key"],
    ]
