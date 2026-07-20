"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";

type ReviewStatus = "pending" | "acknowledged" | "false_positive" | "closed";
type QuarantineEvent = {
  id: string;
  source: string;
  filename: string;
  mime_type?: string | null;
  size_bytes: number;
  sha256: string;
  verdict: "clean" | "infected" | "error";
  signature?: string | null;
  error_code?: string | null;
  content_disarm_status?: string | null;
  content_disarm_reason?: string | null;
  review_status: ReviewStatus;
  review_note?: string | null;
  scanned_at: string;
};

type Queue = { items: QuarantineEvent[]; total: number; pending: number };

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${Math.ceil(value / 1024)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function FileQuarantinePanel() {
  const [queue, setQueue] = useState<Queue | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [selected, setSelected] = useState<QuarantineEvent | null>(null);
  const [note, setNote] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    const response = await apiFetch(`/admin/file-quarantine?review_status=${showAll ? "all" : "pending"}`);
    if (!response.ok) throw new Error("File quarantine could not be loaded.");
    const next = await response.json() as Queue;
    setQueue(next);
    setSelected(current => current ? next.items.find(item => item.id === current.id) ?? null : null);
  }, [showAll]);

  useEffect(() => {
    void load().catch(exc => setError(exc instanceof Error ? exc.message : "File quarantine could not be loaded."));
  }, [load]);

  async function review(status: Exclude<ReviewStatus, "pending">) {
    if (!selected) return;
    setBusy(status);
    setError("");
    try {
      const response = await apiFetch(`/admin/file-quarantine/${selected.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status, note, confirmation: status === "false_positive" ? confirmation : null }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({})) as { detail?: string | Array<{ msg?: string }> };
        const detail = Array.isArray(payload.detail) ? payload.detail[0]?.msg : payload.detail;
        throw new Error(detail || "The review decision was not saved.");
      }
      setNote("");
      setConfirmation("");
      setSelected(null);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "The review decision was not saved.");
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="mb-8" aria-labelledby="file-quarantine-heading">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="file-quarantine-heading" className="text-[16px] font-semibold">File quarantine</h2>
          <p className="mt-1 text-[12px]" style={{ color: "var(--text-dim)" }}>
            Metadata-only review. Blocked bytes are discarded and cannot be restored from this screen.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className="btn btn-ghost btn-sm" aria-pressed={showAll} onClick={() => setShowAll(value => !value)}>
            {showAll ? "Pending only" : "Show reviewed"}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => void load().catch(exc => setError(exc instanceof Error ? exc.message : "Refresh failed."))}>Refresh</button>
        </div>
      </div>

      {error && <div role="alert" className="mb-3 rounded-lg border px-3 py-2 text-[12px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div>}
      {!queue ? (
        <div role="status" className="surface rounded-xl border border-soft p-4 text-[13px]" style={{ color: "var(--text-dim)" }}>Loading quarantine evidence…</div>
      ) : (
        <div className="surface overflow-hidden rounded-xl border border-soft">
          <div className="flex items-center justify-between border-b hairline px-4 py-3 text-[12px]">
            <span>{queue.pending} pending review{queue.pending === 1 ? "" : "s"}</span>
            <span style={{ color: "var(--text-dim)" }}>{queue.total} shown</span>
          </div>
          {queue.items.length === 0 ? (
            <div className="px-4 py-6 text-center text-[13px]" style={{ color: "var(--text-dim)" }}>No file-security events match this view.</div>
          ) : (
            <div className="divide-y hairline">
              {queue.items.map(item => (
                <button
                  key={item.id}
                  type="button"
                  className="block w-full px-4 py-3 text-left hover:bg-black/[0.025] focus-visible:outline focus-visible:outline-2"
                  aria-expanded={selected?.id === item.id}
                  onClick={() => { setSelected(item); setNote(item.review_note || ""); setConfirmation(""); }}
                >
                  <div className="flex min-w-0 items-center justify-between gap-3">
                    <span className="truncate text-[13px] font-medium">{item.filename}</span>
                    <span className="shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium" style={{ background: item.verdict === "infected" ? "color-mix(in srgb, var(--danger) 14%, transparent)" : "var(--surface-2)", color: item.verdict === "infected" ? "var(--danger)" : "var(--text-dim)" }}>{item.verdict}</span>
                  </div>
                  <div className="mt-1 text-[11px]" style={{ color: "var(--text-dim)" }}>{item.source.replaceAll("_", " ")} · {formatBytes(item.size_bytes)} · {new Date(item.scanned_at).toLocaleString()}</div>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {selected && (
        <div className="surface mt-3 rounded-xl border border-soft p-4" aria-live="polite">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0"><div className="truncate text-[13px] font-semibold">{selected.filename}</div><div className="mt-1 font-mono text-[10px] break-all" style={{ color: "var(--text-dim)" }}>SHA-256 {selected.sha256}</div></div>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setSelected(null)} aria-label="Close quarantine review">Close</button>
          </div>
          <dl className="mt-3 grid gap-2 text-[12px] sm:grid-cols-2">
            <div><dt style={{ color: "var(--text-dim)" }}>Detection</dt><dd>{selected.signature || selected.error_code || "No malware signature"}</dd></div>
            <div><dt style={{ color: "var(--text-dim)" }}>Content disarm</dt><dd>{selected.content_disarm_reason || selected.content_disarm_status || "Not applicable"}</dd></div>
            <div><dt style={{ color: "var(--text-dim)" }}>Media type</dt><dd>{selected.mime_type || "Unknown"}</dd></div>
            <div><dt style={{ color: "var(--text-dim)" }}>Review state</dt><dd>{selected.review_status.replaceAll("_", " ")}</dd></div>
          </dl>
          <label className="mt-4 block text-[12px] font-medium" htmlFor="quarantine-review-note">Review note</label>
          <textarea id="quarantine-review-note" className="surface mt-1 min-h-20 w-full rounded-lg border border-soft px-3 py-2 text-[13px]" maxLength={1000} value={note} onChange={event => setNote(event.target.value)} placeholder="Record investigation context without copying sensitive file content." />
          <label className="mt-3 block text-[12px] font-medium" htmlFor="quarantine-false-positive">False-positive confirmation</label>
          <input id="quarantine-false-positive" className="surface mt-1 w-full rounded-lg border border-soft px-3 py-2 text-[13px]" value={confirmation} onChange={event => setConfirmation(event.target.value)} placeholder="Type MARK FALSE POSITIVE" autoComplete="off" />
          <div className="mt-4 flex flex-wrap justify-end gap-2">
            <button type="button" className="btn btn-secondary btn-sm" disabled={Boolean(busy)} onClick={() => void review("acknowledged")}>{busy === "acknowledged" ? "Saving…" : "Acknowledge"}</button>
            <button type="button" className="btn btn-danger-soft btn-sm" disabled={Boolean(busy) || confirmation !== "MARK FALSE POSITIVE" || note.trim().length < 10} onClick={() => void review("false_positive")}>{busy === "false_positive" ? "Saving…" : "Mark false positive"}</button>
            <button type="button" className="btn btn-accent btn-sm" disabled={Boolean(busy)} onClick={() => void review("closed")}>{busy === "closed" ? "Saving…" : "Close review"}</button>
          </div>
        </div>
      )}
    </section>
  );
}
