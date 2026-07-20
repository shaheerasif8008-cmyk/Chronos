"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";

type Suggestion = {
  id: string;
  suggested_patch?: string;
  rationale?: string | null;
  status?: string;
  created_at?: string;
};

function labelTime(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export default function ContextSuggestionsScreen() {
  const [items, setItems] = useState<Suggestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [toast, setToast] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);
  const [loadError, setLoadError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      setItems(await (await apiFetch("/context/suggestions?status=pending")).json());
    } catch (error) {
      setItems([]);
      setLoadError(error instanceof Error ? error.message : "Context suggestions could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  async function generate() {
    setBusy("generate");
    try {
      const res = await (await apiFetch("/context/suggestions/generate", { method: "POST" })).json();
      setToast(res.created
        ? { kind: "ok", text: "New context suggestion proposed." }
        : { kind: "ok", text: "Nothing new to suggest right now." });
      await load();
    } catch {
      setToast({ kind: "danger", text: "Could not generate a suggestion." });
    } finally {
      setBusy(null);
    }
  }

  async function decide(id: string, action: "apply" | "reject") {
    setBusy(id);
    try {
      await apiFetch(`/context/suggestions/${id}/${action}`, { method: "POST", body: JSON.stringify({}) });
      setItems(prev => prev.filter(s => s.id !== id));
      setToast({ kind: "ok", text: action === "apply" ? "Applied to org context." : "Suggestion rejected." });
    } catch {
      setToast({ kind: "danger", text: `Could not ${action} suggestion.` });
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      {toast && (
        <div role={toast.kind === "danger" ? "alert" : "status"} className="mb-4 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: toast.kind === "ok" ? "var(--ok)" : "var(--danger)", color: toast.kind === "ok" ? "var(--ok)" : "var(--danger)" }}>
          {toast.text}
        </div>
      )}

      <div className="flex flex-col items-start justify-between gap-3 mb-4 sm:flex-row sm:items-center">
        <p className="text-[13px] max-w-[620px]" style={{ color: "var(--text-dim)" }}>
          Chronos proposes updates to your organization context as it learns from conversations. Review and apply what is accurate.
        </p>
        <button className="btn btn-accent btn-sm flex-shrink-0" disabled={busy === "generate"} onClick={() => void generate()}>
          {busy === "generate" ? "Thinking…" : "Generate suggestion"}
        </button>
      </div>

      {loading && <div className="text-[13px]" style={{ color: "var(--text-dim)" }}>Loading…</div>}
      {loadError && <div className="mb-3 flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-[13px]" role="alert" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}><span>{loadError}</span><button className="btn btn-ghost btn-sm" onClick={() => void load()}>Try again</button></div>}
      {!loading && !loadError && items.length === 0 && (
        <div className="surface border border-soft rounded-xl px-4 py-8 text-center text-[13px]" style={{ color: "var(--text-dim)" }}>
          No pending context suggestions.
        </div>
      )}

      <div className="space-y-3">
        {items.map(s => (
          <div key={s.id} className="surface border border-soft rounded-xl p-4">
            <div className="flex items-center justify-between gap-3 mb-2">
              <span className="text-[12px]" style={{ color: "var(--text-faint)" }}>{labelTime(s.created_at)}</span>
              <div className="flex gap-2">
                <button className="btn btn-accent btn-sm" disabled={busy === s.id} onClick={() => void decide(s.id, "apply")}>Apply</button>
                <button className="btn btn-ghost btn-sm" disabled={busy === s.id} onClick={() => void decide(s.id, "reject")}>Reject</button>
              </div>
            </div>
            {s.rationale && <p className="text-[13px] mb-2" style={{ color: "var(--text-dim)" }}>{s.rationale}</p>}
            <pre className="text-[12.5px] whitespace-pre-wrap rounded-lg border border-soft p-3 overflow-auto" style={{ background: "var(--surface-2)", maxHeight: 240 }}>
              {s.suggested_patch || ""}
            </pre>
          </div>
        ))}
      </div>
    </div>
  );
}
