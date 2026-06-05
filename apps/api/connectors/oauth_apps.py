"""
OAuth2 App Catalog — config-driven integrations.

Adding a new app = add one entry here + set CLIENT_ID/CLIENT_SECRET in .env.
The generic OAuth2 engine in routers/connectors.py handles the rest.

Each app entry:
  auth_url        — Google/Notion/Slack consent screen
  token_url       — token exchange + refresh endpoint
  scopes          — default scopes requested
  api_base        — base URL for authenticated API calls
  token_style     — "bearer" (default) or "basic" (GitHub PAT-style)
  has_refresh     — False for apps that don't issue refresh tokens (GitHub)
  extra_auth_params — extra params to add to the consent URL
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class OAuthApp:
    id: str                          # e.g. "notion", "slack"
    name: str
    description: str
    icon_svg: str                    # inline SVG or emoji fallback
    auth_url: str
    token_url: str
    scopes: list[str]
    api_base: str
    category: str = "Productivity"
    auth_type: str = "oauth2"
    actions: list[str] = field(default_factory=lambda: ["search", "read", "write"])
    risk_levels: list[str] = field(default_factory=lambda: ["read", "write"])
    sync_supported: bool = True
    policy: str = "Read actions can run with granted scopes; write actions require connector policy and may require approval."
    has_refresh: bool = True
    token_style: str = "bearer"
    extra_auth_params: dict[str, str] = field(default_factory=dict)
    # env var names for client credentials
    client_id_env: str = ""          # e.g. "NOTION_CLIENT_ID"
    client_secret_env: str = ""


APPS: dict[str, OAuthApp] = {
    "gmail": OAuthApp(
        id="gmail",
        name="Gmail",
        description="Read inbox, search emails, and create drafts.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><path d="M20 4H4C2.9 4 2 4.9 2 6v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z" fill="#EA4335"/></svg>',
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
        api_base="https://gmail.googleapis.com/gmail/v1/users/me",
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        client_id_env="GOOGLE_CLIENT_ID",
        client_secret_env="GOOGLE_CLIENT_SECRET",
    ),
    "google_calendar": OAuthApp(
        id="google_calendar",
        name="Google Calendar",
        description="Read, create, and update calendar events.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="18" rx="2" stroke="#4285F4" stroke-width="2"/><path d="M16 2v4M8 2v4M3 10h18" stroke="#4285F4" stroke-width="2"/><text x="12" y="18" text-anchor="middle" font-size="7" fill="#4285F4">31</text></svg>',
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ],
        api_base="https://www.googleapis.com/calendar/v3",
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        client_id_env="GOOGLE_CLIENT_ID",
        client_secret_env="GOOGLE_CLIENT_SECRET",
    ),
    "google_drive": OAuthApp(
        id="google_drive",
        name="Google Drive",
        description="Search, read, and create files in Google Drive.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><path d="M7.5 14.5L3 21h18l-4.5-7.5H7.5z" fill="#FBBC04"/><path d="M12 3L7.5 14.5h9L12 3z" fill="#34A853"/><path d="M3 21l4.5-7.5L3 7.5 3 21z" fill="#EA4335"/></svg>',
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ],
        api_base="https://www.googleapis.com/drive/v3",
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
        client_id_env="GOOGLE_CLIENT_ID",
        client_secret_env="GOOGLE_CLIENT_SECRET",
    ),
    "notion": OAuthApp(
        id="notion",
        name="Notion",
        description="Search pages, read databases, and create content in Notion.",
        icon_svg='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4.5 3h15a1.5 1.5 0 011.5 1.5v15a1.5 1.5 0 01-1.5 1.5h-15A1.5 1.5 0 013 19.5v-15A1.5 1.5 0 014.5 3zm2 3v12h11V6h-11z"/></svg>',
        auth_url="https://api.notion.com/v1/oauth/authorize",
        token_url="https://api.notion.com/v1/oauth/token",
        scopes=[],  # Notion doesn't use scope params — access level set in consent UI
        api_base="https://api.notion.com/v1",
        has_refresh=False,  # Notion tokens don't expire
        extra_auth_params={"owner": "user"},
        client_id_env="NOTION_CLIENT_ID",
        client_secret_env="NOTION_CLIENT_SECRET",
    ),
    "slack": OAuthApp(
        id="slack",
        name="Slack",
        description="Send messages, search conversations, and read channels.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><path d="M6 15a2 2 0 01-2 2 2 2 0 01-2-2 2 2 0 012-2h2v2zm1 0a2 2 0 012-2 2 2 0 012 2v5a2 2 0 01-2 2 2 2 0 01-2-2v-5zM15 6a2 2 0 012-2 2 2 0 012 2 2 2 0 01-2 2h-2V6zm-1 0a2 2 0 01-2 2 2 2 0 01-2-2V1a2 2 0 012-2 2 2 0 012 2v5zM9 18a2 2 0 012 2 2 2 0 012-2h5a2 2 0 012 2 2 2 0 01-2 2H9v-2zm0-1a2 2 0 01-2-2 2 2 0 012-2h2v2a2 2 0 01-2 2zM15 9a2 2 0 012 2 2 2 0 01-2 2h-2V9h2zm1 0a2 2 0 01-2-2V2a2 2 0 012-2 2 2 0 012 2v7h-2z" fill="#E01E5A"/></svg>',
        auth_url="https://slack.com/oauth/v2/authorize",
        token_url="https://slack.com/api/oauth.v2.access",
        scopes=["channels:read", "channels:history", "chat:write", "search:read", "users:read"],
        api_base="https://slack.com/api",
        has_refresh=False,  # Slack tokens don't expire (workspace bot tokens)
        client_id_env="SLACK_CLIENT_ID",
        client_secret_env="SLACK_CLIENT_SECRET",
    ),
    "github": OAuthApp(
        id="github",
        name="GitHub",
        description="Search repos, read files, create issues and pull requests.",
        icon_svg='<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.019 10.019 0 0022 12.017C22 6.484 17.522 2 12 2z"/></svg>',
        auth_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes=["repo", "read:user", "read:org"],
        api_base="https://api.github.com",
        has_refresh=False,  # GitHub tokens don't expire
        client_id_env="GITHUB_CLIENT_ID",
        client_secret_env="GITHUB_CLIENT_SECRET",
    ),
    "linear": OAuthApp(
        id="linear",
        name="Linear",
        description="Search issues, create tasks, and update project status.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><path d="M3.5 3.5L20.5 20.5M3.5 20.5V3.5H20.5V20.5H3.5z" stroke="#5E6AD2" stroke-width="2.5" stroke-linecap="round"/></svg>',
        auth_url="https://linear.app/oauth/authorize",
        token_url="https://api.linear.app/oauth/token",
        scopes=["read", "write", "issues:create"],
        api_base="https://api.linear.app",
        extra_auth_params={"prompt": "consent"},
        client_id_env="LINEAR_CLIENT_ID",
        client_secret_env="LINEAR_CLIENT_SECRET",
    ),
    "hubspot": OAuthApp(
        id="hubspot",
        name="HubSpot",
        description="Search contacts, read deals, and update CRM records.",
        icon_svg='<svg viewBox="0 0 24 24" fill="#FF7A59"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5.64 5.64l2.83 2.83M15.54 15.54l2.83 2.83M5.64 18.36l2.83-2.83M15.54 8.46l2.83-2.83" stroke="#FF7A59" stroke-width="2"/></svg>',
        auth_url="https://app.hubspot.com/oauth/authorize",
        token_url="https://api.hubapi.com/oauth/v1/token",
        scopes=["crm.objects.contacts.read", "crm.objects.contacts.write", "crm.objects.deals.read"],
        api_base="https://api.hubapi.com",
        client_id_env="HUBSPOT_CLIENT_ID",
        client_secret_env="HUBSPOT_CLIENT_SECRET",
    ),
    "airtable": OAuthApp(
        id="airtable",
        name="Airtable",
        description="Read and write records across bases and tables.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><rect x="2" y="3" width="20" height="7" rx="1.5" fill="#FCB400"/><rect x="2" y="13" width="9" height="8" rx="1.5" fill="#18BFFF"/><rect x="13" y="13" width="9" height="8" rx="1.5" fill="#F82B60"/></svg>',
        auth_url="https://airtable.com/oauth2/v1/authorize",
        token_url="https://airtable.com/oauth2/v1/token",
        scopes=["data.records:read", "data.records:write", "schema.bases:read"],
        api_base="https://api.airtable.com/v0",
        extra_auth_params={"prompt": "consent"},
        client_id_env="AIRTABLE_CLIENT_ID",
        client_secret_env="AIRTABLE_CLIENT_SECRET",
    ),
    "jira": OAuthApp(
        id="jira",
        name="Jira",
        description="Search issues, create tickets, and update project status.",
        icon_svg='<svg viewBox="0 0 24 24" fill="#0052CC"><path d="M12 2L2 12l10 10 10-10L12 2zm0 3.5L18.5 12 12 18.5 5.5 12 12 5.5z"/></svg>',
        auth_url="https://auth.atlassian.com/authorize",
        token_url="https://auth.atlassian.com/oauth/token",
        scopes=["read:jira-work", "write:jira-work", "read:jira-user", "offline_access"],
        api_base="https://api.atlassian.com",
        extra_auth_params={"audience": "api.atlassian.com", "prompt": "consent"},
        client_id_env="JIRA_CLIENT_ID",
        client_secret_env="JIRA_CLIENT_SECRET",
    ),
    "outlook": OAuthApp(
        id="outlook",
        name="Outlook",
        description="Search mail, read messages, and draft outbound email through Microsoft Graph.",
        icon_svg='<svg viewBox="0 0 24 24" fill="#0078D4"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M5 7l7 5 7-5" fill="none" stroke="#fff" stroke-width="2"/></svg>',
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=["offline_access", "User.Read", "Mail.Read", "Mail.Send"],
        api_base="https://graph.microsoft.com/v1.0",
        client_id_env="MICROSOFT_CLIENT_ID",
        client_secret_env="MICROSOFT_CLIENT_SECRET",
        risk_levels=["read", "external_message"],
        policy="Reading mail is scoped by Graph permissions; sending or drafting outbound messages is approval-gated by policy.",
    ),
    "teams": OAuthApp(
        id="teams",
        name="Teams",
        description="Read channels and publish approved messages to Microsoft Teams.",
        icon_svg='<svg viewBox="0 0 24 24" fill="#6264A7"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 9h8M12 9v7" stroke="#fff" stroke-width="2"/></svg>',
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=["offline_access", "User.Read", "ChannelMessage.Read.All", "ChannelMessage.Send"],
        api_base="https://graph.microsoft.com/v1.0",
        client_id_env="MICROSOFT_CLIENT_ID",
        client_secret_env="MICROSOFT_CLIENT_SECRET",
        risk_levels=["read", "external_message"],
        policy="Channel reads are scoped; publishing to Teams requires connector policy and approval when configured.",
    ),
    "sharepoint_onedrive": OAuthApp(
        id="sharepoint_onedrive",
        name="SharePoint / OneDrive",
        description="Search, read, and sync Microsoft 365 files as project knowledge.",
        icon_svg='<svg viewBox="0 0 24 24" fill="#036C70"><path d="M4 12a5 5 0 019-3 4 4 0 117 3.5H6A2 2 0 014 12z"/></svg>',
        auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        scopes=["offline_access", "User.Read", "Files.Read.All", "Sites.Read.All", "Files.ReadWrite.All"],
        api_base="https://graph.microsoft.com/v1.0",
        client_id_env="MICROSOFT_CLIENT_ID",
        client_secret_env="MICROSOFT_CLIENT_SECRET",
        risk_levels=["read", "write"],
    ),
    "salesforce": OAuthApp(
        id="salesforce",
        name="Salesforce",
        description="Search accounts, read opportunities, and update CRM records under policy.",
        icon_svg='<svg viewBox="0 0 24 24" fill="#00A1E0"><path d="M8 17h9a4 4 0 000-8 5 5 0 00-9-2 4 4 0 100 10z"/></svg>',
        auth_url="https://login.salesforce.com/services/oauth2/authorize",
        token_url="https://login.salesforce.com/services/oauth2/token",
        scopes=["api", "refresh_token"],
        api_base="https://your-instance.salesforce.com/services/data/v60.0",
        client_id_env="SALESFORCE_CLIENT_ID",
        client_secret_env="SALESFORCE_CLIENT_SECRET",
    ),
    "stripe": OAuthApp(
        id="stripe",
        name="Stripe",
        description="Read customers, inspect payments, and perform approval-gated financial actions.",
        icon_svg='<svg viewBox="0 0 24 24" fill="#635BFF"><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M8 15c1.5 1 6 1 6-1 0-3-6-1-6-4 0-2 4-2 6-1" stroke="#fff" stroke-width="2" fill="none"/></svg>',
        auth_url="https://connect.stripe.com/oauth/authorize",
        token_url="https://connect.stripe.com/oauth/token",
        scopes=["read_write"],
        api_base="https://api.stripe.com/v1",
        has_refresh=False,
        client_id_env="STRIPE_CLIENT_ID",
        client_secret_env="STRIPE_CLIENT_SECRET",
        category="Finance",
        risk_levels=["read", "financial"],
        policy="Read-only inspection is scoped; charges, refunds, payouts, and subscription mutations require explicit approval.",
    ),
    "webhooks": OAuthApp(
        id="webhooks",
        name="Webhooks",
        description="Receive event payloads from external systems and trigger governed Chronos work.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><path d="M7 8a5 5 0 017-3M7 16a5 5 0 0014-3M11 20a5 5 0 01-7-7" stroke="currentColor" stroke-width="2"/></svg>',
        auth_url="",
        token_url="",
        scopes=["webhook.receive", "webhook.trigger"],
        api_base="",
        auth_type="signing_secret",
        actions=["receive", "test_event", "disable"],
        risk_levels=["read", "write"],
        sync_supported=False,
        has_refresh=False,
        client_id_env="WEBHOOK_SIGNING_KEY",
        client_secret_env="WEBHOOK_SIGNING_SECRET",
        policy="Inbound payloads are treated as untrusted content and can only trigger policy-allowed workflows.",
    ),
    "custom_http": OAuthApp(
        id="custom_http",
        name="Custom HTTP",
        description="Define an API connector from an OpenAPI document or manual request schema.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><path d="M8 8l-4 4 4 4M16 8l4 4-4 4M14 5l-4 14" stroke="currentColor" stroke-width="2"/></svg>',
        auth_url="",
        token_url="",
        scopes=["http.request", "http.schema"],
        api_base="",
        auth_type="api_key",
        actions=["discover_schema", "request"],
        risk_levels=["read", "write"],
        sync_supported=True,
        has_refresh=False,
        client_id_env="CUSTOM_HTTP_API_BASE",
        client_secret_env="CUSTOM_HTTP_API_KEY",
        policy="Every request runs through the broker with redaction; non-GET requests are approval-gated by connector policy.",
    ),
    "remote_mcp": OAuthApp(
        id="remote_mcp",
        name="Remote MCP",
        description="Register remote MCP servers, discover tools, and allow or deny actions by policy.",
        icon_svg='<svg viewBox="0 0 24 24" fill="none"><path d="M5 8h14M5 16h14M8 5v14M16 5v14" stroke="currentColor" stroke-width="2"/></svg>',
        auth_url="",
        token_url="",
        scopes=["mcp.discover", "mcp.execute"],
        api_base="",
        auth_type="remote_mcp",
        actions=["register", "discover", "execute_tool"],
        risk_levels=["read", "write"],
        sync_supported=True,
        has_refresh=False,
        client_id_env="REMOTE_MCP_URL",
        client_secret_env="REMOTE_MCP_TOKEN",
        policy="Discovered tools are disabled until explicitly allowed; risky tool calls follow connector approval policy.",
    ),
}


def get_app(provider: str) -> OAuthApp | None:
    return APPS.get(provider)


def get_client_credentials(app: OAuthApp) -> tuple[str, str]:
    """Read client_id and client_secret from environment via settings."""
    import os
    client_id = os.environ.get(app.client_id_env, "")
    client_secret = os.environ.get(app.client_secret_env, "")
    return client_id, client_secret


def available_apps() -> list[dict]:
    """Return catalog entries — each includes whether credentials are configured."""
    import os
    result = []
    for app in APPS.values():
        client_id = os.environ.get(app.client_id_env, "")
        client_secret = os.environ.get(app.client_secret_env, "")
        result.append({
            "id": app.id,
            "name": app.name,
            "description": app.description,
            "icon_svg": app.icon_svg,
            "category": app.category,
            "auth_type": app.auth_type,
            "scopes": app.scopes,
            "actions": app.actions,
            "risk_levels": app.risk_levels,
            "sync_supported": app.sync_supported,
            "policy": app.policy,
            "client_id_env": app.client_id_env,
            "client_secret_env": app.client_secret_env,
            "configured": bool(client_id and client_secret),
        })
    return result
