"use client";

import { type KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";

type Row = Record<string, unknown>;

const GOV_TABS = [
  { id: "logs", label: "Execution logs" },
  { id: "health", label: "Health" },
  { id: "approvals", label: "Approvals" },
  { id: "policies", label: "Policies" },
  { id: "mcp", label: "MCP servers" },
  { id: "traces", label: "Traces" },
  { id: "jobs", label: "Jobs" },
] as const;
type GovTab = typeof GOV_TABS[number]["id"];

function labelTime(value: unknown) {
  if (!value || typeof value !== "string") return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function statusColor(status: unknown) {
  const s = String(status || "");
  if (["ok", "healthy", "active", "approved", "success", "completed"].includes(s)) return "var(--accent)";
  if (["error", "failed", "denied", "rejected", "down"].includes(s)) return "var(--danger)";
  if (["degraded", "pending", "warn"].includes(s)) return "var(--warn)";
  return "var(--text-faint)";
}

function field(row: Row, ...keys: string[]): string {
  for (const key of keys) {
    const v = row[key];
    if (v !== undefined && v !== null && v !== "") return String(v);
  }
  return "";
}

export default function ConnectorGovernanceScreen() {
  const [tab, setTab] = useState<GovTab>("logs");
  const [logs, setLogs] = useState<Row[]>([]);
  const [health, setHealth] = useState<Row[]>([]);
  const [approvals, setApprovals] = useState<Row[]>([]);
  const [policies, setPolicies] = useState<Row[]>([]);
  const [mcp, setMcp] = useState<{ servers: Row[]; discovery_logs: Row[] }>({ servers: [], discovery_logs: [] });
  const [traces, setTraces] = useState<Row[]>([]);
  const [jobs, setJobs] = useState<Row[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // MCP register form
  const [mcpName, setMcpName] = useState("");
  const [mcpTarget, setMcpTarget] = useState("");

  // Policy create form
  const [policyConnector, setPolicyConnector] = useState("");
  const [policyAction, setPolicyAction] = useState("");
  const [policyDecision, setPolicyDecision] = useState<"allow" | "deny" | "require_approval">("require_approval");

  const load = useCallback(async (which: GovTab) => {
    setLoading(true);
    setError(null);
    try {
      if (which === "logs") setLogs(await (await apiFetch("/connectors/execution-logs")).json());
      else if (which === "health") setHealth(await (await apiFetch("/connectors/health")).json());
      else if (which === "approvals") setApprovals(await (await apiFetch("/connectors/approvals?status=pending")).json());
      else if (which === "policies") setPolicies(await (await apiFetch("/connectors/policies")).json());
      else if (which === "mcp") setMcp(await (await apiFetch("/connectors/mcp")).json());
      else if (which === "traces") setTraces(await (await apiFetch("/connectors/execution-traces")).json());
      else if (which === "jobs") setJobs(await (await apiFetch("/connectors/execution-jobs")).json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(tab); }, [tab, load]);

  async function resolveApproval(id: string, approved: boolean) {
    if (!window.confirm(`${approved ? "Approve" : "Deny"} this connector action? This decision is audited and cannot be undone.`)) return;
    setBusy(id);
    setError(null);
    try {
      await apiFetch(`/connectors/approvals/${id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ approved, note: approved ? "approved from governance" : "denied from governance" }),
      });
      await load("approvals");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to resolve approval");
    } finally {
      setBusy(null);
    }
  }

  async function deletePolicy(id: string) {
    if (!window.confirm("Delete this connector policy? Effective access may change immediately.")) return;
    setBusy(id);
    setError(null);
    try {
      await apiFetch(`/connectors/policies/${id}`, { method: "DELETE" });
      await load("policies");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to delete policy");
    } finally {
      setBusy(null);
    }
  }

  async function registerMcp() {
    if (!mcpName.trim() || !mcpTarget.trim()) return;
    setBusy("mcp-register");
    setError(null);
    try {
      await apiFetch("/connectors/mcp/register", {
        method: "POST",
        body: JSON.stringify({
          name: mcpName.trim(),
          transport: "remote",
          server_url: mcpTarget.trim(),
        }),
      });
      setMcpName("");
      setMcpTarget("");
      await load("mcp");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to register MCP server");
    } finally {
      setBusy(null);
    }
  }

  async function createPolicy() {
    if (
      policyDecision === "allow"
      && !window.confirm("Create an allow policy? Matching connector actions may run without an approval prompt, subject to hard policy floors.")
    ) return;
    setBusy("policy-create");
    setError(null);
    try {
      await apiFetch("/connectors/policies", {
        method: "POST",
        body: JSON.stringify({
          connector_id: policyConnector.trim() || null,
          action_name: policyAction.trim() || null,
          decision: policyDecision,
        }),
      });
      setPolicyConnector("");
      setPolicyAction("");
      await load("policies");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create policy");
    } finally {
      setBusy(null);
    }
  }

  async function cancelJob(id: string) {
    if (!window.confirm("Cancel this connector job? In-flight provider work may already have started.")) return;
    setBusy(id);
    setError(null);
    try {
      await apiFetch(`/connectors/execution-jobs/${id}/cancel`, { method: "POST" });
      await load("jobs");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to cancel job");
    } finally {
      setBusy(null);
    }
  }

  async function discoverMcp(serverId: string) {
    setBusy(serverId);
    setError(null);
    try {
      await apiFetch(`/connectors/mcp/${serverId}/discover`, { method: "POST" });
      await load("mcp");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Discovery failed");
    } finally {
      setBusy(null);
    }
  }

  function handleTabKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
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
    <div className="flex flex-col min-w-0">
      <div className="mb-4 flex items-end gap-2 border-b hairline">
        <div role="tablist" aria-label="Connector governance sections" onKeyDown={handleTabKeyDown} className="no-scrollbar flex min-w-0 flex-1 gap-0.5 overflow-x-auto">
          {GOV_TABS.map(t => (
            <button
              key={t.id}
              id={`connector-governance-tab-${t.id}`}
              role="tab"
              aria-selected={tab === t.id}
              aria-controls={`connector-governance-panel-${t.id}`}
              tabIndex={tab === t.id ? 0 : -1}
              onClick={() => setTab(t.id)}
              className="flex-shrink-0 px-3.5 py-2 text-[13px] font-medium transition-colors"
              style={{
                color: tab === t.id ? "var(--text)" : "var(--text-dim)",
                borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
              }}
            >
              {t.label}
            </button>
          ))}
        </div>
        <button className="btn btn-ghost btn-sm mb-1 flex-shrink-0" onClick={() => void load(tab)} disabled={loading}>Refresh</button>
      </div>

      <div id={`connector-governance-panel-${tab}`} role="tabpanel" aria-labelledby={`connector-governance-tab-${tab}`} tabIndex={0} aria-busy={loading}>
      {error && (
        <div role="alert" className="mb-3 rounded-lg border px-3 py-2 text-[12.5px]" style={{ borderColor: "var(--danger)", background: "var(--surface-2)", color: "var(--danger)" }}>
          {error}
        </div>
      )}
      {loading && <div role="status" className="text-[13px] py-4" style={{ color: "var(--text-dim)" }}>Loading connector governance…</div>}

      {!loading && tab === "logs" && (
        <div className="surface border border-soft rounded-lg divide-y" style={{ borderColor: "var(--border-soft)" }}>
          {logs.length === 0 && <div className="px-4 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No connector executions logged yet.</div>}
          {logs.map((row, i) => (
            <div key={field(row, "id") || i} className="px-4 py-2.5 text-[12.5px] flex items-start gap-3">
              <span className="mt-1 inline-block w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: statusColor(field(row, "status")) }} />
              <div className="min-w-0 flex-1">
                <div className="font-medium truncate">{field(row, "action_name", "action", "tool") || "execution"} · {field(row, "connector_id", "provider")}</div>
                <div className="truncate" style={{ color: "var(--text-dim)" }}>{field(row, "status")} {field(row, "error") && `— ${field(row, "error")}`}</div>
                <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(row.created_at)}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {!loading && tab === "health" && (
        <div className="grid grid-cols-2 gap-3">
          {health.length === 0 && <div className="px-4 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No connector health records.</div>}
          {health.map((row, i) => (
            <div key={field(row, "connector_id", "id") || i} className="surface border border-soft rounded-lg p-3">
              <div className="flex items-center gap-2">
                <span className="inline-block w-2 h-2 rounded-full" style={{ background: statusColor(field(row, "status")) }} />
                <span className="text-[13px] font-medium truncate">{field(row, "connector_id", "provider", "id")}</span>
                <span className="tag ml-auto">{field(row, "status") || "unknown"}</span>
              </div>
              {field(row, "last_error") && <div className="mt-1 text-[11.5px] truncate" style={{ color: "var(--danger)" }}>{field(row, "last_error")}</div>}
              <div className="mt-1 text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(row.updated_at)}</div>
            </div>
          ))}
        </div>
      )}

      {!loading && tab === "approvals" && (
        <div className="space-y-2">
          {approvals.length === 0 && <div className="px-4 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No pending connector approvals.</div>}
          {approvals.map((row, i) => {
            const id = field(row, "id");
            return (
              <div key={id || i} className="surface border border-soft rounded-lg p-3">
                <div className="flex flex-col items-stretch justify-between gap-3 sm:flex-row sm:items-start">
                  <div className="min-w-0">
                    <div className="text-[13px] font-medium truncate">{field(row, "action_name", "action") || "Connector action"} · {field(row, "connector_id", "provider")}</div>
                    <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(row.created_at)}</div>
                  </div>
                  <div className="flex gap-2 flex-shrink-0">
                    <button aria-label={`Approve ${field(row, "action_name", "action") || "connector action"}`} className="btn btn-accent btn-sm" disabled={busy === id} onClick={() => void resolveApproval(id, true)}>Approve</button>
                    <button aria-label={`Deny ${field(row, "action_name", "action") || "connector action"}`} className="btn btn-danger-soft btn-sm" disabled={busy === id} onClick={() => void resolveApproval(id, false)}>Deny</button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {!loading && tab === "policies" && (
        <div className="space-y-2">
          <div className="surface border border-soft rounded-lg p-3">
            <div className="text-[13px] font-medium mb-2">New policy</div>
            <div className="flex flex-wrap gap-2">
              <input aria-label="Policy connector ID" value={policyConnector} onChange={e => setPolicyConnector(e.target.value)} placeholder="Connector id (blank = any)"
                     className="surface w-full rounded-md border border-soft px-3 py-1.5 text-[12.5px] outline-none sm:w-52" />
              <input aria-label="Policy action name" value={policyAction} onChange={e => setPolicyAction(e.target.value)} placeholder="Action name (blank = any)"
                     className="surface w-full rounded-md border border-soft px-3 py-1.5 text-[12.5px] outline-none sm:w-52" />
              <select aria-label="Policy decision" value={policyDecision} onChange={e => setPolicyDecision(e.target.value as typeof policyDecision)}
                      className="surface border border-soft rounded-md px-3 py-1.5 text-[12.5px] outline-none">
                <option value="allow">allow</option>
                <option value="require_approval">require_approval</option>
                <option value="deny">deny</option>
              </select>
              <button className="btn btn-accent btn-sm" disabled={busy === "policy-create"} onClick={() => void createPolicy()}>Create</button>
            </div>
          </div>
          {policies.length === 0 && <div className="px-4 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No connector policies configured. Default governance applies.</div>}
          {policies.map((row, i) => {
            const id = field(row, "id");
            return (
              <div key={id || i} className="surface flex flex-col items-stretch justify-between gap-3 rounded-lg border border-soft p-3 sm:flex-row sm:items-start">
                <div className="min-w-0">
                  <div className="text-[13px] font-medium">
                    {field(row, "decision") || "policy"} · {field(row, "connector_id") || "any connector"} · {field(row, "action_name") || "any action"}
                  </div>
                  <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>
                    {field(row, "role") && `role: ${field(row, "role")} · `}
                    {field(row, "risk_level") && `risk: ${field(row, "risk_level")} · `}
                    mode: {field(row, "approval_mode") || "single"} · priority {field(row, "priority") || "0"}
                  </div>
                </div>
                <button className="btn btn-ghost btn-sm flex-shrink-0" disabled={busy === id} onClick={() => void deletePolicy(id)}>Delete</button>
              </div>
            );
          })}
        </div>
      )}

      {!loading && tab === "traces" && (
        <div className="surface border border-soft rounded-lg divide-y" style={{ borderColor: "var(--border-soft)" }}>
          {traces.length === 0 && <div className="px-4 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No execution traces recorded yet.</div>}
          {traces.map((row, i) => (
            <div key={field(row, "id") || i} className="px-4 py-2.5 text-[12.5px] flex items-start gap-3">
              <span className="mt-1 inline-block w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: statusColor(field(row, "status")) }} />
              <div className="min-w-0 flex-1">
                <div className="font-medium truncate">{field(row, "action_name", "action") || "trace"} · {field(row, "connector_id")}</div>
                <div className="truncate" style={{ color: "var(--text-dim)" }}>{field(row, "status")} · {field(row, "duration_ms") && `${field(row, "duration_ms")}ms`}</div>
                <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(row.started_at)}</div>
              </div>
            </div>
          ))}
        </div>
      )}

      {!loading && tab === "jobs" && (
        <div className="space-y-2">
          {jobs.length === 0 && <div className="px-4 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No queued connector jobs.</div>}
          {jobs.map((row, i) => {
            const id = field(row, "id");
            const status = field(row, "status");
            return (
              <div key={id || i} className="surface border border-soft rounded-lg p-3 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[13px] font-medium truncate">{field(row, "action_name", "action") || "job"} · {field(row, "connector_id")}</div>
                  <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{status} · attempts {field(row, "attempts") || "0"}</div>
                  <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(row.created_at)}</div>
                </div>
                {["queued", "pending", "running"].includes(status) && (
                  <button className="btn btn-ghost btn-sm flex-shrink-0" disabled={busy === id} onClick={() => void cancelJob(id)}>Cancel</button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {!loading && tab === "mcp" && (
        <div className="space-y-4">
          <div className="surface border border-soft rounded-lg p-3">
            <div className="text-[13px] font-medium mb-2">Register MCP server</div>
            <div className="flex flex-wrap gap-2">
              <input aria-label="MCP server name" value={mcpName} onChange={e => setMcpName(e.target.value)} placeholder="Name"
                     className="surface w-full rounded-md border border-soft px-3 py-1.5 text-[12.5px] outline-none sm:w-40" />
              <input aria-label="Remote MCP server URL" value={mcpTarget} onChange={e => setMcpTarget(e.target.value)}
                     type="url" inputMode="url" autoCapitalize="none" autoCorrect="off"
                     placeholder="https://mcp.example.com/sse"
                     className="surface min-w-0 flex-1 rounded-md border border-soft px-3 py-1.5 text-[12.5px] outline-none sm:min-w-[200px]" />
              <button className="btn btn-accent btn-sm" disabled={busy === "mcp-register" || !mcpName.trim() || !mcpTarget.trim()} onClick={() => void registerMcp()}>Register</button>
            </div>
          </div>

          <div className="space-y-2">
            {mcp.servers.length === 0 && <div className="px-4 py-4 text-[13px]" style={{ color: "var(--text-dim)" }}>No MCP servers registered.</div>}
            {mcp.servers.map((row, i) => {
              const id = field(row, "id");
              return (
                <div key={id || i} className="surface flex flex-col items-stretch justify-between gap-3 rounded-lg border border-soft p-3 sm:flex-row sm:items-start">
                  <div className="min-w-0">
                    <div className="text-[13px] font-medium truncate">{field(row, "name")} <span className="tag ml-1">{field(row, "transport")}</span></div>
                    <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{field(row, "server_url", "command")}</div>
                    <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{field(row, "status")} · {labelTime(row.updated_at || row.created_at)}</div>
                  </div>
                  <button className="btn btn-ghost btn-sm flex-shrink-0" disabled={busy === id} onClick={() => void discoverMcp(id)}>Discover tools</button>
                </div>
              );
            })}
          </div>
        </div>
      )}
      </div>
    </div>
  );
}
