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
  screenshot_url?: string | null;
  screenshot_object_path?: string | null;
  takeover_state?: string | null;
  takeover_reason?: string | null;
  takeover_summary?: string | null;
  consent?: { purpose?: string; allowed_domains?: string[]; [key: string]: unknown };
  sensitive_site_approvals?: Array<{ domain?: string; approval_id?: string | null; approved_at?: string }>;
  downloads?: Array<{ filename?: string; created_at?: string; content_type?: string; size_bytes?: number; download_url?: string }>;
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
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [purpose, setPurpose] = useState("");
  const [domainScope, setDomainScope] = useState("");
  const [expiryMinutes, setExpiryMinutes] = useState("60");
  const [consentConfirmed, setConsentConfirmed] = useState(false);
  const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null);

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
  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    const durableUrl = active?.screenshot_url;
    if (!durableUrl) {
      setScreenshotUrl(active?.screenshot_data_url || null);
      return () => undefined;
    }
    setScreenshotUrl(null);
    apiFetch(durableUrl)
      .then(response => response.blob())
      .then(blob => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setScreenshotUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setScreenshotUrl(null);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [active?.id, active?.screenshot_data_url, active?.screenshot_url]);

  async function createSession() {
    const allowedDomains = domainScope
      .split(/[\s,]+/)
      .map(value => value.trim().toLowerCase().replace(/^https?:\/\//, "").split("/")[0])
      .filter(Boolean);
    if (!purpose.trim() || allowedDomains.length === 0 || !consentConfirmed) {
      setError("Describe the purpose, allow at least one domain, and confirm the consent scope.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = (await (await apiFetch("/browser-sessions/", {
        method: "POST",
        body: JSON.stringify({
          consent: {
            purpose: purpose.trim(),
            allowed_domains: Array.from(new Set(allowedDomains)),
            expires_at: new Date(Date.now() + Number(expiryMinutes) * 60_000).toISOString(),
            confirmed_by_user: true,
          },
        }),
      })).json()) as BrowserSession;
      await loadSessions();
      setActiveId(created.id);
      setNewSessionOpen(false);
      setPurpose("");
      setDomainScope("");
      setConsentConfirmed(false);
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

  async function openLiveView() {
    if (!active || active.takeover_state !== "requested") return;
    const popup = window.open("about:blank", "_blank", "noopener,noreferrer");
    setBusy(true);
    setError(null);
    try {
      const payload = await apiFetch(`/browser-sessions/${active.id}/live-view`).then(response => response.json()) as { live_view_url?: string };
      if (!payload.live_view_url) throw new Error("Live view URL was not returned");
      if (popup) popup.location.replace(payload.live_view_url);
      else window.open(payload.live_view_url, "_blank", "noopener,noreferrer");
    } catch (err) {
      popup?.close();
      setError(err instanceof Error ? err.message : "Unable to open live view");
    } finally {
      setBusy(false);
    }
  }

  async function downloadFile(download: NonNullable<BrowserSession["downloads"]>[number]) {
    if (!download.download_url) return;
    setBusy(true);
    setError(null);
    try {
      const blob = await apiFetch(download.download_url).then(response => response.blob());
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = download.filename || "download";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to download file");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 min-w-0 overflow-hidden flex flex-col">
      <header className="flex flex-shrink-0 flex-col items-start justify-between gap-4 px-4 pb-4 pt-5 sm:flex-row sm:gap-6 md:px-10 md:pb-5 md:pt-9">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Browser</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Persistent operator sessions, takeover, downloads, and replay.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => { void loadSessions(); void loadEvents(active?.id ?? null); }} disabled={busy}>Refresh</button>
          <button className="btn btn-accent btn-sm" aria-expanded={newSessionOpen} aria-controls="browser-session-consent" onClick={() => setNewSessionOpen(open => !open)} disabled={busy}>{newSessionOpen ? "Cancel" : "New session"}</button>
        </div>
      </header>

      {newSessionOpen && (
        <section id="browser-session-consent" className="mx-4 mb-4 rounded-xl border border-soft p-4 md:mx-10" style={{ background: "var(--surface)" }} aria-labelledby="browser-consent-heading">
          <h2 id="browser-consent-heading" className="text-[14px] font-semibold">Review browser access</h2>
          <p className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>Chronos can navigate only the domains listed here. Sensitive sites still require a separate approval.</p>
          <div className="mt-4 grid gap-3 md:grid-cols-2">
            <label className="grid gap-1 text-[12px]">Purpose
              <input className="input-field w-full" value={purpose} onChange={event => setPurpose(event.target.value)} placeholder="Reconcile invoices in the client portal" />
            </label>
            <label className="grid gap-1 text-[12px]">Allowed domains
              <input className="input-field w-full" value={domainScope} onChange={event => setDomainScope(event.target.value)} placeholder="client.example.com, docs.example.com" />
            </label>
            <label className="grid gap-1 text-[12px]">Session expires
              <select className="input-field w-full" value={expiryMinutes} onChange={event => setExpiryMinutes(event.target.value)}>
                <option value="15">15 minutes</option><option value="30">30 minutes</option><option value="60">1 hour</option><option value="120">2 hours</option>
              </select>
            </label>
            <label className="flex items-start gap-2 rounded-lg border border-soft p-3 text-[12.5px]">
              <input type="checkbox" checked={consentConfirmed} onChange={event => setConsentConfirmed(event.target.checked)} className="mt-0.5" />
              <span>I authorize this purpose and domain scope for the selected time window.</span>
            </label>
          </div>
          <div className="mt-4 flex justify-end"><button className="btn btn-accent btn-sm" onClick={() => void createSession()} disabled={busy || !consentConfirmed}>{busy ? "Creating…" : "Create governed session"}</button></div>
        </section>
      )}

      {error && (
        <div role="alert" className="mx-4 mb-3 rounded-lg border px-3 py-2 text-[12.5px] md:mx-10" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}>
          {error}
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto px-4 pb-6 md:grid-cols-[320px_minmax(0,1fr)] md:overflow-hidden md:px-10 md:pb-10">
        <aside className="surface border border-soft rounded-lg overflow-hidden min-h-0 flex max-h-[260px] flex-col md:max-h-none">
          <div className="px-3 py-2 border-b hairline flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Sessions</span>
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{sessions.length}</span>
          </div>
          <div className="overflow-y-auto p-2 space-y-2">
            {loading && <div role="status" className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading browser sessions…</div>}
            {!loading && sessions.length === 0 && (
              <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No browser sessions yet.</div>
            )}
            {sessions.map(session => (
              <button
                key={session.id}
                className="w-full rounded-md border border-soft p-3 text-left smooth"
                onClick={() => setActiveId(session.id)}
                aria-pressed={active?.id === session.id}
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
              <div className="flex flex-col items-stretch justify-between gap-3 border-b hairline px-4 py-3 sm:flex-row sm:items-center">
                <div className="min-w-0">
                  <div className="text-[14px] font-medium truncate">{active.title || "Browser session"}</div>
                  <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>{active.current_url || "No URL"}</div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {active.status === "active" && active.takeover_state !== "requested" && (
                    <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void postAction("request-takeover", { reason: "User requested live takeover" })}>Request takeover</button>
                  )}
                  {active.takeover_state === "requested" && (
                    <button className="btn btn-accent btn-sm" disabled={busy} onClick={() => void openLiveView()}>Open live view</button>
                  )}
                  <button className="btn btn-ghost btn-sm" disabled={busy || !active} onClick={() => { if (window.confirm("Close this browser session? Running browser work will stop.")) void postAction("close"); }}>Close</button>
                  <button className="btn btn-ghost btn-sm" disabled={busy || !active} onClick={() => { if (window.confirm("Revoke this browser session? Chronos will permanently lose access to it.")) void postAction("revoke", { reason: "revoked from browser view" }); }}>Revoke</button>
                </div>
              </div>

              <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_300px] lg:overflow-hidden">
                <div className="min-w-0 min-h-0 p-4 overflow-auto">
                  <div className="rounded-lg border border-soft overflow-hidden bg-black" style={{ aspectRatio: "16 / 10" }} data-testid="browser-viewport">
                    {screenshotUrl ? (
                      <img src={screenshotUrl} alt="Current browser screenshot" className="w-full h-full object-contain" />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-[13px]" style={{ color: "rgba(255,255,255,.72)" }}>
                        {active.status === "degraded" ? "Browser runtime unavailable" : "No screenshot captured"}
                      </div>
                    )}
                  </div>

                  {active.takeover_state === "requested" && (
                    <div role="region" aria-labelledby="browser-takeover-heading" className="mt-4 rounded-lg border px-3 py-3" style={{ borderColor: "var(--warn)", background: "var(--warn-soft)" }}>
                      <div id="browser-takeover-heading" role="status" aria-live="polite" className="text-[13px] font-medium" style={{ color: "var(--warn)" }}>Takeover requested</div>
                      <div className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>{active.takeover_reason || "User input required"}</div>
                      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                        <input
                          aria-label="Browser takeover hand-back summary"
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
                    <div className="divide-y" style={{ borderColor: "var(--border-soft)" }} aria-live="polite" aria-relevant="additions text">
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

                <aside className="space-y-4 overflow-y-auto border-t hairline p-4 lg:border-l lg:border-t-0">
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
                        <button key={`${download.filename}-${index}`} type="button" disabled={!download.download_url || busy} onClick={() => void downloadFile(download)} className="block w-full truncate rounded-md border border-soft px-2 py-1.5 text-left text-[12px] disabled:cursor-not-allowed disabled:opacity-60">
                          {download.filename || "download"}
                          {typeof download.size_bytes === "number" ? ` · ${Math.max(1, Math.round(download.size_bytes / 1024))} KB` : ""}
                        </button>
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
