"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";

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
  const res = await apiFetch(path);
  return res.json() as Promise<T>;
}

async function post(path: string, body?: object) {
  const res = await apiFetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
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
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
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
    } finally {
      setLoading(false);
    }
  }, [showDismissed]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function markAllRead() {
    setBusy("all");
    try {
      await post("/notifications/read", {});
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not mark notifications as read");
    } finally {
      setBusy("");
    }
  }
  async function dismiss(id: string) {
    setBusy(id);
    try {
      await post("/notifications/dismiss", { ids: [id] });
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not dismiss notification");
    } finally {
      setBusy("");
    }
  }

  return (
    <main className="mobile-safe-bottom h-[100dvh] overflow-y-auto px-4 py-6 sm:px-6 sm:py-8">
    <div className="mx-auto max-w-2xl" aria-busy={loading || Boolean(busy)}>
      <div className="mb-5 flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-end">
        <header><a href="/chat" className="btn btn-ghost btn-sm -ml-2 mb-3">← Chronos workspace</a><h1 className="h-page">
          Notifications {unread > 0 && <span className="text-[13px] font-normal" style={{ color: "var(--text-dim)" }}>({unread} unread)</span>}
        </h1></header>
        <div className="flex flex-wrap gap-2">
          <button className="btn btn-secondary btn-sm" aria-pressed={showDismissed} disabled={loading || Boolean(busy)} onClick={() => setShowDismissed(v => !v)}>{showDismissed ? "Hide dismissed" : "Show dismissed"}</button>
          <button className="btn btn-secondary btn-sm" aria-busy={busy === "all"} onClick={() => void markAllRead()} disabled={unread === 0 || loading || Boolean(busy)}>{busy === "all" ? "Marking read…" : "Mark all read"}</button>
        </div>
      </div>
      {error && <div className="mb-3 rounded-lg border px-3 py-2 text-[13px]" role="alert" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div>}
      {loading ? (
        <div className="surface rounded-xl border border-soft p-6 text-center text-[13px]" role="status" aria-live="polite" style={{ color: "var(--text-dim)" }}>Loading notifications…</div>
      ) : items.length === 0 ? (
        <div className="surface rounded-xl border border-soft p-6 text-center text-[13px]" role="status" style={{ color: "var(--text-dim)" }}>{showDismissed ? "No dismissed notifications." : "You’re all caught up."}</div>
      ) : (
        <ul className="surface overflow-hidden rounded-xl border border-soft" aria-label="Notifications">
          {items.map(n => (
            <li key={n.id} className="flex flex-wrap items-start gap-3 border-b hairline px-4 py-4 last:border-b-0 sm:flex-nowrap">
              <span aria-hidden="true" className="mt-1.5 h-2 w-2 flex-shrink-0 rounded-full" style={{ background: n.read_at ? "transparent" : SEVERITY_COLOR[n.severity] || "var(--text-dim)", border: n.read_at ? "1px solid var(--text-dim)" : "none" }} />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-[13px] font-medium" style={{ color: SEVERITY_COLOR[n.severity] || "var(--text)" }}>{n.title}</h2>
                  <span className={`tag capitalize ${severityTagClass(n.severity)}`}>{n.severity || "info"}</span>
                  <span className="sr-only">{n.read_at ? "Read" : "Unread"}</span>
                </div>
                {n.body && <p className="mt-1 break-words text-[12px] leading-5" style={{ color: "var(--text-muted)" }}>{n.body}</p>}
                <p className="mt-1 text-[11px]" style={{ color: "var(--text-dim)" }}>{formatNotificationType(n.type)}{n.created_at ? <> · <time dateTime={n.created_at}>{formatTimestamp(n.created_at)}</time></> : null}</p>
              </div>
              <button className="btn btn-secondary btn-sm ml-5 sm:ml-0" aria-busy={busy === n.id} disabled={Boolean(busy)} onClick={() => void dismiss(n.id)}>{busy === n.id ? "Dismissing…" : "Dismiss"}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
    </main>
  );
}

function severityTagClass(severity: string): string {
  if (severity === "critical") return "tag-danger";
  if (severity === "warning") return "tag-warn";
  if (severity === "success") return "tag-ok";
  return "tag-info";
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString();
}

function formatNotificationType(value: string): string {
  return value.replaceAll("_", " ");
}
