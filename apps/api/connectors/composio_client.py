"""
Composio client — the single place the Composio SDK is imported and called.

Chronos uses Composio's *managed auth* for external SaaS connectors: Composio
holds the OAuth tokens, and Chronos references a connection by an **entity id**
derived from (org, member). No third-party credential ever touches the Chronos
vault for Composio-managed providers — only the entity id (safe to log).

Everything Composio-specific lives here so the rest of the codebase depends on a
small, stable surface and never imports `composio` directly. The SDK is imported
lazily so the API still boots when the package isn't installed (the wrapper then
reports "not configured" and callers fall back to the demo tier).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Callable

from core.config import settings

log = logging.getLogger(__name__)

# Chronos provider id  →  Composio app slug.
# Native/local tools (browser, fs, code, computer, …) and providers that keep a
# dedicated connector (canva) are intentionally absent — they never route here.
_APP_SLUGS: dict[str, str] = {
    "gmail": "gmail",
    "google_calendar": "googlecalendar",
    "google_drive": "googledrive",
    "notion": "notion",
    "slack": "slack",
    "github": "github",
    "linear": "linear",
    "hubspot": "hubspot",
    "airtable": "airtable",
    "jira": "jira",
    "outlook": "outlook",
    "teams": "microsoft_teams",
    "sharepoint_onedrive": "one_drive",
    "salesforce": "salesforce",
    "stripe": "stripe",
}


# ---------------------------------------------------------------------------
# Per-tool action mapping
# ---------------------------------------------------------------------------
# Chronos tool name  →  (Composio action slug, param adapter).
# Only Gmail is mapped explicitly today (the proven end-to-end loop). Any other
# Composio-managed tool falls back to the generic resolver below, which accepts
# an explicit ``composio_action`` (or ``action``) arg and passes the remaining
# args straight through as Composio params.

def _gmail_fetch_params(args: dict[str, Any]) -> dict[str, Any]:
    return {"max_results": int(args.get("max_results", 10))}


def _gmail_search_params(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": args.get("query", ""),
        "max_results": int(args.get("max_results", 10)),
    }


def _gmail_draft_params(args: dict[str, Any]) -> dict[str, Any]:
    params = {
        "recipient_email": args.get("to", ""),
        "subject": args.get("subject", ""),
        "body": args.get("body", ""),
        "is_html": bool(args.get("is_html", False)),
    }
    if args.get("cc"):
        params["cc"] = [args["cc"]] if isinstance(args["cc"], str) else args["cc"]
    return params


def _gmail_send_params(args: dict[str, Any]) -> dict[str, Any]:
    params = _gmail_draft_params(args)
    return params


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _copy_params(args: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: args[key] for key in keys if args.get(key) not in (None, "")}


def _slack_send_params(args: dict[str, Any]) -> dict[str, Any]:
    return _copy_params(args, ("channel", "text", "thread_ts", "blocks", "attachments"))


def _slack_read_params(args: dict[str, Any]) -> dict[str, Any]:
    params = _copy_params(args, ("channel", "cursor", "oldest", "latest", "inclusive"))
    params["limit"] = _as_int(args.get("limit"), 20)
    return params


def _slack_search_params(args: dict[str, Any]) -> dict[str, Any]:
    params = {
        "query": str(args.get("query") or ""),
        "count": _as_int(args.get("count", args.get("max_results")), 20),
    }
    if args.get("sort"):
        params["sort"] = args["sort"]
    if args.get("sort_dir"):
        params["sort_dir"] = args["sort_dir"]
    return params


def _github_create_issue_params(args: dict[str, Any]) -> dict[str, Any]:
    params = _copy_params(args, ("owner", "repo", "title", "body", "assignees", "milestone"))
    labels = _as_list(args.get("labels"))
    if labels:
        params["labels"] = labels
    return params


def _github_read_params(args: dict[str, Any]) -> dict[str, Any]:
    return _copy_params(args, ("owner", "repo", "path", "ref"))


def _github_search_params(args: dict[str, Any]) -> dict[str, Any]:
    params = {
        "query": str(args.get("query") or args.get("q") or ""),
        "per_page": _as_int(args.get("per_page", args.get("max_results")), 10),
    }
    if args.get("page"):
        params["page"] = _as_int(args.get("page"), 1)
    return params


def _drive_search_params(args: dict[str, Any]) -> dict[str, Any]:
    params = {
        "query": str(args.get("query") or args.get("q") or ""),
        "page_size": _as_int(args.get("page_size", args.get("max_results")), 10),
    }
    if args.get("page_token"):
        params["page_token"] = args["page_token"]
    return params


def _drive_read_params(args: dict[str, Any]) -> dict[str, Any]:
    return _copy_params(args, ("file_id", "fields", "supports_all_drives"))


def _drive_upload_params(args: dict[str, Any]) -> dict[str, Any]:
    return _copy_params(
        args,
        (
            "file_name",
            "name",
            "mime_type",
            "content",
            "parent_id",
            "folder_id",
            "drive_id",
            "supports_all_drives",
        ),
    )


_ACTION_MAP: dict[str, tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "gmail.read_inbox": ("GMAIL_FETCH_EMAILS", _gmail_fetch_params),
    "gmail.search": ("GMAIL_FETCH_EMAILS", _gmail_search_params),
    "gmail.draft": ("GMAIL_CREATE_EMAIL_DRAFT", _gmail_draft_params),
    "gmail.send": ("GMAIL_SEND_EMAIL", _gmail_send_params),
    "slack.send": ("SLACK_CHAT_POST_MESSAGE", _slack_send_params),
    "slack.read": ("SLACK_FETCH_CONVERSATION_HISTORY", _slack_read_params),
    "slack.search": ("SLACK_SEARCH_MESSAGES", _slack_search_params),
    "github.create_issue": ("GITHUB_CREATE_AN_ISSUE", _github_create_issue_params),
    "github.read": ("GITHUB_GET_REPOSITORY_CONTENT", _github_read_params),
    "github.search": ("GITHUB_SEARCH_REPOSITORIES", _github_search_params),
    "google_drive.search": ("GOOGLEDRIVE_FIND_FILE", _drive_search_params),
    "google_drive.read": ("GOOGLEDRIVE_GET_FILE_METADATA", _drive_read_params),
    "google_drive.upload": ("GOOGLEDRIVE_UPLOAD_FILE", _drive_upload_params),
}

_INTERNAL_ARG_PREFIX = "__"


def is_composio_provider(provider: str) -> bool:
    """True if *provider* is a SaaS app Chronos brokers through Composio."""
    return provider in _APP_SLUGS


def app_slug(provider: str) -> str | None:
    return _APP_SLUGS.get(provider)


def _sdk_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("composio") is not None


def is_configured() -> bool:
    """True when a Composio API key is set AND the SDK is importable.

    When this returns False, the broker leaves SaaS connectors on their existing
    path (direct OAuth / generic HTTP / demo tier)."""
    return bool(getattr(settings, "composio_api_key", "")) and _sdk_available()


def entity_id(org_id: str, member_id: str | None) -> str:
    """Stable Composio entity id for a (org, member).

    Scoped per member by default so each user authorises their own accounts.
    Set ``COMPOSIO_ENTITY_SCOPE=org`` to share one connection per organization.
    """
    if getattr(settings, "composio_entity_scope", "member") == "org" or not member_id:
        return f"org:{org_id}"
    return f"{org_id}:{member_id}"


def managed_vault_ref(provider: str, entity: str) -> str:
    """Non-secret connector reference for a Composio-managed provider/entity."""
    return f"composio:{provider}:{entity}"


def managed_connector_id(provider: str, org_id: str, member_id: str | None) -> str:
    """Connector table id that matches the configured Composio entity scope."""
    if getattr(settings, "composio_entity_scope", "member") == "org" or not member_id:
        return f"{provider}:{org_id}"
    return f"{provider}:{org_id}:{member_id}"


def parse_managed_vault_ref(vault_ref: str) -> tuple[str, str] | None:
    """Parse a provider-scoped Composio vault ref, or None for legacy/invalid refs."""
    parts = vault_ref.split(":", 2)
    if (
        len(parts) != 3
        or parts[0] != "composio"
        or parts[1] not in _APP_SLUGS
        or not parts[2]
    ):
        return None
    return parts[1], parts[2]


@lru_cache(maxsize=1)
def _client():
    """Return a cached Composio v3 client. Raises if the SDK/key are unavailable."""
    if not settings.composio_api_key:
        raise RuntimeError("COMPOSIO_API_KEY is not set")
    try:
        from composio import Composio  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised when SDK absent
        raise RuntimeError("composio SDK is not installed") from exc
    return Composio(api_key=settings.composio_api_key)


# Backwards-compatible cache handle for older tests and local debug snippets.
_toolset = _client


def resolve_action(tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Map a Chronos tool name + args to a (Composio action slug, params) pair.

    Known tools use the curated ``_ACTION_MAP``. Anything else must carry an
    explicit ``composio_action`` (or ``action``) arg; its remaining args become
    the Composio params verbatim.
    """
    if tool in _ACTION_MAP:
        slug, adapter = _ACTION_MAP[tool]
        return slug, adapter(args)

    if not tool.endswith(".api"):
        raise ValueError(f"No Composio action mapping for '{tool}'.")

    explicit = args.get("composio_action") or args.get("action")
    if not explicit:
        raise ValueError(
            f"No Composio action mapping for '{tool}'. "
            "Pass a 'composio_action' arg with the Composio action slug."
        )
    params: dict[str, Any] = {}
    if isinstance(args.get("params"), dict):
        params.update(args["params"])
    params.update(
        {
            key: value
            for key, value in args.items()
            if (
                not key.startswith(_INTERNAL_ARG_PREFIX)
                and key not in {"composio_action", "action", "params"}
            )
        }
    )
    return str(explicit), params


async def execute_action(
    action: str,
    params: dict[str, Any],
    *,
    entity: str,
) -> dict[str, Any]:
    """Execute one Composio action for an entity. Returns the raw SDK response.

    The SDK call is synchronous, so it runs in a worker thread to avoid blocking
    the event loop.
    """
    import anyio

    def _call() -> dict[str, Any]:
        client = _client()
        return client.tools.execute(action, arguments=params, user_id=entity)

    return await anyio.to_thread.run_sync(_call)


async def initiate_connection(
    provider: str,
    *,
    entity: str,
    redirect_url: str,
) -> dict[str, Any]:
    """Start a Composio managed-auth connection. Returns {redirect_url, connection_id}."""
    import anyio

    slug = app_slug(provider)
    if not slug:
        raise ValueError(f"{provider} is not a Composio-managed provider")

    def _call() -> dict[str, Any]:
        client = _client()
        # The SDK raises provider-specific exception types (API errors, missing
        # auth configs, network failures). Normalize them all to RuntimeError so
        # the router can return an honest 502 instead of an unhandled 500.
        try:
            auth_config_id = client.toolkits._get_auth_config_id(toolkit=slug)
        except Exception as exc:  # noqa: BLE001 — SDK exception types vary by version
            raise RuntimeError(
                f"Composio has no usable auth config for '{slug}'. Create one for this "
                f"app in your Composio project (or verify COMPOSIO_API_KEY). ({exc})"
            ) from exc
        try:
            request = client.connected_accounts.initiate(
                user_id=entity,
                auth_config_id=auth_config_id,
                callback_url=redirect_url,
            )
        except Exception as exc:  # noqa: BLE001 — SDK exception types vary by version
            raise RuntimeError(
                f"Composio could not start the {slug} connection: {exc}"
            ) from exc
        return {
            "redirect_url": getattr(request, "redirectUrl", None)
            or getattr(request, "redirect_url", None)
            or getattr(request, "redirectUrl", ""),
            "connection_id": getattr(request, "connectedAccountId", None)
            or getattr(request, "connected_account_id", None)
            or getattr(request, "id", ""),
        }

    return await anyio.to_thread.run_sync(_call)


async def connection_status(provider: str, *, entity: str) -> str:
    """Return 'active' if the entity has a live connection for *provider*, else 'inactive'."""
    import anyio

    slug = app_slug(provider)
    if not slug:
        return "inactive"

    def _call() -> str:
        try:
            client = _client()
            response = client.connected_accounts.list(
                toolkit_slugs=[slug],
                user_ids=[entity],
                limit=10,
            )
        except Exception:
            return "inactive"
        items = getattr(response, "items", None) or getattr(response, "data", None) or []
        for conn in items:
            status = getattr(conn, "status", None)
            if isinstance(conn, dict):
                status = conn.get("status")
            normalized = str(status or "").lower()
            if normalized == "active":
                return "active"
            if normalized:
                return normalized
        return "inactive"

    return await anyio.to_thread.run_sync(_call)
