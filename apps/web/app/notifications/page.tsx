"use client";

import { useCallback, useEffect, useState } from "react";

const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;
function apiBase() {
  if (CONFIGURED_API_BASE) return CONFIGURED_API_BASE;
  if (typeof window !== "undefined") {
    const webPort = Number(window.location.port || "3000");
    if (Number.isFinite(webPort) && webPort >= 3000 && webPort < 3100) {
      return `http://${window.location.hostname}:${8000 + (webPort - 3000)}`;
    }
  }
  return "http://localhost:8000";
}

type Notification = {
  id: string;
  type: string;
  title: string;
  body: string | null;
  severity: string;
  resource_type: string | null;
  resource_id: string | null;
  read_at: string | null;
  created_at: string | null;
};

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { credentials: "include" });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<T>;
}

async function post(path: string, body?: object) {
  const res = await fetch(`${apiBase()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    credentials: "include",
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

const SEVERITY_COLOR: Record<string, string> = {
  info: "var(--text-dim)",
  success: "var(--ok, #16a34a)",
  warning: "var(--warn, #d97706)",
  critical: "var(--danger, #dc2626)",
};

export default function NotificationsPage() {
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const [error, setError] = useState("");
  const [showDismissed, setShowDismissed] = useState(false);

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [list, count] = await Promise.all([
        getJson<Notification[]>(`/notifications?include_dismissed=${showDismissed}`),
        getJson<{ count: number }>("/notifications/unread_count"),
      ]);
      setItems(list);
      setUnread(count.count);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load notifications");
    }
  }, [showDismissed]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function markAllRead() {
    await post("/notifications/read", {});
    await refresh();
  }
  async function dismiss(id: string) {
    await post("/notifications/dismiss", { ids: [id] });
    await refresh();
  }

  return (
    <div className="max-w-2xl mx-auto p-6">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-[20px] font-semibold">
          Notifications {unread > 0 && <span className="text-[13px] font-normal" style={{ color: "var(--text-dim)" }}>({unread} unread)</span>}
        </h1>
        <div className="flex gap-2">
          <button className="btn btn-sm" onClick={() => setShowDismissed(v => !v)}>{showDismissed ? "Hide dismissed" : "Show dismissed"}</button>
          <button className="btn btn-sm" onClick={() => void markAllRead()} disabled={unread === 0}>Mark all read</button>
        </div>
      </div>
      {error && <div className="text-[13px] mb-3" style={{ color: "var(--danger, #dc2626)" }}>{error}</div>}
      {items.length === 0 ? (
        <div className="surface border border-soft rounded-xl p-6 text-center text-[13px]" style={{ color: "var(--text-dim)" }}>No notifications.</div>
      ) : (
        <div className="surface border border-soft rounded-xl overflow-hidden">
          {items.map(n => (
            <div key={n.id} className="px-4 py-3 border-b hairline last:border-b-0 flex items-start gap-3">
              <span className="mt-1.5 w-2 h-2 rounded-full flex-shrink-0" style={{ background: n.read_at ? "transparent" : SEVERITY_COLOR[n.severity] || "var(--text-dim)", border: n.read_at ? "1px solid var(--text-dim)" : "none" }} />
              <div className="min-w-0 flex-1">
                <div className="font-medium text-[13px]" style={{ color: SEVERITY_COLOR[n.severity] || "var(--text)" }}>{n.title}</div>
                {n.body && <div className="text-[12px] mt-0.5 break-words" style={{ color: "var(--text-muted)" }}>{n.body}</div>}
                <div className="text-[11px] mt-1" style={{ color: "var(--text-dim)" }}>{n.type}{n.created_at ? ` · ${new Date(n.created_at).toLocaleString()}` : ""}</div>
              </div>
              <button className="btn btn-sm" onClick={() => void dismiss(n.id)}>Dismiss</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
