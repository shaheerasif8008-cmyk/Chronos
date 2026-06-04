"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { getContentText } from "../../lib/artifacts";

// ─── Types ────────────────────────────────────────────────────────────────────
type SourceScopes = {
  web: boolean;
  project: boolean;
  connector: boolean;
  upload: boolean;
  mcp: boolean;
  allowed_domains: string[];
  disallowed_domains: string[];
};

type ResearchRun = {
  id: string;
  question: string;
  depth: string;
  source_scopes: SourceScopes;
  citation_policy?: string | null;
  time_budget_seconds?: number | null;
  status: "pending" | "planning" | "running" | "complete" | "failed" | "cancelled";
  plan?: { queries?: string[]; rounds?: number } | null;
  findings?: { summary?: string; citation_count?: number } | null;
  limitations?: string | null;
  report_artifact_id?: string | null;
  error?: string | null;
  token_count?: number | null;
  cost_estimate?: number | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  project_id?: string | null;
};

type ResearchEvent = {
  id: string;
  run_id: string;
  seq: number;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

type Citation = {
  id: string;
  marker: string;
  source_type: string;
  source_id?: string | null;
  source_title?: string | null;
  url?: string | null;
  snippet?: string | null;
  confidence?: number | null;
  distance?: number | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
};

type Project = { id: string; name: string };

const TERMINAL = new Set(["complete", "failed", "cancelled"]);

function statusColor(s: ResearchRun["status"]): string {
  if (s === "complete") return "var(--ok)";
  if (s === "failed") return "var(--danger)";
  if (s === "cancelled") return "var(--text-dim)";
  return "var(--accent)";
}

function statusBg(s: ResearchRun["status"]): string {
  if (s === "complete") return "var(--ok-soft)";
  if (s === "failed") return "var(--danger-soft)";
  if (s === "cancelled") return "transparent";
  return "var(--accent-soft)";
}

// ─── Main Component ───────────────────────────────────────────────────────────
export default function ResearchScreen() {
  const [runs, setRuns] = useState<ResearchRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);

  const loadRuns = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch("/research/");
      const data: ResearchRun[] = await res.json();
      setRuns(data.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()));
    } catch {
      // silent – list stays empty
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadRuns(); }, [loadRuns]);

  function onRunCreated(id: string) {
    setComposerOpen(false);
    void loadRuns().then(() => setSelectedId(id));
  }

  return (
    <div className="flex h-full min-h-0">
      {/* ── Left aside ── */}
      <aside className="w-[320px] flex-shrink-0 border-r flex flex-col" style={{ borderColor: "var(--border)" }}>
        <div className="p-3 border-b flex flex-col gap-2" style={{ borderColor: "var(--border)" }}>
          <div className="flex items-center justify-between">
            <div className="text-[15px] font-semibold">Research</div>
            <button
              data-testid="research-new-run"
              onClick={() => setComposerOpen(v => !v)}
              className="btn btn-primary btn-sm"
            >
              {composerOpen ? "Cancel" : "New research run"}
            </button>
          </div>
          {composerOpen && (
            <ResearchComposer
              onCreated={onRunCreated}
              onCancel={() => setComposerOpen(false)}
            />
          )}
        </div>
        <div className="flex-1 overflow-auto p-2">
          {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading…</div>}
          {!loading && runs.length === 0 && (
            <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No research runs yet.</div>
          )}
          {runs.map(run => (
            <button
              key={run.id}
              onClick={() => setSelectedId(run.id)}
              className="w-full text-left px-3 py-2 rounded-lg mb-1"
              style={{ background: selectedId === run.id ? "var(--accent-soft)" : "transparent" }}
            >
              <div className="text-[13.5px] font-medium truncate">{run.question}</div>
              <div className="text-[11.5px] flex items-center gap-2 mt-0.5" style={{ color: "var(--text-dim)" }}>
                <span
                  className="inline-block rounded px-1.5 py-0.5 text-[10.5px] font-semibold"
                  style={{ background: statusBg(run.status), color: statusColor(run.status) }}
                >
                  {run.status}
                </span>
                <span>{new Date(run.created_at).toLocaleString()}</span>
              </div>
            </button>
          ))}
        </div>
      </aside>

      {/* ── Detail pane ── */}
      <section className="flex-1 min-w-0">
        {selectedId
          ? <RunDetail key={selectedId} runId={selectedId} onCancelled={loadRuns} />
          : <div className="h-full flex items-center justify-center text-[14px]" style={{ color: "var(--text-dim)" }}>
              Select a research run or start a new one.
            </div>
        }
      </section>
    </div>
  );
}

// ─── Composer ────────────────────────────────────────────────────────────────
function ResearchComposer({ onCreated, onCancel }: { onCreated: (id: string) => void; onCancel: () => void }) {
  const [question, setQuestion] = useState("");
  const [depth, setDepth] = useState<"quick" | "standard" | "exhaustive" | "trusted">("standard");
  const [webScope, setWebScope] = useState(true);
  const [projectScope, setProjectScope] = useState(false);
  const [connectorScope, setConnectorScope] = useState(false);
  const [uploadScope, setUploadScope] = useState(false);
  const [mcpScope, setMcpScope] = useState(false);
  const [timeBudget, setTimeBudget] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedProject, setSelectedProject] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectScope) return;
    apiFetch("/projects/").then(r => r.json()).then((data: Project[]) => setProjects(data)).catch(() => setProjects([]));
  }, [projectScope]);

  async function submit() {
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {
        question: question.trim(),
        depth,
        source_scopes: {
          web: webScope,
          project: projectScope,
          connector: connectorScope,
          upload: uploadScope,
          mcp: mcpScope,
          allowed_domains: [],
          disallowed_domains: [],
        },
      };
      if (timeBudget) body.time_budget_seconds = Number(timeBudget);
      if (projectScope && selectedProject) body.project_id = selectedProject;

      const res = await apiFetch("/research/", { method: "POST", body: JSON.stringify(body) });
      const data: { run_id: string; status: string } = await res.json();
      onCreated(data.run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start research run.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-2 pt-1">
      {error && (
        <div className="rounded-lg border px-3 py-2 text-[12px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>
          {error}
        </div>
      )}
      <textarea
        data-testid="research-question-input"
        value={question}
        onChange={e => setQuestion(e.target.value)}
        placeholder="Enter your research question…"
        rows={3}
        className="w-full px-2.5 py-1.5 rounded-lg border text-[13px] resize-none"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      />
      <select
        value={depth}
        onChange={e => setDepth(e.target.value as typeof depth)}
        className="w-full px-2 py-1.5 rounded-lg border text-[13px]"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      >
        <option value="quick">Quick</option>
        <option value="standard">Standard</option>
        <option value="exhaustive">Exhaustive</option>
        <option value="trusted">Trusted</option>
      </select>

      <div className="flex flex-col gap-1 text-[12.5px]">
        <div className="font-medium" style={{ color: "var(--text-dim)" }}>Sources</div>
        {([
          ["web", "Web", webScope, setWebScope],
          ["project", "Project files", projectScope, setProjectScope],
          ["connector", "Connectors", connectorScope, setConnectorScope],
          ["upload", "Uploads", uploadScope, setUploadScope],
          ["mcp", "MCP", mcpScope, setMcpScope],
        ] as [string, string, boolean, (v: boolean) => void][]).map(([key, label, checked, setter]) => (
          <label key={key} className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={checked} onChange={e => setter(e.target.checked)} />
            {label}
          </label>
        ))}
        {projectScope && (
          <select
            value={selectedProject}
            onChange={e => setSelectedProject(e.target.value)}
            className="w-full px-2 py-1.5 rounded-lg border text-[12px] mt-1"
            style={{ borderColor: "var(--border)", background: "var(--surface)" }}
          >
            <option value="">— Select project —</option>
            {projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        )}
      </div>

      <input
        type="number"
        value={timeBudget}
        onChange={e => setTimeBudget(e.target.value)}
        placeholder="Time budget (seconds, optional)"
        className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      />

      <div className="flex gap-2">
        <button
          data-testid="research-start"
          onClick={submit}
          disabled={busy || !question.trim()}
          className="btn btn-primary btn-sm flex-1"
        >
          {busy ? "Starting…" : "Start research"}
        </button>
        <button onClick={onCancel} disabled={busy} className="btn btn-ghost btn-sm">
          Cancel
        </button>
      </div>
    </div>
  );
}

// ─── Run Detail ───────────────────────────────────────────────────────────────
function RunDetail({ runId, onCancelled }: { runId: string; onCancelled: () => void }) {
  const [run, setRun] = useState<ResearchRun | null>(null);
  const [events, setEvents] = useState<ResearchEvent[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reportText, setReportText] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const lastSeqRef = useRef(0);
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadRun = useCallback(async () => {
    try {
      const res = await apiFetch(`/research/${runId}`);
      const data: ResearchRun = await res.json();
      setRun(data);
      return data;
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load run.");
      return null;
    }
  }, [runId]);

  const loadEvents = useCallback(async () => {
    try {
      const res = await apiFetch(`/research/${runId}/events?after_seq=${lastSeqRef.current}`);
      const newEvents: ResearchEvent[] = await res.json();
      if (newEvents.length > 0) {
        setEvents(prev => [...prev, ...newEvents]);
        lastSeqRef.current = newEvents[newEvents.length - 1].seq;
      }
    } catch {
      // silent
    }
  }, [runId]);

  const loadCitations = useCallback(async () => {
    try {
      const res = await apiFetch(`/research/${runId}/citations`);
      const data: Citation[] = await res.json();
      setCitations(data);
    } catch {
      // silent
    }
  }, [runId]);

  // Polling loop
  useEffect(() => {
    let cancelled = false;

    async function poll() {
      const current = await loadRun();
      await loadEvents();
      if (current && TERMINAL.has(current.status)) {
        void loadCitations();
        return; // stop polling
      }
      if (!cancelled) {
        pollRef.current = setTimeout(poll, 1500);
      }
    }

    void poll();

    return () => {
      cancelled = true;
      if (pollRef.current) clearTimeout(pollRef.current);
    };
  }, [runId, loadRun, loadEvents, loadCitations]);

  async function cancel() {
    setCancelling(true);
    try {
      await apiFetch(`/research/${runId}/cancel`, { method: "POST" });
      const updated = await loadRun();
      if (updated && TERMINAL.has(updated.status)) {
        if (pollRef.current) clearTimeout(pollRef.current);
      }
      onCancelled();
    } catch {
      // silent
    } finally {
      setCancelling(false);
    }
  }

  async function openReport() {
    if (!run?.report_artifact_id) return;
    setReportLoading(true);
    try {
      const text = await getContentText(run.report_artifact_id);
      setReportText(text);
    } catch {
      setReportText("(Could not load report)");
    } finally {
      setReportLoading(false);
    }
  }

  if (loadError) {
    return <div className="p-6 text-[14px]" style={{ color: "var(--danger)" }}>{loadError}</div>;
  }
  if (!run) {
    return <div className="p-6 text-[14px]" style={{ color: "var(--text-dim)" }}>Loading…</div>;
  }

  const isTerminal = TERMINAL.has(run.status);
  const isRunning = !isTerminal;

  return (
    <div className="flex flex-col h-full min-h-0 overflow-auto">
      {/* Header */}
      <header className="px-5 py-3 border-b flex items-start gap-3" style={{ borderColor: "var(--border)" }}>
        <div className="flex-1 min-w-0">
          <div className="text-[15px] font-semibold">{run.question}</div>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <span
              data-testid="research-status"
              className="inline-block rounded px-2 py-0.5 text-[11.5px] font-semibold"
              style={{ background: statusBg(run.status), color: statusColor(run.status) }}
            >
              {run.status}
            </span>
            <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>depth: {run.depth}</span>
            {run.completed_at && (
              <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>
                completed {new Date(run.completed_at).toLocaleString()}
              </span>
            )}
          </div>
        </div>
        {isRunning && (
          <button
            onClick={cancel}
            disabled={cancelling}
            className="btn btn-ghost btn-sm"
            style={{ color: "var(--danger)" }}
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        )}
      </header>

      <div className="flex-1 overflow-auto p-5 flex flex-col gap-5">
        {/* Limitations callout — always visible when non-empty */}
        {run.limitations && (
          <div
            className="rounded-lg border px-4 py-3 text-[13px]"
            style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}
          >
            <div className="font-semibold mb-1">Limitations</div>
            <pre className="whitespace-pre-wrap font-sans">{run.limitations}</pre>
          </div>
        )}

        {/* Error */}
        {run.error && (
          <div
            className="rounded-lg border px-4 py-3 text-[13px]"
            style={{ borderColor: "var(--danger)", color: "var(--danger)" }}
          >
            <div className="font-semibold mb-1">Error</div>
            {run.error}
          </div>
        )}

        {/* Report */}
        {run.report_artifact_id && (
          <div>
            {reportText == null ? (
              <button
                data-testid="research-open-report"
                onClick={openReport}
                disabled={reportLoading}
                className="btn btn-secondary btn-sm"
              >
                {reportLoading ? "Loading report…" : "Open report"}
              </button>
            ) : (
              <div>
                <div className="flex items-center gap-2 mb-2">
                  <div className="text-[13px] font-semibold">Report</div>
                  <button
                    onClick={() => setReportText(null)}
                    className="btn btn-ghost btn-sm text-[11px]"
                  >
                    Close
                  </button>
                </div>
                <pre
                  data-testid="research-report-body"
                  className="text-[12.5px] font-mono overflow-auto p-4 rounded-lg whitespace-pre-wrap"
                  style={{ background: "var(--surface)", color: "var(--text)", maxHeight: "400px" }}
                >
                  {reportText}
                </pre>
              </div>
            )}
          </div>
        )}

        {/* Findings summary */}
        {run.findings?.summary && (
          <div>
            <div className="text-[13px] font-semibold mb-1">Summary</div>
            <div className="text-[13px]" style={{ color: "var(--text)" }}>{run.findings.summary}</div>
            {run.findings.citation_count != null && (
              <div className="text-[12px] mt-1" style={{ color: "var(--text-dim)" }}>
                {run.findings.citation_count} citations
              </div>
            )}
          </div>
        )}

        {/* Live event timeline */}
        <div>
          <div className="text-[13px] font-semibold mb-2 flex items-center gap-2">
            Timeline
            {isRunning && (
              <span className="inline-block w-2 h-2 rounded-full animate-pulse" style={{ background: "var(--accent)" }} />
            )}
          </div>
          {events.length === 0 && isRunning && (
            <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>Waiting for events…</div>
          )}
          {events.length === 0 && isTerminal && (
            <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>No events recorded.</div>
          )}
          <div className="flex flex-col gap-1">
            {events.map(ev => (
              <div
                key={ev.id}
                className="text-[12px] px-3 py-2 rounded-lg border"
                style={{ borderColor: "var(--border)", background: "var(--surface)" }}
              >
                <span className="font-semibold">{ev.event_type}</span>
                {Object.keys(ev.payload).length > 0 && (
                  <span className="ml-2" style={{ color: "var(--text-dim)" }}>
                    {Object.entries(ev.payload)
                      .slice(0, 3)
                      .map(([k, v]) => `${k}: ${typeof v === "string" ? v.slice(0, 80) : JSON.stringify(v).slice(0, 80)}`)
                      .join("  ·  ")}
                  </span>
                )}
                <span className="ml-2 text-[10.5px]" style={{ color: "var(--text-dim)" }}>
                  seq {ev.seq}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Citations table */}
        {citations.length > 0 && (
          <div>
            <div className="text-[13px] font-semibold mb-2">Citations</div>
            <div className="overflow-x-auto rounded-lg border" style={{ borderColor: "var(--border)" }}>
              <table className="w-full text-[12px]">
                <thead>
                  <tr style={{ background: "var(--surface-2, var(--surface))" }}>
                    {["Marker", "Type", "Title", "URL", "Snippet"].map(h => (
                      <th key={h} className="text-left px-3 py-2 font-semibold" style={{ color: "var(--text-dim)" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {citations.map(c => (
                    <tr key={c.id} className="border-t" style={{ borderColor: "var(--border)" }}>
                      <td className="px-3 py-2 font-mono">{c.marker}</td>
                      <td className="px-3 py-2">{c.source_type}</td>
                      <td className="px-3 py-2 max-w-[160px] truncate">{c.source_title ?? "—"}</td>
                      <td className="px-3 py-2 max-w-[160px] truncate">
                        {c.url
                          ? <a href={c.url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>{c.url}</a>
                          : "—"}
                      </td>
                      <td className="px-3 py-2 max-w-[220px] truncate" style={{ color: "var(--text-dim)" }}>
                        {c.snippet ? c.snippet.slice(0, 100) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
