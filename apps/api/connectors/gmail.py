"""
Gmail connector via Composio.

Capability map:
  gmail.read_inbox       — list recent messages
  gmail.draft            — create a draft (NEVER sends automatically)
  gmail.send             — blocked at ToolBroker level in Phase 1 (ApprovalRequired)

Composio manages OAuth token storage.  We store only the entity_id in our vault.
"""
from __future__ import annotations

import logging
from typing import Any

from core.exceptions import ApprovalRequired
from core.models import ToolResult

log = logging.getLogger(__name__)


def _get_composio(api_key: str):
    try:
        from composio import Composio  # type: ignore[import]
        return Composio(api_key=api_key)
    except ImportError as e:
        raise RuntimeError("composio-core package not installed — run: pip install composio-core") from e


def _get_toolset(api_key: str):
    try:
        from composio import ComposioToolSet  # type: ignore[import]
        return ComposioToolSet(api_key=api_key)
    except ImportError as e:
        raise RuntimeError("composio-core package not installed — run: pip install composio-core") from e


class GmailConnector:
    """Thin wrapper that routes gmail.* tool calls to Composio."""

    async def execute(self, tool: str, args: dict[str, Any], vault_ref: str) -> ToolResult:
        from connectors.vault import get as vault_get
        from core.config import settings

        # gmail.send is blocked by ToolBroker before reaching here in Phase 1.
        # This guard is a belt-and-suspenders defence.
        if tool == "gmail.send":
            raise ApprovalRequired("gmail.send", "use gmail.draft; sending requires an approval record")

        credentials = await vault_get(vault_ref)
        entity_id = credentials.get("composio_entity_id")
        if not entity_id:
            raise ValueError(f"vault_ref {vault_ref} missing composio_entity_id")

        api_key = settings.composio_api_key
        if not api_key:
            raise RuntimeError("COMPOSIO_API_KEY is not configured")

        if tool == "gmail.read_inbox":
            return await self._read_inbox(api_key, entity_id, args)
        if tool == "gmail.draft":
            return await self._create_draft(api_key, entity_id, args)

        raise ValueError(f"Unknown gmail tool: {tool}")

    async def _read_inbox(self, api_key: str, entity_id: str, args: dict) -> ToolResult:
        try:
            from composio import Action  # type: ignore[import]
        except ImportError as e:
            raise RuntimeError("composio-core not installed") from e

        toolset = _get_toolset(api_key)
        max_results = args.get("max_results", 10)
        result = toolset.execute_action(
            action=Action.GMAIL_LIST_THREADS,
            params={"maxResults": max_results},
            entity_id=entity_id,
        )
        data = result if isinstance(result, dict) else {"raw": str(result)}
        return ToolResult(data=data, summary=f"Read inbox ({max_results} threads)")

    async def _create_draft(self, api_key: str, entity_id: str, args: dict) -> ToolResult:
        try:
            from composio import Action  # type: ignore[import]
        except ImportError as e:
            raise RuntimeError("composio-core not installed") from e

        toolset = _get_toolset(api_key)
        result = toolset.execute_action(
            action=Action.GMAIL_CREATE_EMAIL_DRAFT,
            params={
                "to": args.get("to", ""),
                "subject": args.get("subject", ""),
                "body": args.get("body", ""),
                "cc": args.get("cc", ""),
            },
            entity_id=entity_id,
        )
        data = result if isinstance(result, dict) else {"raw": str(result)}
        draft_id = data.get("id", "unknown")
        return ToolResult(data=data, summary=f"Draft created: {draft_id}")


gmail_connector = GmailConnector()


# ---------------------------------------------------------------------------
# OAuth helpers — used by the connectors router
# ---------------------------------------------------------------------------

async def oauth_start_url(member_id: str, org_id: str) -> str:
    """Return the Composio OAuth URL to redirect the user to."""
    from core.config import settings

    api_key = settings.composio_api_key
    if not api_key:
        raise RuntimeError("COMPOSIO_API_KEY is not configured")

    client = _get_composio(api_key)
    entity_id = f"{org_id}:{member_id}"
    callback_url = f"{settings.composio_callback_base_url}/connectors/gmail/oauth-callback"

    try:
        entity = client.get_entity(entity_id)
        connection_request = entity.initiate_connection(
            app="gmail",
            redirect_url=callback_url,
        )
        return connection_request.redirectUrl
    except Exception as exc:
        raise RuntimeError(f"Composio OAuth initiation failed: {exc}") from exc


async def oauth_finish(code: str, state: str, org_id: str) -> dict[str, str]:
    """
    Called after Composio redirects back.

    Composio handles the token exchange internally; we just confirm the
    connection exists and return the entity_id to store in our vault.
    """
    from core.config import settings

    api_key = settings.composio_api_key
    if not api_key:
        raise RuntimeError("COMPOSIO_API_KEY is not configured")

    # state encodes member_id passed through the OAuth flow
    member_id = state
    entity_id = f"{org_id}:{member_id}"

    client = _get_composio(api_key)
    try:
        entity = client.get_entity(entity_id)
        connection = entity.get_connection(app="gmail")
        return {
            "composio_entity_id": entity_id,
            "composio_connection_id": connection.id,
        }
    except Exception as exc:
        raise RuntimeError(f"Composio connection retrieval failed: {exc}") from exc
