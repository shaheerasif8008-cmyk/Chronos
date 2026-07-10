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

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";
import ConnectorGovernanceScreen from "./ConnectorGovernanceScreen";

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
    />
  );
}

function Banner({ kind, children, onDismiss }: { kind: "ok" | "error"; children: React.ReactNode; onDismiss: () => void }) {
  return (
    <div
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

function ToolPermissions({ app, onPermissionChange }: {
  app: CatalogApp;
  onPermissionChange: (tool: ToolSpec, permission: ToolSpec["permission"]) => Promise<void>;
}) {
  const [saving, setSaving] = useState<string | null>(null);
  const readTools = app.tools.filter(t => t.access === "read");
  const writeTools = app.tools.filter(t => t.access === "write");

  async function change(tool: ToolSpec, permission: ToolSpec["permission"]) {
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
            <div key={tool.name} className="py-2 flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="text-[13px] font-medium capitalize">{toolActionLabel(tool.name)}</div>
                <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{tool.description}</div>
              </div>
              {tool.always_approval ? (
                <span className="tag tag-warn shrink-0" title="This action always requires human approval — it cannot be loosened.">Always requires approval</span>
              ) : (
                <select
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

function ConnectorRow({ app, busy, onConnect, onDisconnect, onPermissionChange }: {
  app: CatalogApp;
  busy: string | null;
  onConnect: (app: CatalogApp) => void;
  onDisconnect: (app: CatalogApp) => void;
  onPermissionChange: (tool: ToolSpec, permission: ToolSpec["permission"]) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="surface border border-soft rounded-xl px-4 py-3">
      <div className="flex items-center gap-3.5 min-w-0">
        <AppIcon svg={app.icon_svg} name={app.name} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-semibold text-[14px] truncate">{app.name}</span>
            {app.connected && <span className="inline-block w-1.5 h-1.5 rounded-full shrink-0" style={{ background: "var(--ok, var(--accent))" }} />}
            {app.auth_mode === "composio" && !app.connected && (
              <span className="tag tag-info shrink-0" title="Managed auth is configured — connecting is one click.">Managed auth</span>
            )}
          </div>
          <div className="text-[12.5px] truncate" style={{ color: "var(--text-dim)" }}>
            {app.connected && app.account_handle ? app.account_handle : app.description}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {app.connected ? (
            <>
              <button className="btn btn-ghost btn-sm" onClick={() => setExpanded(v => !v)}>
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
          ) : (
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
                disabled={busy === app.id}
                onClick={() => onConnect(app)}
              >
                {busy === app.id ? "Redirecting…" : "Connect"}
              </button>
            </>
          )}
        </div>
      </div>
      {expanded && app.connected && (
        <ToolPermissions app={app} onPermissionChange={onPermissionChange} />
      )}
    </div>
  );
}

function AddCustomConnectorModal({ onClose, onAdded }: { onClose: () => void; onAdded: () => Promise<void> }) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

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
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6" style={{ background: "rgba(0,0,0,0.45)" }} onClick={onClose}>
      <div className="surface border border-soft rounded-2xl w-full max-w-md p-6" onClick={e => e.stopPropagation()}>
        <div className="text-[16px] font-semibold">Add custom connector</div>
        <p className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
          Connect a remote MCP server. Chronos discovers its tools and makes them available in chats and tasks;
          risky tool calls stay approval-gated by policy.
        </p>
        <div className="mt-4 space-y-3">
          <div>
            <label className="text-[12px] font-medium block mb-1">Name</label>
            <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Internal knowledge base"
                   className="w-full surface border border-soft rounded-md px-3 py-2 text-[13px] outline-none" />
          </div>
          <div>
            <label className="text-[12px] font-medium block mb-1">Remote MCP server URL</label>
            <input value={url} onChange={e => setUrl(e.target.value)} placeholder="https://mcp.example.com/sse"
                   className="w-full surface border border-soft rounded-md px-3 py-2 text-[13px] outline-none" />
          </div>
          {error && <div className="text-[12.5px]" style={{ color: "var(--danger)" }}>{error}</div>}
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

export default function ConnectorsScreen() {
  const [view, setView] = useState<"connectors" | "governance">("connectors");
  const [apps, setApps] = useState<CatalogApp[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [showAddModal, setShowAddModal] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [catalog, mcp] = await Promise.all([
        apiFetch("/connectors/catalog").then(r => r.json()) as Promise<CatalogApp[]>,
        apiFetch("/connectors/mcp").then(r => r.json()).catch(() => ({ servers: [] })) as Promise<{ servers: McpServer[] }>,
      ]);
      setApps(catalog);
      setMcpServers(mcp.servers || []);
    } catch (err) {
      setError(errorMessage(err, "Could not load connectors"));
    } finally {
      setLoading(false);
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
  const advanced = apps.filter(a => !a.connected && a.auth_type !== "oauth2" && !["remote_mcp"].includes(a.id));

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <header className="glass sticky top-0 z-20 px-10 pt-9 pb-6 flex items-start justify-between gap-6 flex-shrink-0 border-b hairline">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Connectors</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Connect Chronos to your tools and data sources. Connected apps become tools the model can use — governed, audited, and approval-gated.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button className="btn btn-accent btn-sm" onClick={() => setShowAddModal(true)}>Add custom connector</button>
        </div>
      </header>

      <div className="px-10 pt-4 pb-2 flex items-center gap-1">
        {([["connectors", "Connectors"], ["governance", "Governance"]] as const).map(([id, label]) => (
          <button key={id} onClick={() => setView(id)}
                  className="px-3 py-1.5 rounded-md text-[13px] font-medium smooth"
                  style={{ background: view === id ? "var(--surface-2)" : "transparent", color: view === id ? "var(--text)" : "var(--text-muted)" }}>
            {label}
          </button>
        ))}
      </div>

      {view === "governance" && (
        <div className="px-10 pb-10 pt-2"><ConnectorGovernanceScreen /></div>
      )}

      {view === "connectors" && (
        <div className="px-10 pb-12 pt-2 max-w-[860px]">
          {notice && <Banner kind="ok" onDismiss={() => setNotice("")}>{notice}</Banner>}
          {error && <Banner kind="error" onDismiss={() => setError("")}>{error}</Banner>}

          {loading && (
            <div className="space-y-3">
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
                      <ConnectorRow key={app.id} app={app} busy={busy}
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
                      <div key={server.id} className="surface border border-soft rounded-xl px-4 py-3 flex items-center gap-3.5">
                        <div className="rounded-xl flex items-center justify-center shrink-0 border border-soft" style={{ background: "var(--surface-2)", width: 36, height: 36 }}>
                          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M5 8h14M5 16h14M8 5v14M16 5v14"/></svg>
                        </div>
                        <div className="min-w-0 flex-1">
                          <div className="font-semibold text-[14px] truncate">{server.name}</div>
                          <div className="text-[12.5px] truncate" style={{ color: "var(--text-dim)" }}>{server.server_url || server.command}</div>
                        </div>
                        <span className="tag shrink-0">{server.transport}</span>
                        <button className="btn btn-ghost btn-sm shrink-0" disabled={busy === server.id} onClick={() => void rediscover(server)}>
                          {busy === server.id ? "Refreshing…" : "Refresh tools"}
                        </button>
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <section className="mb-8">
                <h2 className="text-[13px] font-semibold uppercase tracking-wide mb-3" style={{ color: "var(--text-faint)" }}>Browse connectors</h2>
                {directory.length === 0 ? (
                  <div className="text-[13px] py-4" style={{ color: "var(--text-dim)" }}>All available connectors are connected.</div>
                ) : (
                  <div className="space-y-2.5">
                    {directory.map(app => (
                      <ConnectorRow key={app.id} app={app} busy={busy}
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
                      <div key={app.id} className="surface border border-soft rounded-xl px-4 py-3 flex items-center gap-3.5">
                        <AppIcon svg={app.icon_svg} name={app.name} />
                        <div className="min-w-0 flex-1">
                          <div className="font-semibold text-[14px] truncate">{app.name}</div>
                          <div className="text-[12.5px] truncate" style={{ color: "var(--text-dim)" }}>{app.description}</div>
                        </div>
                        <span className="tag shrink-0" title={app.policy}>{app.auth_type}</span>
                        {app.id === "webhooks" || app.id === "custom_http" ? (
                          <span className="text-[12px] shrink-0" style={{ color: "var(--text-dim)" }}>Configured via environment</span>
                        ) : null}
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

      {showAddModal && <AddCustomConnectorModal onClose={() => setShowAddModal(false)} onAdded={load} />}
    </div>
  );
}
