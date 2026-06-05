"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";

type BrowserSession = {
  id: string;
  status: string;
  task_id?: string | null;
  member_id?: string | null;
  current_url?: string | null;
  title?: string | null;
  screenshot_data_url?: string | null;
  screenshot_object_path?: string | null;
  takeover_state?: string | null;
  takeover_reason?: string | null;
  takeover_summary?: string | null;
  consent?: { purpose?: string; allowed_domains?: string[]; [key: string]: unknown };
  sensitive_site_approvals?: Array<{ domain?: string; approval_id?: string | null; approved_at?: string }>;
  downloads?: Array<{ filename?: string; path?: string; created_at?: string }>;
  history?: Array<{ action?: string; payload?: Record<string, unknown>; created_at?: string }>;
  updated_at?: string;
  created_at?: string;
  revoked_at?: string | null;
};

type BrowserEvent = {
  id?: string;
  seq?: number;
  event_type?: string;
  payload?: Record<string, unknown>;
  url?: string | null;
  screenshot_ref?: string | null;
  created_at?: string;
  action?: string;
};

function labelTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function statusTone(status?: string | null) {
  if (status === "active") return "var(--accent)";
  if (status === "revoked") return "var(--danger)";
  if (status === "closed") return "var(--text-faint)";
  if (status === "degraded") return "var(--warn)";
  return "var(--text-muted)";
}

export default function BrowserOperatorScreen() {
  const [sessions, setSessions] = useState<BrowserSession[]>([]);
  const [events, setEvents] = useState<BrowserEvent[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [handBackSummary, setHandBackSummary] = useState("");

  const active = useMemo(
    () => sessions.find(session => session.id === activeId) ?? sessions[0] ?? null,
    [activeId, sessions],
  );
  const replayEvents = useMemo<BrowserEvent[]>(() => {
    if (events.length) return events;
    return (active?.history || []).map((item, index) => ({
      id: `history-${index}`,
      event_type: item.action,
      payload: item.payload,
      created_at: item.created_at,
    }));
  }, [active?.history, events]);

  const loadSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = (await (await apiFetch("/browser-sessions/")).json()) as BrowserSession[];
      setSessions(data);
      setActiveId(current => current && data.some(session => session.id === current) ? current : data[0]?.id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load browser sessions");
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
      const data = (await (await apiFetch(`/browser-sessions/${sessionId}/events`)).json()) as BrowserEvent[];
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
      const created = (await (await apiFetch("/browser-sessions/", {
        method: "POST",
        body: JSON.stringify({ consent: { purpose: "Manual browser operation", allowed_domains: [] } }),
      })).json()) as BrowserSession;
      await loadSessions();
      setActiveId(created.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create browser session");
    } finally {
      setBusy(false);
    }
  }

  async function postAction(path: string, body?: Record<string, unknown>) {
    if (!active) return;
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/browser-sessions/${active.id}/${path}`, {
        method: "POST",
        body: body ? JSON.stringify(body) : undefined,
      });
      await loadSessions();
      await loadEvents(active.id);
      setHandBackSummary("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Browser action failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 min-w-0 overflow-hidden flex flex-col">
      <header className="px-10 pt-9 pb-5 flex items-start justify-between gap-6 flex-shrink-0">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Browser</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Persistent operator sessions, takeover, downloads, and replay.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => { void loadSessions(); void loadEvents(active?.id ?? null); }} disabled={busy}>Refresh</button>
          <button className="btn btn-accent btn-sm" onClick={createSession} disabled={busy}>New session</button>
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
            <span className="text-[12.5px] font-medium">Sessions</span>
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{sessions.length}</span>
          </div>
          <div className="overflow-y-auto p-2 space-y-2">
            {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading...</div>}
            {!loading && sessions.length === 0 && (
              <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No browser sessions yet.</div>
            )}
            {sessions.map(session => (
              <button
                key={session.id}
                className="w-full rounded-md border border-soft p-3 text-left smooth"
                onClick={() => setActiveId(session.id)}
                style={{ background: active?.id === session.id ? "var(--surface-2)" : "transparent" }}
              >
                <div className="flex items-center gap-2">
                  <span className="inline-block w-2 h-2 rounded-full" style={{ background: statusTone(session.status) }} />
                  <span className="text-[13px] font-medium truncate">{session.title || session.current_url || "Browser session"}</span>
                </div>
                <div className="mt-1 text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{session.current_url || "No page loaded"}</div>
                <div className="mt-2 flex items-center gap-1 flex-wrap text-[11px]" style={{ color: "var(--text-faint)" }}>
                  <span>{session.status}</span>
                  {session.takeover_state && session.takeover_state !== "none" && <span>· {session.takeover_state}</span>}
                  {session.task_id && <span>· task {session.task_id.slice(0, 8)}</span>}
                </div>
              </button>
            ))}
          </div>
        </aside>

        <section className="surface border border-soft rounded-lg min-w-0 min-h-0 overflow-hidden flex flex-col">
          {!active ? (
            <div className="flex-1 flex items-center justify-center text-[13px]" style={{ color: "var(--text-dim)" }}>Select or create a browser session.</div>
          ) : (
            <>
              <div className="px-4 py-3 border-b hairline flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[14px] font-medium truncate">{active.title || "Browser session"}</div>
                  <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>{active.current_url || "No URL"}</div>
                </div>
                <div className="flex items-center gap-2">
                  <button className="btn btn-ghost btn-sm" disabled={busy || !active} onClick={() => void postAction("close")}>Close</button>
                  <button className="btn btn-ghost btn-sm" disabled={busy || !active} onClick={() => void postAction("revoke", { reason: "revoked from browser view" })}>Revoke</button>
                </div>
              </div>

              <div className="flex-1 min-h-0 grid" style={{ gridTemplateColumns: "minmax(0, 1fr) 300px" }}>
                <div className="min-w-0 min-h-0 p-4 overflow-auto">
                  <div className="rounded-lg border border-soft overflow-hidden bg-black" style={{ aspectRatio: "16 / 10" }} data-testid="browser-viewport">
                    {active.screenshot_data_url ? (
                      // eslint-disable-next-line @next/next/no-img-element
                      <img src={active.screenshot_data_url} alt="Current browser screenshot" className="w-full h-full object-contain" />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-[13px]" style={{ color: "rgba(255,255,255,.72)" }}>
                        {active.status === "degraded" ? "Browser runtime unavailable" : "No screenshot captured"}
                      </div>
                    )}
                  </div>

                  {active.takeover_state === "requested" && (
                    <div className="mt-4 rounded-lg border px-3 py-3" style={{ borderColor: "var(--warn)", background: "var(--warn-soft)" }}>
                      <div className="text-[13px] font-medium" style={{ color: "var(--warn)" }}>Takeover requested</div>
                      <div className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>{active.takeover_reason || "User input required"}</div>
                      <div className="mt-3 flex gap-2">
                        <input
                          value={handBackSummary}
                          onChange={event => setHandBackSummary(event.target.value)}
                          placeholder="Hand-back summary"
                          className="flex-1 surface border border-soft rounded-md px-3 py-1.5 text-[12.5px] outline-none"
                        />
                        <button className="btn btn-accent btn-sm" disabled={busy} onClick={() => void postAction("hand-back", { summary: handBackSummary || "User completed takeover" })}>Hand back</button>
                      </div>
                    </div>
                  )}

                  <div className="mt-4 rounded-lg border border-soft overflow-hidden">
                    <div className="px-3 py-2 border-b hairline text-[12.5px] font-medium">Replay</div>
                    <div className="divide-y" style={{ borderColor: "var(--border-soft)" }}>
                      {replayEvents.map((event, index) => {
                        const payload = event.payload || {};
                        const type = event.event_type || event.action || String(payload.type || "browser_event");
                        const url = event.url || String(payload.current_url || "");
                        return (
                          <div key={event.id || index} className="px-3 py-2.5 text-[12.5px] flex items-start gap-3" data-testid="browser-event-row">
                            <span className="mt-1 inline-block w-1.5 h-1.5 rounded-full" style={{ background: type === "browser_session_revoked" ? "var(--danger)" : "var(--accent)" }} />
                            <div className="min-w-0">
                              <div className="font-medium">{type.replaceAll("_", " ")}</div>
                              {url && <div className="truncate" style={{ color: "var(--text-dim)" }}>{url}</div>}
                              <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{labelTime(event.created_at)}</div>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                </div>

                <aside className="border-l hairline p-4 overflow-y-auto space-y-4">
                  <div>
                    <div className="text-[12px] font-medium mb-2">State</div>
                    <div className="space-y-1.5 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
                      <div>Status: <span style={{ color: statusTone(active.status) }}>{active.status}</span></div>
                      <div>Takeover: {active.takeover_state || "none"}</div>
                      <div>Updated: {labelTime(active.updated_at)}</div>
                    </div>
                  </div>

                  <div>
                    <div className="text-[12px] font-medium mb-2">Consent</div>
                    <div className="rounded-md border border-soft p-2 text-[12px]" style={{ color: "var(--text-dim)" }}>
                      <div>{active.consent?.purpose || "No purpose recorded"}</div>
                      <div className="mt-1">{(active.consent?.allowed_domains || []).join(", ") || "No domain limit"}</div>
                    </div>
                  </div>

                  <div>
                    <div className="text-[12px] font-medium mb-2">Sensitive Approvals</div>
                    <div className="space-y-1.5">
                      {(active.sensitive_site_approvals || []).length === 0 && <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>None</div>}
                      {(active.sensitive_site_approvals || []).map((approval, index) => (
                        <div key={`${approval.domain}-${index}`} className="rounded-md border border-soft px-2 py-1.5 text-[12px]">{approval.domain}</div>
                      ))}
                    </div>
                  </div>

                  <div>
                    <div className="text-[12px] font-medium mb-2">Downloads</div>
                    <div className="space-y-1.5">
                      {(active.downloads || []).length === 0 && <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>No downloads</div>}
                      {(active.downloads || []).map((download, index) => (
                        <div key={`${download.filename}-${index}`} className="rounded-md border border-soft px-2 py-1.5 text-[12px] truncate">{download.filename || download.path || "download"}</div>
                      ))}
                    </div>
                  </div>
                </aside>
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}
