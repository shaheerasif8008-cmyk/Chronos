"use client";

// Connectors — modeled on Claude.ai's Settings → Connectors.
//
// - Browse a directory of connectors and connect with one click (OAuth).
//   With Composio managed auth configured, no per-provider credentials are
//   needed — Connect goes straight to the provider consent screen.
// - Connected connectors show the account, and expand into a per-tool
//   permissions panel (Always allow / Ask first / Blocked), exactly like
//   Claude's connector tool permissions.
// - Custom connectors: register a remote MCP server by URL; its tools are
//   discovered and become available to the model through platform tools.
// - Governance (execution logs, approvals, policies, traces) stays available
//   as a secondary tab for admins.

import { type KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import ConnectorGovernanceScreen from "./ConnectorGovernanceScreen";
import CustomIntegrationsPanel from "./CustomIntegrationsPanel";

type ToolSpec = {
  name: string;
  broker_name: string;
  description: string;
  access: "read" | "write";
  always_approval: boolean;
  permission: "default" | "always_allow" | "require_approval" | "blocked";
};

type CatalogApp = {
  id: string;
  name: string;
  description: string;
  icon_svg: string;
  category?: string;
  auth_type?: string;
  auth_mode?: "composio" | "direct" | "unconfigured";
  scopes?: string[];
  policy?: string;
  client_id_env?: string;
  client_secret_env?: string;
  configured: boolean;
  connected: boolean;
  account_handle: string;
  health_status?: string;
  health_reason?: string;
  health_checked_at?: string | null;
  health_verified_at?: string | null;
  health_stale?: boolean;
  health_error_code?: string | null;
  provider_health_status?: string;
  connection_health_status?: string | null;
  health_latency_ms?: number | null;
  last_used_at?: string;
  tools: ToolSpec[];
};

type McpServer = {
  id: string;
  name: string;
  transport: string;
  server_url?: string;
  command?: string;
  status?: string;
  created_at?: string;
};

// apiFetch throws Error(raw body text) for non-OK responses and a bare
// TypeError ("Failed to fetch") when the request never got a readable
// response. Turn both into something a human can act on.
function errorMessage(err: unknown, fallback: string): string {
  if (!(err instanceof Error) || !err.message) return fallback;
  if (/failed to fetch|networkerror|load failed/i.test(err.message)) {
    return `${fallback}: the Chronos API did not respond. Check that the backend is running and reachable, then check its logs.`;
  }
  try {
    const parsed = JSON.parse(err.message) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail) return `${fallback}: ${parsed.detail}`;
  } catch { /* not JSON — fall through to the raw message */ }
  return `${fallback}: ${err.message}`;
}

const PERMISSION_OPTIONS: Array<{ value: ToolSpec["permission"]; label: string }> = [
  { value: "default", label: "Default" },
  { value: "always_allow", label: "Always allow" },
  { value: "require_approval", label: "Ask first" },
  { value: "blocked", label: "Blocked" },
];

function AppIcon({ svg, name, size = 36 }: { svg: string; name: string; size?: number }) {
  return (
    <div
      className="rounded-xl flex items-center justify-center shrink-0 border border-soft"
      style={{ background: "var(--surface-2)", width: size, height: size, padding: size * 0.2 }}
      dangerouslySetInnerHTML={{ __html: svg }}
      title={name}
      aria-hidden="true"
    />
  );
}

function Banner({ kind, children, onDismiss }: { kind: "ok" | "error"; children: React.ReactNode; onDismiss: () => void }) {
  return (
    <div
      role={kind === "error" ? "alert" : "status"}
      aria-live={kind === "error" ? "assertive" : "polite"}
      className="mb-5 rounded-xl border border-soft px-4 py-3 text-[13px] flex items-start gap-3"
      style={{ color: kind === "ok" ? "var(--ok)" : "var(--danger)", background: "var(--surface-2)" }}
    >
      <div className="flex-1 min-w-0">{children}</div>
      <button className="underline shrink-0" onClick={onDismiss}>Dismiss</button>
    </div>
  );
}

function toolActionLabel(name: string) {
  const action = name.split("__")[1] || name;
  return action.replaceAll("_", " ");
}

type HealthTone = "ok" | "warn" | "danger" | "info" | "neutral";

function healthPresentation(app: CatalogApp): { label: string; tone: HealthTone; dot: string } {
  const status = app.health_status || (app.connected ? "connected_unverified" : "not_connected");
  if ((status === "verified" || status === "healthy") && !app.health_stale) {
    return { label: "Verified", tone: "ok", dot: "var(--ok)" };
  }
  if (status === "stale" || (app.health_stale && app.connected)) {
    return { label: "Verification stale", tone: "warn", dot: "var(--warn)" };
  }
  if (status === "connected_unverified") {
    return { label: "Connected · not verified", tone: "warn", dot: "var(--warn)" };
  }
  if (status === "configured") {
    return { label: "Setup configured", tone: "info", dot: "var(--info)" };
  }
  if (status === "degraded" || status === "rate_limited") {
    return {
      label: status === "rate_limited" ? "Rate limited" : "Degraded",
      tone: "warn",
      dot: "var(--warn)",
    };
  }
  if (status === "error" || status === "unavailable") {
    return {
      label: status === "error" ? "Verification failed" : "Unavailable",
      tone: "danger",
      dot: "var(--danger)",
    };
  }
  if (status === "demo" || status === "fixture") {
    return {
      label: status === "demo" ? "Demo only" : "Fixture only",
      tone: "warn",
      dot: "var(--warn)",
    };
  }
  return { label: app.connected ? "Connected" : "Not connected", tone: "neutral", dot: "var(--text-faint)" };
}

function healthTagClass(tone: HealthTone): string {
  return {
    ok: "tag-ok",
    warn: "tag-warn",
    danger: "tag-danger",
    info: "tag-info",
    neutral: "",
  }[tone];
}

function readableHealthTime(value?: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function ToolPermissions({ app, onPermissionChange }: {
  app: CatalogApp;
  onPermissionChange: (tool: ToolSpec, permission: ToolSpec["permission"]) => Promise<void>;
}) {
  const [saving, setSaving] = useState<string | null>(null);
  const readTools = app.tools.filter(t => t.access === "read");
  const writeTools = app.tools.filter(t => t.access === "write");

  async function change(tool: ToolSpec, permission: ToolSpec["permission"]) {
    if (
      permission === "always_allow"
      && tool.permission !== "always_allow"
      && !window.confirm(
        `Always allow ${toolActionLabel(tool.name)}? Chronos will run this tool without asking each time. Hard policy floors and audit logging still apply.`,
      )
    ) return;
    setSaving(tool.name);
    try {
      await onPermissionChange(tool, permission);
    } finally {
      setSaving(null);
    }
  }

  function group(label: string, tools: ToolSpec[]) {
    if (tools.length === 0) return null;
    return (
      <div>
        <div className="text-[11.5px] font-semibold uppercase tracking-wide mb-1.5" style={{ color: "var(--text-faint)" }}>{label}</div>
        <div className="divide-y" style={{ borderColor: "var(--border-soft)" }}>
          {tools.map(tool => (
            <div key={tool.name} className="flex flex-col items-stretch gap-2 py-2 sm:flex-row sm:items-center sm:gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium capitalize">{toolActionLabel(tool.name)}</div>
                <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{tool.description}</div>
              </div>
              {tool.always_approval ? (
                <span className="tag tag-warn shrink-0" title="This action always requires human approval — it cannot be loosened.">Always requires approval</span>
              ) : (
                <select
                  aria-label={`${app.name} ${toolActionLabel(tool.name)} permission`}
                  className="surface border border-soft rounded-md px-2 py-1 text-[12px] outline-none shrink-0"
                  value={tool.permission}
                  disabled={saving === tool.name}
                  onChange={e => void change(tool, e.target.value as ToolSpec["permission"])}
                >
                  {PERMISSION_OPTIONS.map(opt => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              )}
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (app.tools.length === 0) {
    return (
      <div className="text-[12.5px] py-2" style={{ color: "var(--text-dim)" }}>
        This connector is used through discovery tools (platform actions); it has no fixed tool list.
      </div>
    );
  }

  return (
    <div className="mt-3 pt-3 border-t hairline space-y-4">
      <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>
        Tool permissions control what Chronos can do with this connector. &ldquo;Ask first&rdquo; routes the call
        through your approval inbox; &ldquo;Blocked&rdquo; hides the tool from the model entirely.
      </div>
      {group("Read tools", readTools)}
      {group("Write & send tools", writeTools)}
    </div>
  );
}

function ConnectorRow({ app, busy, canManage, onConnect, onDisconnect, onPermissionChange }: {
  app: CatalogApp;
  busy: string | null;
  canManage: boolean;
  onConnect: (app: CatalogApp) => void;
  onDisconnect: (app: CatalogApp) => void;
  onPermissionChange: (tool: ToolSpec, permission: ToolSpec["permission"]) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  const health = healthPresentation(app);
  const checkedAt = readableHealthTime(app.health_checked_at);
  const verifiedAt = readableHealthTime(app.health_verified_at);

  return (
    <div className="surface border border-soft rounded-xl px-4 py-3">
      <div className="flex flex-wrap sm:flex-nowrap items-center gap-3.5 min-w-0">
        <AppIcon svg={app.icon_svg} name={app.name} />
        <div className="min-w-0 flex-1 basis-[220px]">
          <div className="flex flex-wrap items-center gap-2 min-w-0">
            <span className="font-semibold text-[14px] truncate">{app.name}</span>
            <span
              className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
              style={{ background: health.dot }}
              aria-hidden="true"
              title={app.health_reason || health.label}
            />
            <span className={`tag ${healthTagClass(health.tone)} shrink-0`} title={app.health_reason || health.label} aria-live="polite">
              {health.label}
            </span>
            {app.auth_mode === "composio" && !app.connected && (
              <span className="tag tag-info shrink-0" title="Managed auth is configured — connecting is one click.">Managed auth</span>
            )}
          </div>
          <div className="text-[12.5px] truncate" style={{ color: "var(--text-dim)" }}>
            {app.connected && app.account_handle ? app.account_handle : app.description}
          </div>
          {app.health_reason && (
            <div className="text-[11.5px] mt-1" style={{ color: "var(--text-muted)" }}>
              {app.health_reason}
            </div>
          )}
          {(checkedAt || verifiedAt) && (
            <div className="text-[11px] mt-1 font-mono" style={{ color: "var(--text-faint)" }}>
              {verifiedAt ? `Verified ${verifiedAt}` : `Checked ${checkedAt}`}
              {app.health_latency_ms != null ? ` · ${app.health_latency_ms} ms` : ""}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0 ml-auto">
          {app.connected && canManage ? (
            <>
              <button className="btn btn-ghost btn-sm" aria-expanded={expanded} aria-controls={`connector-tools-${app.id}`} onClick={() => setExpanded(v => !v)}>
                {expanded ? "Close" : "Configure"}
              </button>
              <button
                className="btn btn-danger-soft btn-sm disabled:opacity-50"
                disabled={busy === app.id}
                onClick={() => onDisconnect(app)}
              >
                {busy === app.id ? "Disconnecting…" : "Disconnect"}
              </button>
            </>
          ) : !app.connected && canManage ? (
            <>
              {!app.configured && (
                <span
                  className="tag shrink-0"
                  title={`Set COMPOSIO_API_KEY (managed auth) or ${app.client_id_env || "CLIENT_ID"} + ${app.client_secret_env || "CLIENT_SECRET"} in the API environment.`}
                >
                  Requires setup
                </span>
              )}
              <button
                className="btn btn-accent btn-sm disabled:opacity-50"
                disabled={busy === app.id || !app.configured}
                title={!app.configured ? "Configure provider credentials before connecting an account." : undefined}
                onClick={() => onConnect(app)}
              >
                {busy === app.id ? "Redirecting…" : "Connect"}
              </button>
            </>
          ) : null}
        </div>
      </div>
      {expanded && app.connected && (
        <div id={`connector-tools-${app.id}`}><ToolPermissions app={app} onPermissionChange={onPermissionChange} /></div>
      )}
    </div>
  );
}

function AddCustomConnectorModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(
    typeof document !== "undefined" && document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null,
  );

  useEffect(() => {
    const previousFocus = previousFocusRef.current;
    return () => previousFocus?.focus();
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
      if (event.key === "Tab" && dialogRef.current) {
        const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')]
          .filter(element => element.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  async function submit() {
    if (!name.trim() || !url.trim()) return;
    setBusy(true);
    setError("");
    try {
      const res = await apiFetch("/connectors/mcp/register", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), transport: "remote", server_url: url.trim() }),
      });
      const server = await res.json() as { id?: string };
      if (server.id) {
        // Discover tools right away so the model can use it immediately.
        await apiFetch(`/connectors/mcp/${server.id}/discover`, { method: "POST" }).catch(() => null);
      }
      await onAdded();
      onClose();
    } catch (err) {
      setError(errorMessage(err, "Could not add connector"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-6" style={{ background: "rgba(0,0,0,0.45)" }} onClick={() => { if (!busy) onClose(); }}>
      <div ref={dialogRef} tabIndex={-1} className="surface border border-soft max-h-[calc(100dvh-24px)] w-full max-w-md overflow-y-auto rounded-2xl p-4 sm:p-6" onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-labelledby="add-connector-title" aria-describedby="add-connector-description">
        <div id="add-connector-title" className="text-[16px] font-semibold">Add custom connector</div>
        <p id="add-connector-description" className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
          Connect a remote MCP server. Chronos discovers its tools and makes them available in chats and tasks;
          risky tool calls stay approval-gated by policy.
        </p>
        <div className="mt-4 space-y-3">
          <div>
            <label htmlFor="custom-connector-name" className="text-[12px] font-medium block mb-1">Name</label>
            <input id="custom-connector-name" autoFocus value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Internal knowledge base"
                   className="w-full surface border border-soft rounded-md px-3 py-2 text-[13px] outline-none" />
          </div>
          <div>
            <label htmlFor="custom-connector-url" className="text-[12px] font-medium block mb-1">Remote MCP server URL</label>
            <input id="custom-connector-url" type="url" inputMode="url" autoCapitalize="none" autoCorrect="off" value={url} onChange={e => setUrl(e.target.value)} placeholder="https://mcp.example.com/sse"
                   className="w-full surface border border-soft rounded-md px-3 py-2 text-[13px] outline-none" />
          </div>
          {error && <div role="alert" className="text-[12.5px]" style={{ color: "var(--danger)" }}>{error}</div>}
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button className="btn btn-ghost btn-sm" onClick={onClose}>Cancel</button>
          <button className="btn btn-accent btn-sm disabled:opacity-50" disabled={busy || !name.trim() || !url.trim()} onClick={() => void submit()}>
            {busy ? "Adding…" : "Add connector"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function ConnectorsScreen({ memberRole }: { memberRole: string }) {
  const canManage = memberRole === "admin" || memberRole === "owner";
  const [view, setView] = useState<"connectors" | "governance">("connectors");
  const [apps, setApps] = useState<CatalogApp[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showAddModal, setShowAddModal] = useState(false);
  const addConnectorTriggerRef = useRef<HTMLButtonElement>(null);

  const closeAddConnectorModal = useCallback(() => {
    setShowAddModal(false);
    window.requestAnimationFrame(() => addConnectorTriggerRef.current?.focus());
  }, []);

  const load = useCallback(async (refreshHealth = false) => {
    if (refreshHealth) setRefreshing(true);
    else setLoading(true);
    try {
      const [catalog, mcp] = await Promise.all([
        apiFetch(`/connectors/catalog${refreshHealth ? "?refresh_health=true" : ""}`).then(r => r.json()) as Promise<CatalogApp[]>,
        apiFetch("/connectors/mcp").then(r => r.json()).catch(() => ({ servers: [] })) as Promise<{ servers: McpServer[] }>,
      ]);
      setApps(catalog);
      setMcpServers(mcp.servers || []);
    } catch (err) {
      setError(errorMessage(err, "Could not load connectors"));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // OAuth callback results arrive as query params after the provider redirect.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const callbackError = params.get("connector_error") || params.get("error");
    const callbackSuccess = params.get("connector_success");
    if (callbackError) setError(callbackError);
    else if (callbackSuccess) setNotice(`${callbackSuccess.replaceAll("_", " ")} connected.`);
    if (callbackError || callbackSuccess) {
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  async function connect(app: CatalogApp) {
    setBusy(app.id);
    setError("");
    setNotice("");
    try {
      const endpoint = app.id === "gmail" ? "/connectors/gmail/oauth-start" : `/connectors/${app.id}/oauth-start`;
      const res = await apiFetch(endpoint, { method: "POST" });
      const { url } = await res.json() as { url: string };
      window.location.href = url;
    } catch (err) {
      setError(errorMessage(err, `Could not connect ${app.name}`));
      setBusy(null);
    }
  }

  async function disconnect(app: CatalogApp) {
    if (!confirm(`Disconnect ${app.name}? Chronos will no longer be able to access it.`)) return;
    setBusy(app.id);
    setError("");
    try {
      await apiFetch(`/connectors/${app.id}/disconnect`, { method: "DELETE" });
      await load();
    } catch (err) {
      setError(errorMessage(err, `Could not disconnect ${app.name}`));
    } finally {
      setBusy(null);
    }
  }

  async function setPermission(tool: ToolSpec, permission: ToolSpec["permission"]) {
    try {
      await apiFetch(`/connectors/tool-permissions/${tool.name}`, {
        method: "PUT",
        body: JSON.stringify({ permission }),
      });
      setApps(prev => prev.map(app => ({
        ...app,
        tools: app.tools.map(t => (t.name === tool.name ? { ...t, permission } : t)),
      })));
    } catch (err) {
      setError(errorMessage(err, "Could not update tool permission"));
    }
  }

  async function rediscover(server: McpServer) {
    setBusy(server.id);
    try {
      await apiFetch(`/connectors/mcp/${server.id}/discover`, { method: "POST" });
      setNotice(`${server.name}: tools refreshed.`);
    } catch {
      setError(`Could not refresh tools for ${server.name}.`);
    } finally {
      setBusy(null);
    }
  }

  const connected = apps.filter(a => a.connected);
  const directory = apps.filter(a => !a.connected && a.auth_type === "oauth2");
  const advanced = apps.filter(a => !a.connected && a.auth_type !== "oauth2" && !["remote_mcp", "webhooks", "custom_http"].includes(a.id));

  function handleViewTabKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
    if (tabs.length === 0) return;
    event.preventDefault();
    const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === "Home"
      ? tabs[0]
      : event.key === "End"
        ? tabs[tabs.length - 1]
        : event.key === "ArrowRight"
          ? tabs[(current + 1 + tabs.length) % tabs.length]
          : tabs[(current - 1 + tabs.length) % tabs.length];
    next.focus();
    next.click();
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <header className="glass sticky top-0 z-20 px-4 sm:px-6 lg:px-10 pt-7 lg:pt-9 pb-5 lg:pb-6 flex flex-wrap items-start justify-between gap-4 lg:gap-6 flex-shrink-0 border-b hairline">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Connectors</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Connect Chronos to your tools and data sources. Connected apps become tools the model can use — governed, audited, and approval-gated.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2 flex-shrink-0">
          <button className="btn btn-ghost btn-sm" disabled={loading || refreshing} aria-busy={refreshing} onClick={() => void load(true)}>
            {refreshing ? "Verifying…" : "Refresh status"}
          </button>
          {canManage && (
            <button ref={addConnectorTriggerRef} className="btn btn-accent btn-sm" onClick={() => setShowAddModal(true)}>Add custom connector</button>
          )}
        </div>
      </header>

      <div role="tablist" aria-label="Connector views" onKeyDown={handleViewTabKeyDown} className="px-4 sm:px-6 lg:px-10 pt-4 pb-2 flex items-center gap-1">
        {([["connectors", "Connectors"], ...(canManage ? [["governance", "Governance"]] as const : [])] as const).map(([id, label]) => (
          <button key={id} id={`connectors-tab-${id}`} role="tab" aria-selected={view === id} aria-controls={`connectors-panel-${id}`} tabIndex={view === id ? 0 : -1} onClick={() => setView(id)}
                  className="px-3 py-1.5 rounded-md text-[13px] font-medium smooth"
                  style={{ background: view === id ? "var(--surface-2)" : "transparent", color: view === id ? "var(--text)" : "var(--text-muted)" }}>
            {label}
          </button>
        ))}
      </div>

      {view === "governance" && canManage && (
        <div id="connectors-panel-governance" role="tabpanel" aria-labelledby="connectors-tab-governance" tabIndex={0} className="px-4 sm:px-6 lg:px-10 pb-10 pt-2"><ConnectorGovernanceScreen /></div>
      )}

      {view === "connectors" && (
        <div id="connectors-panel-connectors" role="tabpanel" aria-labelledby="connectors-tab-connectors" tabIndex={0} className="px-4 sm:px-6 lg:px-10 pb-12 pt-2 max-w-[860px]">
          {notice && <Banner kind="ok" onDismiss={() => setNotice("")}>{notice}</Banner>}
          {error && <Banner kind="error" onDismiss={() => setError("")}>{error}</Banner>}

          {loading && (
            <div role="status" aria-label="Loading connectors" className="space-y-3">
              {[1, 2, 3, 4].map(i => <div key={i} className="surface border border-soft rounded-xl h-16 animate-pulse" />)}
            </div>
          )}

          {!loading && (
            <>
              {connected.length > 0 && (
                <section className="mb-8">
                  <h2 className="text-[13px] font-semibold uppercase tracking-wide mb-3" style={{ color: "var(--text-faint)" }}>Connected</h2>
                  <div className="space-y-2.5">
                    {connected.map(app => (
                      <ConnectorRow key={app.id} app={app} busy={busy} canManage={canManage}
                                    onConnect={connect} onDisconnect={disconnect}
                                    onPermissionChange={setPermission} />
                    ))}
                  </div>
                </section>
              )}

              {mcpServers.length > 0 && (
                <section className="mb-8">
                  <h2 className="text-[13px] font-semibold uppercase tracking-wide mb-3" style={{ color: "var(--text-faint)" }}>Custom connectors</h2>
                  <div className="space-y-2.5">
                    {mcpServers.map(server => (
                      <div key={server.id} className="surface border border-soft rounded-xl px-4 py-3 flex flex-wrap items-center gap-3.5 sm:flex-nowrap">
                        <span className="tag shrink-0" aria-hidden="true">MCP</span>
                        <div className="min-w-0 flex-1">
                          <div className="font-semibold text-[14px] truncate">{server.name}</div>
                          <div className="text-[12.5px] truncate" style={{ color: "var(--text-dim)" }}>{server.server_url || server.command}</div>
                        </div>
                        <span className="tag shrink-0">{server.transport}</span>
                        {canManage && (
                          <button className="btn btn-ghost btn-sm shrink-0" disabled={busy === server.id} onClick={() => void rediscover(server)}>
                            {busy === server.id ? "Refreshing…" : "Refresh tools"}
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                </section>
              )}

              {canManage && <CustomIntegrationsPanel />}

              <section className="mb-8">
                <h2 className="text-[13px] font-semibold uppercase tracking-wide mb-3" style={{ color: "var(--text-faint)" }}>Browse connectors</h2>
                {directory.length === 0 ? (
                  <div className="text-[13px] py-4" style={{ color: "var(--text-dim)" }}>All available connectors are connected.</div>
                ) : (
                  <div className="space-y-2.5">
                    {directory.map(app => (
                      <ConnectorRow key={app.id} app={app} busy={busy} canManage={canManage}
                                    onConnect={connect} onDisconnect={disconnect}
                                    onPermissionChange={setPermission} />
                    ))}
                  </div>
                )}
              </section>

              {advanced.length > 0 && (
                <section className="mb-4">
                  <h2 className="text-[13px] font-semibold uppercase tracking-wide mb-3" style={{ color: "var(--text-faint)" }}>Advanced integrations</h2>
                  <div className="space-y-2.5">
                    {advanced.map(app => (
                      <div key={app.id} className="surface border border-soft rounded-xl px-4 py-3 flex flex-wrap items-center gap-3.5 sm:flex-nowrap">
                        <AppIcon svg={app.icon_svg} name={app.name} />
                        <div className="min-w-0 flex-1">
                          <div className="font-semibold text-[14px] truncate">{app.name}</div>
                          <div className="text-[12.5px] truncate" style={{ color: "var(--text-dim)" }}>{app.description}</div>
                        </div>
                        <span className="tag shrink-0" title={app.policy}>{app.auth_type}</span>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <p className="mt-6 text-[12px]" style={{ color: "var(--text-dim)" }}>
                OAuth connectors use the provider&rsquo;s consent screen — Chronos never sees your password.
                Every action runs through the governed tool broker with audit logging; sending, posting, and
                publishing always require approval.
              </p>
            </>
          )}
        </div>
      )}

      {showAddModal && canManage && <AddCustomConnectorModal onClose={closeAddConnectorModal} onAdded={load} />}
    </div>
  );
}
