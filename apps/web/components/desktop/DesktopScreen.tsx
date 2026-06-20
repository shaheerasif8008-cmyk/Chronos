"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";

type DesktopSession = {
  id: string;
  status: string;
  purpose?: string | null;
  task_id?: string | null;
  history?: Array<{ event_type?: string; payload?: Record<string, unknown>; created_at?: string }>;
  updated_at?: string;
  created_at?: string;
};

type DesktopEvent = {
  id?: string;
  seq?: number;
  event_type?: string;
  payload?: Record<string, unknown>;
  created_at?: string;
};

function labelTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function statusColor(status?: string | null) {
  if (status === "active" || status === "running") return "var(--accent)";
  if (status === "revoked" || status === "failed") return "var(--danger)";
  if (status === "degraded") return "var(--warn)";
  if (status === "closed" || status === "completed") return "var(--text-faint)";
  return "var(--text-faint)";
}

export default function DesktopScreen() {
  const [sessions, setSessions] = useState<DesktopSession[]>([]);
  const [events, setEvents] = useState<DesktopEvent[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
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
      const data = (await (await apiFetch("/desktop-sessions/")).json()) as DesktopSession[];
      setSessions(data);
      setActiveId(current => current && data.some(session => session.id === current) ? current : data[0]?.id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load desktop sessions");
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEvents = useCallback(async (sessionId: string | null) => {
    if (!sessionId) {
      setEvents([]);
      return;
    }
    try {
      const data = (await (await apiFetch(`/desktop-sessions/${sessionId}/events`)).json()) as DesktopEvent[];
      setEvents(data);
    } catch {
      setEvents([]);
    }
  }, []);

  useEffect(() => { void loadSessions(); }, [loadSessions]);
  useEffect(() => { void loadEvents(active?.id ?? null); }, [active?.id, loadEvents]);

  async function createSession() {
    setBusy(true);
    setError(null);
    try {
      const created = (await (await apiFetch("/desktop-sessions/", {
        method: "POST",
        body: JSON.stringify({ purpose: "Manual desktop operation session" }),
      })).json()) as DesktopSession;
      await loadSessions();
      setActiveId(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create desktop session");
    } finally {
      setBusy(false);
    }
  }

  async function sessionAction(sessionId: string, action: "revoke" | "close") {
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/desktop-sessions/${sessionId}/${action}`, {
        method: "POST",
        body: action === "revoke" ? JSON.stringify({ reason: "revoked from desktop screen" }) : undefined,
      });
      await loadSessions();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Unable to ${action} desktop session`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 min-w-0 overflow-hidden flex flex-col">
      <header className="px-10 pt-9 pb-5 flex items-start justify-between gap-6 flex-shrink-0">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Desktop</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Governed desktop operation sessions — consent-gated, fully replayed, and revocable at any time.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => { void loadSessions(); void loadEvents(active?.id ?? null); }} disabled={busy}>Refresh</button>
          <button className="btn btn-accent btn-sm" onClick={createSession} disabled={busy}>New desktop session</button>
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
            <span className="text-[12.5px] font-medium">Desktop sessions</span>
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{sessions.length}</span>
          </div>
          <div className="overflow-y-auto p-2 space-y-2">
            {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading...</div>}
            {!loading && sessions.length === 0 && (
              <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No desktop sessions yet.</div>
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
                  <span className="text-[13px] font-medium truncate">{session.purpose || "Desktop session"}</span>
                </div>
                <div className="mt-2 text-[11px]" style={{ color: "var(--text-faint)" }}>{session.status} · {labelTime(session.updated_at || session.created_at)}</div>
              </button>
            ))}
          </div>
        </aside>

        <section className="surface border border-soft rounded-lg min-w-0 min-h-0 overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b hairline flex items-center justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[14px] font-medium truncate">{active?.purpose || "Desktop session"}</div>
              <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>
                {active ? `Session ${active.id}` : "Select or create a desktop session"}
              </div>
            </div>
            {active && (
              <div className="flex items-center gap-2 flex-shrink-0">
                <span className="tag">{active.status}</span>
                <button className="btn btn-ghost btn-sm" disabled={busy || active.status !== "active"} onClick={() => void sessionAction(active.id, "revoke")}>Revoke</button>
                <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void sessionAction(active.id, "close")}>Close</button>
              </div>
            )}
          </div>

          <div className="flex-1 min-h-0 p-4 overflow-auto">
            <div className="rounded-lg border border-soft overflow-hidden">
              <div className="px-3 py-2 border-b hairline text-[12.5px] font-medium">Replay</div>
              <div className="divide-y" style={{ borderColor: "var(--border-soft)" }}>
                {replay.length === 0 && <div className="px-3 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No desktop events yet.</div>}
                {replay.map((event, index) => {
                  const payload = event.payload || {};
                  const type = event.event_type || String(payload.type || "desktop_event");
                  return (
                    <div key={event.id || index} className="px-3 py-2.5 text-[12.5px] flex items-start gap-3" data-testid="desktop-event-row">
                      <span className="mt-1 inline-block w-1.5 h-1.5 rounded-full" style={{ background: statusColor(String(payload.status || "active")) }} />
                      <div className="min-w-0">
                        <div className="font-medium">{type.replaceAll("_", " ")}</div>
                        {"action" in payload && <div className="truncate font-mono" style={{ color: "var(--text-dim)" }}>{String(payload.action)}</div>}
                        {"target" in payload && <div className="truncate" style={{ color: "var(--text-dim)" }}>{String(payload.target)}</div>}
                        <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(event.created_at)}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
