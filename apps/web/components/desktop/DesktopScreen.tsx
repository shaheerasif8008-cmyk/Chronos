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
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [purpose, setPurpose] = useState("");
  const [resourceScope, setResourceScope] = useState("");
  const [expiryMinutes, setExpiryMinutes] = useState("60");
  const [consentConfirmed, setConsentConfirmed] = useState(false);

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
    const allowedResources = resourceScope.split(/[,\n]+/).map(value => value.trim()).filter(Boolean);
    if (!purpose.trim() || allowedResources.length === 0 || !consentConfirmed) {
      setError("Describe the purpose, list the allowed apps or resources, and confirm consent.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = (await (await apiFetch("/desktop-sessions/", {
        method: "POST",
        body: JSON.stringify({
          purpose: purpose.trim(),
          consent: {
            purpose: purpose.trim(),
            allowed_resources: Array.from(new Set(allowedResources)),
            expires_at: new Date(Date.now() + Number(expiryMinutes) * 60_000).toISOString(),
            confirmed_by_user: true,
          },
        }),
      })).json()) as DesktopSession;
      await loadSessions();
      setActiveId(created.id);
      setNewSessionOpen(false);
      setPurpose("");
      setResourceScope("");
      setConsentConfirmed(false);
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
      <header className="flex flex-shrink-0 flex-col items-start justify-between gap-4 px-4 pb-4 pt-5 sm:flex-row sm:gap-6 md:px-10 md:pb-5 md:pt-9">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Desktop</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Governed desktop operation sessions — consent-gated, fully replayed, and revocable at any time.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => { void loadSessions(); void loadEvents(active?.id ?? null); }} disabled={busy}>Refresh</button>
          <button className="btn btn-accent btn-sm" onClick={() => setNewSessionOpen(open => !open)} disabled={busy}>{newSessionOpen ? "Cancel" : "New desktop session"}</button>
        </div>
      </header>

      {newSessionOpen && (
        <section className="mx-4 mb-4 rounded-xl border border-soft p-4 md:mx-10" style={{ background: "var(--surface)" }} aria-label="Desktop session consent">
          <h2 className="text-[14px] font-semibold">Review desktop access</h2>
          <p className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>The session is revocable and replayed. Limit it to the apps and resources needed for this task.</p>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <label className="grid gap-1 text-[12px]">Purpose
              <input className="input-field w-full" value={purpose} onChange={event => setPurpose(event.target.value)} placeholder="Update the approved presentation" />
            </label>
            <label className="grid gap-1 text-[12px]">Allowed apps or resources
              <input className="input-field w-full" value={resourceScope} onChange={event => setResourceScope(event.target.value)} placeholder="Keynote, Client Q3 deck" />
            </label>
            <label className="grid gap-1 text-[12px]">Session expires
              <select className="input-field w-full" value={expiryMinutes} onChange={event => setExpiryMinutes(event.target.value)}>
                <option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">1 hour</option><option value="120">2 hours</option>
              </select>
            </label>
            <label className="flex items-start gap-2 rounded-lg border border-soft p-3 text-[12.5px]">
              <input type="checkbox" checked={consentConfirmed} onChange={event => setConsentConfirmed(event.target.checked)} className="mt-0.5" />
              <span>I authorize only the stated purpose and resources for this time window.</span>
            </label>
          </div>
          <div className="mt-4 flex justify-end"><button className="btn btn-accent btn-sm" onClick={() => void createSession()} disabled={busy || !consentConfirmed}>{busy ? "Creating…" : "Create governed desktop"}</button></div>
        </section>
      )}

      {error && (
        <div className="mx-4 mb-3 rounded-lg border px-3 py-2 text-[12.5px] md:mx-10" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}>
          {error}
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto px-4 pb-6 md:grid-cols-[320px_minmax(0,1fr)] md:overflow-hidden md:px-10 md:pb-10">
        <aside className="surface border border-soft rounded-lg overflow-hidden min-h-0 flex max-h-[260px] flex-col md:max-h-none">
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
          <div className="flex flex-col items-stretch justify-between gap-3 border-b hairline px-4 py-3 sm:flex-row sm:items-center">
            <div className="min-w-0">
              <div className="text-[14px] font-medium truncate">{active?.purpose || "Desktop session"}</div>
              <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>
                {active ? `Session ${active.id}` : "Select or create a desktop session"}
              </div>
            </div>
            {active && (
              <div className="flex flex-wrap items-center gap-2 flex-shrink-0">
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
