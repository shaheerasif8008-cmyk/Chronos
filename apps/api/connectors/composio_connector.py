"""
Composio connector — broker-facing adapter for all Composio-managed SaaS apps.

The ToolBroker routes every Composio-managed provider here (RULE 1: no direct
connector calls). This module translates a Chronos `provider.action` tool call
into a Composio action execution against the caller's managed-auth entity, then
normalises the response into a ToolResult.

When Composio isn't configured, or the call runs under a demo/fixture tier, it
returns clearly-labelled placeholder data instead of hitting the network.
"""
from __future__ import annotations

import logging
from typing import Any

from connectors import composio_client
from connectors.gmail_delivery import (
    DeliveryContext,
    DraftEvidence,
    EmailEnvelope,
    SentEvidence,
    deliver_approved_email,
    validate_email_args,
)
from core.exceptions import ApprovalRequired, SafetyLimitViolation
from core.models import AgentContext, ToolResult

log = logging.getLogger(__name__)


def _response_data(response: dict[str, Any], action: str) -> dict[str, Any]:
    successful = response.get("successful")
    if successful is None:
        successful = response.get("success", True)
    if not successful:
        raise RuntimeError(f"{action} failed: {response.get('error') or 'provider error'}")
    data = response.get("data")
    return data if isinstance(data, dict) else {"result": data}


def _nested_value(value: Any, *keys: str) -> Any:
    """Find the first named field in a small provider response tree."""
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
        for nested in value.values():
            found = _nested_value(nested, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value[:10]:
            found = _nested_value(nested, *keys)
            if found not in (None, ""):
                return found
    return None


def _not_found_response(response: dict[str, Any]) -> bool:
    if response.get("successful", response.get("success", True)):
        return False
    error = str(response.get("error") or "").lower()
    return "not found" in error or "404" in error


def _normalise_response(tool: str, response: dict[str, Any]) -> ToolResult:
    """Turn a raw Composio SDK response into a ToolResult."""
    successful = response.get("successful")
    if successful is None:
        successful = response.get("success", True)
    data = response.get("data")
    if not isinstance(data, dict):
        data = {"result": data} if data is not None else {}

    if not successful:
        error = response.get("error") or "Composio action failed"
        return ToolResult(data={"error": str(error)}, summary=f"{tool} failed: {error}")

    return ToolResult(data=data, summary=f"{tool} → ok")


class ComposioConnector:
    """Routes Composio-managed `provider.action` calls through Composio managed auth."""

    async def execute(self, tool: str, args: dict[str, Any], agent: AgentContext) -> ToolResult:
        provider = tool.split(".")[0]
        approved_by_gate = bool(args.pop("__approved_by_gate", False))
        approval_id = str(args.pop("__approval_id", "") or "")
        idempotency_key = str(args.pop("__idempotency_key", "") or "")
        tier = args.pop("__connector_tier", "live")
        org_id = str(args.pop("__org_id", "") or agent.org_id)
        task_id = str(args.pop("__task_id", "") or agent.task_id or "")
        member_id = str(args.pop("__member_id", "") or agent.member_id or "")
        write_operation_id = str(args.pop("__write_operation_id", "") or "")
        args.pop("__provider_idempotency_key", None)

        if tool == "gmail.send":
            if not approved_by_gate:
                raise ApprovalRequired("gmail.send", "sending requires an approved approval record")
            if not all((approval_id, idempotency_key, org_id, task_id, member_id)):
                raise SafetyLimitViolation(
                    "gmail.send: approved execution requires approval, idempotency, tenant, task, and member scope"
                )

        if tier in {"demo", "fixture"} or not composio_client.is_configured():
            reason = (
                "demo tier" if tier in {"demo", "fixture"} else "COMPOSIO_API_KEY is not set"
            )
            return ToolResult(
                data={"demo": True, "tool": tool, "reason": reason},
                summary=f"[demo] {tool} — connect {provider} via Composio to use live data",
            )

        if tool == "gmail.send":
            return await self._send_gmail_approved(
                args=args,
                org_id=org_id,
                member_id=member_id,
                task_id=task_id,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )

        try:
            action, params = composio_client.resolve_action(tool, args)
        except ValueError as exc:
            return ToolResult(data={"error": str(exc)}, summary=f"{tool} failed: {exc}")

        entity = composio_client.entity_id(org_id, agent.member_id)
        try:
            response = await composio_client.execute_action(action, params, entity=entity)
        except Exception as exc:  # noqa: BLE001 - SDK raises a wide range of errors
            log.warning("Composio %s (action=%s) failed: %s", tool, action, exc)
            if write_operation_id:
                return ToolResult(
                    data={
                        "status": "ambiguous",
                        "manual_review_required": True,
                        "error": "Composio transport failed after dispatch",
                    },
                    summary=(
                        f"{tool} outcome is ambiguous; automatic retry is disabled "
                        "until a human reconciles provider state"
                    ),
                )
            return ToolResult(data={"error": str(exc)}, summary=f"{tool} failed: {exc}")

        if not isinstance(response, dict):
            response = {"successful": True, "data": response}
        return _normalise_response(tool, response)

    async def _send_gmail_approved(
        self,
        *,
        args: dict[str, Any],
        org_id: str,
        member_id: str,
        task_id: str,
        approval_id: str,
        idempotency_key: str,
    ) -> ToolResult:
        """Draft-first managed Gmail send using the member-scoped entity."""
        envelope = validate_email_args(args)
        entity = composio_client.entity_id(org_id, member_id)
        context = DeliveryContext(
            approval_id=approval_id,
            organization_id=org_id,
            member_id=member_id,
            task_id=task_id,
            credential_scope=f"composio:{entity}",
            idempotency_key=idempotency_key,
        )

        async def execute(action: str, params: dict[str, Any]) -> dict[str, Any]:
            response = await composio_client.execute_action(action, params, entity=entity)
            if not isinstance(response, dict):
                response = {"successful": True, "data": response}
            return response

        async def create_draft(
            approved_email: EmailEnvelope,
            _idempotency_sha256: str,
        ) -> DraftEvidence:
            params: dict[str, Any] = {
                "recipient_email": approved_email.to[0],
                "subject": approved_email.subject,
                "body": approved_email.body,
                "is_html": approved_email.is_html,
            }
            if len(approved_email.to) > 1:
                params["extra_recipients"] = list(approved_email.to[1:])
            if approved_email.cc:
                params["cc"] = list(approved_email.cc)
            if approved_email.bcc:
                params["bcc"] = list(approved_email.bcc)
            data = _response_data(
                await execute("GMAIL_CREATE_EMAIL_DRAFT", params),
                "GMAIL_CREATE_EMAIL_DRAFT",
            )
            draft_id = str(_nested_value(data, "draft_id", "draftId") or "")
            # Some Composio versions expose the Gmail draft's top-level id.
            if not draft_id:
                draft_id = str(data.get("id") or "")
            message_id = str(
                _nested_value(data.get("message") or {}, "message_id", "messageId", "id") or ""
            ) or None
            if not draft_id:
                raise RuntimeError("GMAIL_CREATE_EMAIL_DRAFT returned no draft id")
            return DraftEvidence(draft_id=draft_id, message_id=message_id)

        async def inspect_delivery(evidence: DraftEvidence) -> SentEvidence | None | bool:
            draft_response = await execute("GMAIL_GET_DRAFT", {"draft_id": evidence.draft_id})
            if draft_response.get("successful", draft_response.get("success", True)):
                return False
            if not _not_found_response(draft_response):
                _response_data(draft_response, "GMAIL_GET_DRAFT")
            if not evidence.message_id:
                return None
            message_response = await execute(
                "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
                {"message_id": evidence.message_id},
            )
            if _not_found_response(message_response):
                return None
            data = _response_data(message_response, "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID")
            labels = _nested_value(data, "labelIds", "label_ids") or []
            if isinstance(labels, str):
                labels = [labels]
            if "SENT" not in {str(label).upper() for label in labels}:
                return None
            return SentEvidence(
                message_id=str(_nested_value(data, "message_id", "messageId", "id") or evidence.message_id),
                thread_id=str(_nested_value(data, "thread_id", "threadId") or "") or None,
            )

        async def send_draft(evidence: DraftEvidence) -> SentEvidence:
            data = _response_data(
                await execute("GMAIL_SEND_DRAFT", {"draft_id": evidence.draft_id}),
                "GMAIL_SEND_DRAFT",
            )
            message_id = str(
                _nested_value(data, "message_id", "messageId", "id") or evidence.message_id or ""
            )
            if not message_id:
                raise RuntimeError("GMAIL_SEND_DRAFT returned no message id")
            return SentEvidence(
                message_id=message_id,
                thread_id=str(_nested_value(data, "thread_id", "threadId") or "") or None,
            )

        return await deliver_approved_email(
            context=context,
            envelope=envelope,
            create_draft=create_draft,
            inspect_delivery=inspect_delivery,
            send_draft=send_draft,
        )


composio_connector = ComposioConnector()
