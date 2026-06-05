"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";

type ComputerSession = {
  id: string;
  status: string;
  purpose?: string | null;
  task_id?: string | null;
  workspace_path?: string | null;
  network_policy?: Record<string, unknown>;
  resource_limits?: Record<string, unknown>;
  history?: Array<{ event_type?: string; payload?: Record<string, unknown>; created_at?: string }>;
  updated_at?: string;
  created_at?: string;
};

type ComputerEvent = {
  id?: string;
  seq?: number;
  event_type?: string;
  payload?: Record<string, unknown>;
  created_at?: string;
};

type LocalGrant = {
  id: string;
  status: string;
  folder_path: string;
  purpose?: string | null;
  task_id?: string | null;
  updated_at?: string;
  created_at?: string;
  revoked_at?: string | null;
};

function labelTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function statusColor(status?: string | null) {
  if (status === "active") return "var(--accent)";
  if (status === "revoked" || status === "failed") return "var(--danger)";
  if (status === "degraded") return "var(--warn)";
  return "var(--text-faint)";
}

export default function ComputerScreen() {
  const [sessions, setSessions] = useState<ComputerSession[]>([]);
  const [events, setEvents] = useState<ComputerEvent[]>([]);
  const [grants, setGrants] = useState<LocalGrant[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [folderPath, setFolderPath] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const active = useMemo(
    () => sessions.find(session => session.id === activeId) ?? sessions[0] ?? null,
    [activeId, sessions],
  );
  const replay = events.length ? events : (active?.history || []).map((event, index) => ({ ...event, id: `history-${index}` }));

  const loadSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = (await (await apiFetch("/computer-sessions/")).json()) as ComputerSession[];
      setSessions(data);
      setActiveId(current => current && data.some(session => session.id === current) ? current : data[0]?.id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load computer sessions");
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadGrants = useCallback(async () => {
    try {
      const data = (await (await apiFetch("/computer-sessions/local-grants")).json()) as LocalGrant[];
      setGrants(data);
    } catch {
      setGrants([]);
    }
  }, []);

  const loadEvents = useCallback(async (sessionId: string | null) => {
    if (!sessionId) {
      setEvents([]);
      return;
    }
    try {
      const data = (await (await apiFetch(`/computer-sessions/${sessionId}/events`)).json()) as ComputerEvent[];
      setEvents(data);
    } catch {
      setEvents([]);
    }
  }, []);

  useEffect(() => { void loadSessions(); void loadGrants(); }, [loadGrants, loadSessions]);
  useEffect(() => { void loadEvents(active?.id ?? null); }, [active?.id, loadEvents]);

  async function createSession() {
    setBusy(true);
    setError(null);
    try {
      const created = (await (await apiFetch("/computer-sessions/", {
        method: "POST",
        body: JSON.stringify({ purpose: "Manual cloud computer workspace" }),
      })).json()) as ComputerSession;
      await loadSessions();
      setActiveId(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create computer session");
    } finally {
      setBusy(false);
    }
  }

  async function grantLocalFolder() {
    if (!folderPath.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await apiFetch("/computer-sessions/local-grants", {
        method: "POST",
        body: JSON.stringify({ folder_path: folderPath.trim(), purpose: "Manual local computer bridge" }),
      });
      setFolderPath("");
      await loadGrants();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to grant local folder");
    } finally {
      setBusy(false);
    }
  }

  async function revokeGrant(grantId: string) {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/computer-sessions/local-grants/${grantId}/revoke`, {
        method: "POST",
        body: JSON.stringify({ reason: "revoked from computer screen" }),
      });
      await loadGrants();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to revoke local grant");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 min-w-0 overflow-hidden flex flex-col">
      <header className="px-10 pt-9 pb-5 flex items-start justify-between gap-6 flex-shrink-0">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Computer</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Cloud workspaces, sandboxed commands, artifact exports, and authorized local bridge grants.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => { void loadSessions(); void loadGrants(); void loadEvents(active?.id ?? null); }} disabled={busy}>Refresh</button>
          <button className="btn btn-accent btn-sm" onClick={createSession} disabled={busy}>New cloud computer</button>
        </div>
      </header>

      {error && (
        <div className="mx-10 mb-3 rounded-lg border px-3 py-2 text-[12.5px]" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}>
          {error}
        </div>
      )}

      <div className="flex-1 min-h-0 px-10 pb-10 grid gap-4" style={{ gridTemplateColumns: "320px minmax(0, 1fr)" }}>
        <aside className="surface border border-soft rounded-lg overflow-hidden min-h-0 flex flex-col">
          <div className="px-3 py-2 border-b hairline flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Cloud computers</span>
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{sessions.length}</span>
          </div>
          <div className="overflow-y-auto p-2 space-y-2">
            {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading...</div>}
            {!loading && sessions.length === 0 && (
              <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No cloud computer sessions yet.</div>
            )}
            {sessions.map(session => (
              <button
                key={session.id}
                className="w-full rounded-md border border-soft p-3 text-left smooth"
                onClick={() => setActiveId(session.id)}
                style={{ background: active?.id === session.id ? "var(--surface-2)" : "transparent" }}
              >
                <div className="flex items-center gap-2">
                  <span className="inline-block w-2 h-2 rounded-full" style={{ background: statusColor(session.status) }} />
                  <span className="text-[13px] font-medium truncate">{session.purpose || "Cloud computer"}</span>
                </div>
                <div className="mt-1 text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{session.workspace_path || "Workspace pending"}</div>
                <div className="mt-2 text-[11px]" style={{ color: "var(--text-faint)" }}>{session.status} · {labelTime(session.updated_at || session.created_at)}</div>
              </button>
            ))}
          </div>
        </aside>

        <section className="surface border border-soft rounded-lg min-w-0 min-h-0 overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b hairline flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[14px] font-medium truncate">{active?.purpose || "Cloud computer"}</div>
              <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>{active?.workspace_path || "Select or create a cloud computer session"}</div>
            </div>
            {active && <span className="tag">{active.status}</span>}
          </div>

          <div className="flex-1 min-h-0 grid" style={{ gridTemplateColumns: "minmax(0, 1fr) 340px" }}>
            <div className="min-w-0 min-h-0 p-4 overflow-auto">
              <div className="rounded-lg border border-soft overflow-hidden bg-black" style={{ aspectRatio: "16 / 10" }} data-testid="computer-viewport">
                <div className="w-full h-full flex flex-col justify-center px-6 text-[13px]" style={{ color: "rgba(255,255,255,.76)" }}>
                  <div className="text-[15px] font-medium text-white">Cloud computer workspace</div>
                  <div className="mt-2 font-mono text-[12px] break-all">{active?.workspace_path || "No active workspace"}</div>
                  <div className="mt-4 grid gap-2 sm:grid-cols-2">
                    <div className="rounded-md border px-3 py-2" style={{ borderColor: "rgba(255,255,255,.16)" }}>Terminal: audited</div>
                    <div className="rounded-md border px-3 py-2" style={{ borderColor: "rgba(255,255,255,.16)" }}>Filesystem: jailed</div>
                    <div className="rounded-md border px-3 py-2" style={{ borderColor: "rgba(255,255,255,.16)" }}>Network: restricted</div>
                    <div className="rounded-md border px-3 py-2" style={{ borderColor: "rgba(255,255,255,.16)" }}>Export: artifacts</div>
                  </div>
                </div>
              </div>

              <div className="mt-4 rounded-lg border border-soft overflow-hidden">
                <div className="px-3 py-2 border-b hairline text-[12.5px] font-medium">Replay</div>
                <div className="divide-y" style={{ borderColor: "var(--border-soft)" }}>
                  {replay.length === 0 && <div className="px-3 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No computer events yet.</div>}
                  {replay.map((event, index) => {
                    const payload = event.payload || {};
                    const type = event.event_type || String(payload.type || "computer_event");
                    return (
                      <div key={event.id || index} className="px-3 py-2.5 text-[12.5px] flex items-start gap-3" data-testid="computer-event-row">
                        <span className="mt-1 inline-block w-1.5 h-1.5 rounded-full" style={{ background: statusColor(String(payload.status || "active")) }} />
                        <div className="min-w-0">
                          <div className="font-medium">{type.replaceAll("_", " ")}</div>
                          {"command" in payload && <div className="truncate font-mono" style={{ color: "var(--text-dim)" }}>{String(payload.command)}</div>}
                          {"artifact_id" in payload && <div className="truncate" style={{ color: "var(--text-dim)" }}>artifact {String(payload.artifact_id)}</div>}
                          <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(event.created_at)}</div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            </div>

            <aside className="border-l hairline min-h-0 overflow-y-auto p-4 space-y-4">
              <div>
                <div className="text-[13px] font-medium">Sandbox policy</div>
                <div className="mt-2 rounded-lg border border-soft p-3 text-[12.5px] space-y-1" style={{ color: "var(--text-dim)" }}>
                  <div>Timeout: {String(active?.resource_limits?.timeout_seconds ?? 30)} seconds</div>
                  <div>Output cap: {String(active?.resource_limits?.output_bytes ?? 131072)} bytes</div>
                  <div>Network: {String(active?.network_policy?.mode ?? "restricted")}</div>
                </div>
              </div>

              <div>
                <div className="text-[13px] font-medium">Local bridge</div>
                <div className="mt-2 flex gap-2">
                  <input
                    value={folderPath}
                    onChange={event => setFolderPath(event.target.value)}
                    placeholder="/Users/name/project"
                    className="flex-1 surface border border-soft rounded-md px-3 py-1.5 text-[12.5px] outline-none min-w-0"
                  />
                  <button className="btn btn-accent btn-sm" disabled={busy || !folderPath.trim()} onClick={() => void grantLocalFolder()}>Grant</button>
                </div>
                <div className="mt-3 space-y-2">
                  {grants.length === 0 && <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>No local folder grants.</div>}
                  {grants.map(grant => (
                    <div key={grant.id} className="rounded-lg border border-soft p-3" data-testid="local-grant-row">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[12.5px] font-medium truncate">{grant.purpose || "Local grant"}</span>
                        <span className="tag">{grant.status}</span>
                      </div>
                      <div className="mt-1 text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{grant.folder_path}</div>
                      <div className="mt-3 flex justify-end">
                        <button className="btn btn-ghost btn-sm" disabled={busy || grant.status !== "active"} onClick={() => void revokeGrant(grant.id)}>Revoke</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </aside>
          </div>
        </section>
      </div>
    </div>
  );
}
