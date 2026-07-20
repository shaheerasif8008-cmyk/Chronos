"use client";

import { useCallback, useEffect, useMemo, useState, type MouseEvent } from "react";
import { apiFetch } from "../../lib/api";

type ComputerSession = {
  id: string;
  status: string;
  purpose?: string | null;
  task_id?: string | null;
  workspace_path?: string | null;
  network_policy?: Record<string, unknown>;
  resource_limits?: Record<string, unknown>;
  capabilities?: string[];
  allowed_egress_domains?: string[];
  expires_at?: string | null;
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
  folder_path?: string;
  display_name?: string;
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
  if (status === "revoked" || status === "failed" || status === "cancelled" || status === "expired") return "var(--danger)";
  if (status === "degraded" || status === "paused") return "var(--warn)";
  return "var(--text-faint)";
}

export default function ComputerScreen() {
  const [sessions, setSessions] = useState<ComputerSession[]>([]);
  const [events, setEvents] = useState<ComputerEvent[]>([]);
  const [grants, setGrants] = useState<LocalGrant[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newSessionOpen, setNewSessionOpen] = useState(false);
  const [sessionPurpose, setSessionPurpose] = useState("");
  const [sessionConfirmed, setSessionConfirmed] = useState(false);
  const [sessionDurationMinutes, setSessionDurationMinutes] = useState(60);
  const [sessionCapabilities, setSessionCapabilities] = useState<string[]>(["terminal", "files", "desktop"]);
  const [sessionEgressDomains, setSessionEgressDomains] = useState("");
  const [screenshot, setScreenshot] = useState<string | null>(null);
  const [screenBusy, setScreenBusy] = useState(false);
  const [inputText, setInputText] = useState("");

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

  const loadScreenshot = useCallback(async (session: ComputerSession | null) => {
    if (!session || session.status !== "active" || !session.capabilities?.includes("desktop")) {
      setScreenshot(null);
      return;
    }
    setScreenBusy(true);
    try {
      const result = (await (await apiFetch(`/computer-sessions/${session.id}/screenshot`)).json()) as { screenshot_data_url?: string };
      setScreenshot(result.screenshot_data_url || null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to capture cloud desktop");
      setScreenshot(null);
    } finally {
      setScreenBusy(false);
    }
  }, []);

  useEffect(() => { void loadSessions(); void loadGrants(); }, [loadGrants, loadSessions]);
  useEffect(() => { void loadEvents(active?.id ?? null); }, [active?.id, loadEvents]);
  useEffect(() => { void loadScreenshot(active); }, [active, loadScreenshot]);

  async function createSession() {
    if (!sessionPurpose.trim() || !sessionConfirmed) {
      setError("Describe the work and confirm the isolated computer scope.");
      return;
    }
    const allowedEgressDomains = sessionEgressDomains
      .split(",")
      .map(domain => domain.trim().toLowerCase())
      .filter(Boolean);
    if (sessionCapabilities.includes("network") && allowedEgressDomains.length === 0) {
      setError("List at least one organization-approved domain for network access.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = (await (await apiFetch("/computer-sessions/", {
        method: "POST",
        body: JSON.stringify({
          purpose: sessionPurpose.trim(),
          consent: {
            purpose: sessionPurpose.trim(),
            capabilities: sessionCapabilities,
            expires_at: new Date(Date.now() + sessionDurationMinutes * 60_000).toISOString(),
            confirmed_by_user: true,
            allowed_egress_domains: sessionCapabilities.includes("network") ? allowedEgressDomains : [],
          },
        }),
      })).json()) as ComputerSession;
      await loadSessions();
      setActiveId(created.id);
      setNewSessionOpen(false);
      setSessionPurpose("");
      setSessionConfirmed(false);
      setSessionEgressDomains("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create computer session");
    } finally {
      setBusy(false);
    }
  }

  function toggleCapability(capability: string) {
    setSessionCapabilities(current => {
      const next = current.includes(capability) ? current.filter(item => item !== capability) : [...current, capability];
      if (capability === "network" && current.includes("network")) {
        setSessionEgressDomains("");
        return next.filter(item => item !== "packages");
      }
      if (capability === "packages" && !current.includes("packages") && !next.includes("network")) next.push("network");
      return next;
    });
    setSessionConfirmed(false);
  }

  async function sessionAction(action: "pause" | "resume" | "cancel") {
    if (!active) return;
    if (action === "cancel" && !window.confirm("Destroy this cloud computer? Unexported files and desktop state will be permanently deleted.")) return;
    setBusy(true);
    setError(null);
    try {
      await apiFetch(`/computer-sessions/${active.id}/${action}`, { method: "POST" });
      await loadSessions();
      await loadEvents(active.id);
      if (action !== "cancel") await loadScreenshot({ ...active, status: action === "pause" ? "paused" : "active" });
      else setScreenshot(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Unable to ${action} computer session`);
    } finally {
      setBusy(false);
    }
  }

  async function sendInput(payload: Record<string, unknown>) {
    if (!active || active.status !== "active") return;
    setScreenBusy(true);
    setError(null);
    try {
      await apiFetch(`/computer-sessions/${active.id}/input`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await loadScreenshot(active);
      await loadEvents(active.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to control cloud desktop");
    } finally {
      setScreenBusy(false);
    }
  }

  function clickScreenshot(event: MouseEvent<HTMLButtonElement>) {
    if (!active) return;
    const bounds = event.currentTarget.getBoundingClientRect();
    const width = Number(active.resource_limits?.screen_width || 1280);
    const height = Number(active.resource_limits?.screen_height || 800);
    const x = Math.max(0, Math.min(width - 1, Math.round(((event.clientX - bounds.left) / bounds.width) * width)));
    const y = Math.max(0, Math.min(height - 1, Math.round(((event.clientY - bounds.top) / bounds.height) * height)));
    void sendInput({ action: "click", x, y, button: "left" });
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
      <header className="flex flex-shrink-0 flex-col items-start justify-between gap-4 px-4 pb-4 pt-5 sm:flex-row sm:gap-6 md:px-10 md:pb-5 md:pt-9">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Computer</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Cloud workspaces, sandboxed commands, artifact exports, and authorized local bridge grants.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => { void loadSessions(); void loadGrants(); void loadEvents(active?.id ?? null); }} disabled={busy}>Refresh</button>
          <button className="btn btn-accent btn-sm" onClick={() => setNewSessionOpen(open => !open)} disabled={busy}>{newSessionOpen ? "Cancel" : "New cloud computer"}</button>
        </div>
      </header>

      {newSessionOpen && (
        <section className="mx-4 mb-4 rounded-xl border border-soft p-4 md:mx-10" style={{ background: "var(--surface)" }} aria-label="Cloud computer consent">
          <h2 className="text-[14px] font-semibold">Review cloud computer scope</h2>
          <p className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>This creates a resumable Linux desktop in E2B. It auto-pauses when idle, is destroyed at the expiry below, and cannot access your Mac unless you separately grant a folder through a paired desktop device.</p>
          <label className="mt-4 grid gap-1 text-[12px]">Purpose
            <input className="input-field w-full" value={sessionPurpose} onChange={event => setSessionPurpose(event.target.value)} placeholder="Analyze the uploaded client workbook" />
          </label>
          <label className="mt-3 grid gap-1 text-[12px]">Authorization window
            <select className="input-field w-full" value={sessionDurationMinutes} onChange={event => { setSessionDurationMinutes(Number(event.target.value)); setSessionConfirmed(false); }}>
              <option value={30}>30 minutes</option>
              <option value={60}>1 hour</option>
              <option value={120}>2 hours</option>
              <option value={240}>4 hours</option>
            </select>
          </label>
          <fieldset className="mt-3 rounded-lg border border-soft p-3">
            <legend className="px-1 text-[12px] font-medium">Allowed capabilities</legend>
            <div className="grid gap-2 text-[12.5px] sm:grid-cols-2">
              {["terminal", "files", "desktop", "network", "packages"].map(capability => (
                <label key={capability} className="flex items-center gap-2">
                  <input type="checkbox" checked={sessionCapabilities.includes(capability)} onChange={() => toggleCapability(capability)} />
                  <span>{capability === "desktop" ? "Linux desktop input" : capability === "network" ? "Allowlisted network access" : capability === "packages" ? "Package installation" : capability}</span>
                </label>
              ))}
            </div>
            <p className="mt-2 text-[11.5px]" style={{ color: "var(--text-dim)" }}>Website logins and external writes still use Chronos approval controls. Use the Browser workspace for governed website automation.</p>
          </fieldset>
          {sessionCapabilities.includes("network") && (
            <label className="mt-3 grid gap-1 text-[12px]">Allowed egress domains
              <input
                className="input-field w-full"
                value={sessionEgressDomains}
                onChange={event => { setSessionEgressDomains(event.target.value); setSessionConfirmed(false); }}
                placeholder="client.example.com, api.vendor.com"
                autoCapitalize="none"
                autoCorrect="off"
              />
              <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>Exact domains only. Chronos rejects domains outside the operator-configured organization ceiling, and E2B blocks every unlisted destination.</span>
            </label>
          )}
          <label className="mt-3 flex items-start gap-2 rounded-lg border border-soft p-3 text-[12.5px]">
            <input type="checkbox" checked={sessionConfirmed} onChange={event => setSessionConfirmed(event.target.checked)} className="mt-0.5" />
            <span>I authorize only the selected capabilities for the stated purpose until {sessionDurationMinutes >= 60 ? `${sessionDurationMinutes / 60} hour${sessionDurationMinutes > 60 ? "s" : ""}` : `${sessionDurationMinutes} minutes`} from now.</span>
          </label>
          <div className="mt-4 flex justify-end"><button className="btn btn-accent btn-sm" onClick={() => void createSession()} disabled={busy || !sessionConfirmed || sessionCapabilities.length === 0 || (sessionCapabilities.includes("network") && !sessionEgressDomains.trim())}>{busy ? "Creating…" : "Create isolated computer"}</button></div>
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
          <div className="flex flex-col items-stretch justify-between gap-3 border-b hairline px-4 py-3 sm:flex-row sm:items-center">
            <div className="min-w-0">
              <div className="text-[14px] font-medium truncate">{active?.purpose || "Cloud computer"}</div>
              <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>{active?.workspace_path || "Select or create a cloud computer session"}</div>
            </div>
            {active && (
              <div className="flex flex-wrap items-center gap-2">
                <span className="tag">{active.status}</span>
                {active.status === "active" && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void sessionAction("pause")}>Pause</button>}
                {active.status === "paused" && <button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void sessionAction("resume")}>Resume</button>}
                {!["cancelled", "expired"].includes(active.status) && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void sessionAction("cancel")}>Destroy</button>}
              </div>
            )}
          </div>

          <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_340px] lg:overflow-hidden">
            <div className="min-w-0 min-h-0 p-4 overflow-auto">
              <div className="rounded-lg border border-soft overflow-hidden bg-black" style={{ aspectRatio: "16 / 10" }} data-testid="computer-viewport">
                {screenshot && active?.status === "active" ? (
                  <button className="relative block h-full w-full cursor-crosshair" aria-label="Cloud desktop; click to send a left click" onClick={clickScreenshot} disabled={screenBusy}>
                    <img src={screenshot} alt="Current E2B Linux desktop" className="h-full w-full object-contain" />
                    {screenBusy && <span className="absolute inset-0 grid place-items-center bg-black/35 text-[12px] text-white">Updating…</span>}
                  </button>
                ) : (
                  <div className="w-full h-full flex flex-col justify-center px-6 text-[13px]" style={{ color: "rgba(255,255,255,.76)" }}>
                    <div className="text-[15px] font-medium text-white">E2B Linux desktop</div>
                    <div className="mt-2 font-mono text-[12px] break-all">{active?.workspace_path || "No active workspace"}</div>
                    <div className="mt-3">{!active ? "Select a session." : active.status === "paused" ? "This computer is paused; resume it to restore the desktop." : !active.capabilities?.includes("desktop") ? "This session was authorized for terminal/files only." : "Capture pending. Refresh the screen to retry."}</div>
                  </div>
                )}
              </div>
              {active?.capabilities?.includes("desktop") && active.status === "active" && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <button className="btn btn-ghost btn-sm" onClick={() => void loadScreenshot(active)} disabled={screenBusy}>Refresh screen</button>
                  <button className="btn btn-ghost btn-sm" onClick={() => void sendInput({ action: "scroll", direction: "up", amount: 3 })} disabled={screenBusy}>Scroll up</button>
                  <button className="btn btn-ghost btn-sm" onClick={() => void sendInput({ action: "scroll", direction: "down", amount: 3 })} disabled={screenBusy}>Scroll down</button>
                  <input className="input-field min-w-[180px] flex-1" aria-label="Text to type in cloud desktop" value={inputText} onChange={event => setInputText(event.target.value)} placeholder="Type into the focused app" />
                  <button className="btn btn-secondary btn-sm" onClick={() => { void sendInput({ action: "type", text: inputText }); setInputText(""); }} disabled={screenBusy || !inputText}>Type</button>
                  <button className="btn btn-ghost btn-sm" onClick={() => void sendInput({ action: "key", key: "enter" })} disabled={screenBusy}>Enter</button>
                </div>
              )}

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

            <aside className="min-h-0 space-y-4 overflow-y-auto border-t hairline p-4 lg:border-l lg:border-t-0">
              <div>
                <div className="text-[13px] font-medium">Sandbox policy</div>
                <div className="mt-2 rounded-lg border border-soft p-3 text-[12.5px] space-y-1" style={{ color: "var(--text-dim)" }}>
                  <div>Idle pause: {String(active?.resource_limits?.idle_pause_seconds ?? 900)} seconds</div>
                  <div>Consent expiry: {active?.expires_at ? labelTime(active.expires_at) : "—"}</div>
                  <div>Network: {String(active?.network_policy?.mode ?? "deny_egress")}</div>
                  {active?.allowed_egress_domains?.length ? <div>Allowed domains: {active.allowed_egress_domains.join(", ")}</div> : null}
                  <div>Capabilities: {active?.capabilities?.join(", ") || "—"}</div>
                </div>
              </div>

              <div>
                <div className="text-[13px] font-medium">Local bridge</div>
                <p className="mt-1 text-[12.5px] leading-5" style={{ color: "var(--text-dim)" }}>For client machines, folder paths stay on the paired Chronos desktop app. Grant a folder there; the server receives only an opaque, revocable grant.</p>
                <a className="btn btn-secondary btn-sm mt-2 inline-flex" href="/settings?tab=devices">Manage paired devices</a>
                <div className="mt-3 space-y-2">
                  {grants.length === 0 && <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>No local folder grants.</div>}
                  {grants.map(grant => (
                    <div key={grant.id} className="rounded-lg border border-soft p-3" data-testid="local-grant-row">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[12.5px] font-medium truncate">{grant.purpose || "Local grant"}</span>
                        <span className="tag">{grant.status}</span>
                      </div>
                      <div className="mt-1 text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{grant.display_name || grant.folder_path || "Authorized folder"}</div>
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
