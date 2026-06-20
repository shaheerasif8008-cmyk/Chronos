"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { apiFetch } from "../../lib/api";

type ComputerSession = {
  id: string;
  status: string;
  purpose?: string | null;
  task_id?: string | null;
  workspace_path?: string | null;
  updated_at?: string;
  created_at?: string;
};

type DesktopSession = {
  id: string;
  status: string;
  purpose?: string | null;
  task_id?: string | null;
  updated_at?: string;
};

type ComputerEvent = {
  id?: string;
  seq?: number;
  event_type?: string;
  payload?: Record<string, unknown>;
  created_at?: string;
};

function statusColor(status?: string | null) {
  if (status === "active") return "var(--accent)";
  if (status === "revoked" || status === "failed") return "var(--danger)";
  if (status === "degraded") return "var(--warn)";
  return "var(--text-faint)";
}

function timeLabel(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Render one computer event as a terminal line.
function EventLine({ event }: { event: ComputerEvent }) {
  const p = event.payload ?? {};
  const t = event.event_type ?? "event";
  const time = timeLabel(event.created_at);
  const dim = { color: "var(--text-dim)" } as const;

  if (t === "computer_session_created") {
    return <Line time={time} glyph="●" glyphColor="var(--accent)">session started{p.purpose ? ` — ${String(p.purpose)}` : ""}{p.runtime ? `  (${String(p.runtime)})` : ""}</Line>;
  }
  if (t === "computer_command" || t === "computer_package_install") {
    const ok = p.status === "succeeded" || p.returncode === 0;
    return (
      <div className="px-3 py-1.5 border-b hairline">
        <div className="font-mono text-[12.5px]"><span style={{ color: "var(--accent)" }}>$ </span>{String(p.command ?? "")}</div>
        <div className="mt-0.5 flex items-center gap-2 text-[11px]" style={dim}>
          <span style={{ color: ok ? "var(--ok)" : "var(--danger)" }}>{ok ? "exit 0" : `exit ${p.returncode ?? "?"}`}</span>
          {typeof p.stdout_bytes === "number" && <span>{p.stdout_bytes}b out</span>}
          {typeof p.stderr_bytes === "number" && Number(p.stderr_bytes) > 0 && <span style={{ color: "var(--warn)" }}>{p.stderr_bytes}b err</span>}
          <span className="ml-auto">{time}</span>
        </div>
      </div>
    );
  }
  if (t === "computer_file_written") return <Line time={time} glyph="✎" glyphColor="var(--ok)">wrote {String(p.path ?? "")}{typeof p.bytes === "number" ? `  (${p.bytes}b)` : ""}</Line>;
  if (t === "computer_file_read") return <Line time={time} glyph="↘">read {String(p.path ?? "")}</Line>;
  if (t === "computer_files_listed") return <Line time={time} glyph="≣">listed {String(p.path ?? "")}{typeof p.count === "number" ? `  (${p.count})` : ""}</Line>;
  if (t === "computer_artifact_exported") return <Line time={time} glyph="⇪" glyphColor="var(--accent)">exported {String(p.path ?? "")}</Line>;
  if (t === "computer_screenshot") return <Line time={time} glyph="▣">screenshot {String(p.status ?? "")}</Line>;

  return <Line time={time} glyph="·">{t.replace(/^computer_/, "").replace(/_/g, " ")}</Line>;
}

function Line({ time, glyph, glyphColor, children }: { time: string; glyph: string; glyphColor?: string; children: ReactNode }) {
  return (
    <div className="px-3 py-1.5 border-b hairline flex items-baseline gap-2 text-[12.5px]">
      <span style={{ color: glyphColor ?? "var(--text-faint)" }}>{glyph}</span>
      <span className="flex-1 min-w-0">{children}</span>
      <span className="text-[11px]" style={{ color: "var(--text-dim)" }}>{time}</span>
    </div>
  );
}

export default function ComputerLiveView() {
  const [open, setOpen] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [session, setSession] = useState<ComputerSession | null>(null);
  const [events, setEvents] = useState<ComputerEvent[]>([]);
  const [desktop, setDesktop] = useState<DesktopSession | null>(null);
  const [shot, setShot] = useState<string | null>(null);
  const [shotStatus, setShotStatus] = useState<string>("");
  const [view, setView] = useState<"screen" | "console">("screen");
  const feedRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onOpen(e: Event) {
      const id = (e as CustomEvent<{ id?: string }>).detail?.id ?? null;
      setSessionId(id);
      setOpen(true);
    }
    window.addEventListener("chronos:open-computer", onOpen as EventListener);
    return () => window.removeEventListener("chronos:open-computer", onOpen as EventListener);
  }, []);

  const refresh = useCallback(async () => {
    if (!open) return;
    // Command sandbox (activity console).
    const sessions = await apiFetch("/computer-sessions/").then(r => r.json()).catch(() => []) as ComputerSession[];
    const active = (sessionId && sessions.find(s => s.id === sessionId))
      || sessions.find(s => s.status === "active")
      || sessions[0] || null;
    setSession(active);
    if (active) {
      const evs = await apiFetch(`/computer-sessions/${active.id}/events`).then(r => r.json()).catch(() => []) as ComputerEvent[];
      setEvents([...evs].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)));
    } else {
      setEvents([]);
    }

    // GUI desktop operator (true pixel stream).
    const desks = await apiFetch("/desktop-sessions/").then(r => r.json()).catch(() => []) as DesktopSession[];
    const liveDesk = desks.find(d => d.status === "active") || desks.find(d => d.status === "degraded") || desks[0] || null;
    setDesktop(liveDesk);
    if (liveDesk && liveDesk.status !== "revoked" && liveDesk.status !== "closed") {
      const frame = await apiFetch(`/desktop-sessions/${liveDesk.id}/screenshot`).then(r => r.json()).catch(() => null) as { screenshot_data_url?: string | null; status?: string } | null;
      setShot(frame?.screenshot_data_url ?? null);
      setShotStatus(frame?.status ?? "");
    } else {
      setShot(null);
      setShotStatus("");
    }
  }, [open, sessionId]);

  useEffect(() => {
    if (!open) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  }, [open, refresh]);

  // Default to the live screen when a desktop session exists, else the console.
  useEffect(() => {
    if (desktop && desktop.status !== "revoked" && desktop.status !== "closed") setView("screen");
    else if (!desktop) setView("console");
  }, [desktop]);

  useEffect(() => {
    if (view === "console" && feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [events, view]);

  if (!open) return null;

  const hasDesktop = !!desktop && desktop.status !== "revoked" && desktop.status !== "closed";

  return (
    <div className="fixed inset-0 z-[60] flex flex-col" style={{ background: "var(--bg)" }}>
      <header className="flex items-center gap-3 px-5 h-[52px] border-b hairline">
        <div className="flex items-center gap-2 text-[14px] font-semibold">
          <span style={{ color: statusColor(session?.status || desktop?.status) }}>●</span>
          Virtual computer
        </div>
        <div className="text-[12.5px] truncate" style={{ color: "var(--text-dim)" }}>
          {desktop?.purpose || session?.purpose || "Governed sandbox workspace"}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <div className="flex rounded-lg border overflow-hidden" style={{ borderColor: "var(--border)" }}>
            <button className="px-3 py-1 text-[12.5px]" onClick={() => setView("screen")}
                    style={{ background: view === "screen" ? "var(--accent-soft)" : "transparent", color: view === "screen" ? "var(--accent)" : "var(--text-dim)" }}>Screen</button>
            <button className="px-3 py-1 text-[12.5px]" onClick={() => setView("console")}
                    style={{ background: view === "console" ? "var(--accent-soft)" : "transparent", color: view === "console" ? "var(--accent)" : "var(--text-dim)" }}>Console</button>
          </div>
          <span className="text-[12px] rounded-full px-2.5 py-0.5 border" style={{ borderColor: "var(--border)", color: statusColor(session?.status || desktop?.status) }}>
            {session?.status || desktop?.status || "no session"}
          </span>
          <button className="btn btn-ghost btn-sm" onClick={() => void refresh()}>Refresh</button>
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>Close</button>
        </div>
      </header>

      <div className="flex-1 min-h-0 flex">
        <main className="flex-1 min-w-0 flex flex-col">
          {view === "screen" ? (
            <div className="flex-1 min-h-0 flex items-center justify-center p-4" style={{ background: "#000" }}>
              {shot ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={shot} alt="Live desktop" className="max-w-full max-h-full object-contain rounded-md" style={{ boxShadow: "0 0 0 1px rgba(255,255,255,0.08)" }} />
              ) : (
                <div className="text-center text-[13px] max-w-md" style={{ color: "rgba(255,255,255,.72)" }}>
                  {!hasDesktop
                    ? "No desktop session is running. Chronos opens one when a task needs to operate a graphical app, and the live screen streams here."
                    : shotStatus === "degraded" || shotStatus === "unavailable"
                      ? "This runtime has no virtual display, so there are no pixels to stream. Switch to Console to follow the sandbox commands and file activity."
                      : "Waiting for the next desktop frame…"}
                </div>
              )}
            </div>
          ) : (
            <>
              <div className="px-5 py-2 border-b hairline text-[11px] uppercase tracking-wide" style={{ color: "var(--text-dim)" }}>Activity console</div>
              <div ref={feedRef} className="flex-1 min-h-0 overflow-auto" style={{ background: "var(--surface)" }}>
                {!session ? (
                  <div className="h-full flex items-center justify-center text-[13px]" style={{ color: "var(--text-dim)" }}>
                    No virtual computer session is active. Chronos opens one when a task needs to run commands or build files.
                  </div>
                ) : events.length === 0 ? (
                  <div className="p-5 text-[13px]" style={{ color: "var(--text-dim)" }}>Waiting for the first command…</div>
                ) : (
                  events.map((ev, i) => <EventLine key={ev.id ?? ev.seq ?? i} event={ev} />)
                )}
              </div>
            </>
          )}
        </main>

        <aside className="w-[320px] flex-shrink-0 border-l hairline p-5 space-y-4 overflow-auto">
          <div>
            <div className="text-[11px] uppercase tracking-wide mb-1" style={{ color: "var(--text-dim)" }}>Screen</div>
            <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>
              {hasDesktop ? `Desktop ${String(desktop?.status)}${shot ? " · streaming" : ""}` : "No desktop session"}
            </div>
          </div>
          <div>
            <div className="text-[11px] uppercase tracking-wide mb-1" style={{ color: "var(--text-dim)" }}>Workspace</div>
            <div className="font-mono text-[12px] break-all rounded-md border p-2" style={{ borderColor: "var(--border)" }}>
              {session?.workspace_path || "—"}
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2 text-[12px]">
            <div className="rounded-md border px-2 py-1.5" style={{ borderColor: "var(--border)" }}>Terminal audited</div>
            <div className="rounded-md border px-2 py-1.5" style={{ borderColor: "var(--border)" }}>Files sandboxed</div>
            <div className="rounded-md border px-2 py-1.5" style={{ borderColor: "var(--border)" }}>Network governed</div>
            <div className="rounded-md border px-2 py-1.5" style={{ borderColor: "var(--border)" }}>Artifacts exported</div>
          </div>
          <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>
            {session?.task_id ? `Task ${session.task_id.slice(0, 8)}` : desktop?.task_id ? `Task ${desktop.task_id.slice(0, 8)}` : "Standalone session"}
            {session?.updated_at ? ` · updated ${timeLabel(session.updated_at)}` : ""}
          </div>
        </aside>
      </div>
    </div>
  );
}
