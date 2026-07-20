"use client";
import { type KeyboardEvent as ReactKeyboardEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  Artifact, ArtifactPreview, ArtifactVersion, DiffResult,
  aiEditArtifact, deleteArtifact, duplicateArtifact, editArtifact, getArtifact, getContentBlob, getContentText,
  getDiff, getPreview, getPreviewPageBlob, getShareStatus, listArtifacts, listVersions,
  publishArtifact, renameArtifact, restoreVersion, unpublishArtifact,
} from "../../lib/artifacts";
import { ArtifactRenderer } from "./ArtifactRenderer";
import { CommentsThread } from "../collaboration/CommentsThread";
import type { CollaborationIdentity } from "../../lib/collaboration";

type Tab = "preview" | "edit" | "versions" | "diff" | "comments";
const ROLE_RANK: Record<string, number> = { viewer: 1, operator: 2, user: 2, approver: 2, manager: 3, admin: 4, owner: 5 };

function isEditableTextArtifact(artifact: Artifact | null): boolean {
  if (!artifact) return false;
  const mime = (artifact.mime_type ?? "").toLowerCase();
  return mime.startsWith("text/") || mime.includes("json") || mime.includes("csv") || ["markdown", "code", "data", "csv", "html", "react", "diagram", "mermaid"].includes(artifact.kind);
}

export default function ArtifactsScreen({ memberRole = "viewer", currentMember }: { memberRole?: string; currentMember?: CollaborationIdentity }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("artifact");
  });
  const [kindFilter, setKindFilter] = useState<string>("");
  const [query, setQuery] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try { setArtifacts(await listArtifacts()); }
    catch (err) { setLoadError(err instanceof Error ? err.message : "Unable to load artifacts."); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return artifacts.filter(a => (!kindFilter || a.kind === kindFilter) && (!q || (a.title ?? "").toLowerCase().includes(q) || a.kind.includes(q)));
  }, [artifacts, kindFilter, query]);

  const kinds = useMemo(() => Array.from(new Set(artifacts.map(a => a.kind))).sort(), [artifacts]);

  const grouped = useMemo(() => {
    const map = new Map<string, Artifact[]>();
    for (const a of filtered) {
      const key = a.conversation_id ? `Conversation ${a.conversation_id.slice(0, 8)}`
        : a.task_id ? `Task ${a.task_id.slice(0, 8)}`
        : "Ungrouped";
      (map.get(key) ?? map.set(key, []).get(key)!).push(a);
    }
    return Array.from(map.entries());
  }, [filtered]);

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      <aside aria-label="Artifact library" className="h-[240px] w-full flex-shrink-0 border-b flex flex-col md:h-auto md:w-[320px] md:border-b-0 md:border-r" style={{ borderColor: "var(--border)" }}>
        <div className="p-3 border-b flex flex-col gap-2" style={{ borderColor: "var(--border)" }}>
          <div className="text-[15px] font-semibold">Artifacts</div>
          <input aria-label="Search artifacts" value={query} onChange={e => setQuery(e.target.value)} placeholder="Search artifacts…"
                 className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
          <select aria-label="Filter artifacts by type" value={kindFilter} onChange={e => setKindFilter(e.target.value)}
                  className="w-full px-2 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>
            <option value="">All types</option>
            {kinds.map(k => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <div className="flex-1 overflow-auto p-2">
          {loading && <div role="status" className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading artifacts…</div>}
          {!loading && loadError && <div role="alert" className="text-[12.5px] p-3 m-1 rounded-lg border" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>Couldn’t load artifacts: {loadError}</div>}
          {!loading && !loadError && filtered.length === 0 && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No artifacts yet.</div>}
          {grouped.map(([label, items]) => (
            <div key={label} className="mb-2">
              <div className="px-2 py-1 text-[10.5px] font-semibold uppercase tracking-wide" style={{ color: "var(--text-dim)" }}>{label}</div>
              {items.map(a => (
                <button key={a.id} onClick={() => setSelectedId(a.id)}
                        aria-pressed={selectedId === a.id}
                        className={`w-full text-left px-3 py-2 rounded-lg mb-1 ${selectedId === a.id ? "active" : ""}`}
                        style={{ background: selectedId === a.id ? "var(--accent-soft)" : "transparent" }}>
                  <div className="text-[13.5px] font-medium truncate">{a.title ?? "Untitled"}</div>
                  <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{a.kind} · v{a.version}</div>
                </button>
              ))}
            </div>
          ))}
        </div>
      </aside>
      <section className="flex-1 min-w-0">
        {selectedId
          ? <ArtifactDetail key={selectedId} memberRole={memberRole} currentMember={currentMember} artifactId={selectedId} onChanged={refresh} onDeleted={() => { setSelectedId(null); void refresh(); }} />
          : <div className="h-full flex items-center justify-center px-6 text-center text-[14px]" style={{ color: "var(--text-dim)" }}>Select an artifact to preview or review its versions.</div>}
      </section>
    </div>
  );
}

export function ArtifactDetail({ artifactId, onChanged, onDeleted, memberRole = "viewer", currentMember }: { artifactId: string; onChanged: () => void; onDeleted: () => void; memberRole?: string; currentMember?: CollaborationIdentity }) {
  const roleRank = ROLE_RANK[memberRole] ?? 0;
  const canCreate = roleRank >= ROLE_RANK.operator;
  const canEdit = roleRank >= ROLE_RANK.manager;
  const canPublish = roleRank >= ROLE_RANK.admin;
  const [meta, setMeta] = useState<Artifact | null>(null);
  const [tab, setTab] = useState<Tab>("preview");
  const [text, setText] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [draft, setDraft] = useState("");
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [share, setShare] = useState<{ published: boolean; share_path?: string; expires_at?: string | null }>({ published: false });
  const [shareDurationHours, setShareDurationHours] = useState(168);
  const [aiInstruction, setAiInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const isText = useMemo(() => {
    return isEditableTextArtifact(meta);
  }, [meta]);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const m = await getArtifact(artifactId);
      setMeta(m);
      const nextPreview = await getPreview(artifactId);
      setPreview(nextPreview);
      if (canPublish) {
        void getShareStatus(artifactId).then(setShare).catch(() => setShare({ published: false }));
      } else {
        setShare({ published: false });
      }
      if (nextPreview.renderer === "image") {
        const blob = await getContentBlob(artifactId);
        setBlobUrl((current) => {
          if (current) URL.revokeObjectURL(current);
          return URL.createObjectURL(blob);
        });
        setText(null);
      } else if (isEditableTextArtifact(m)) {
        const source = await getContentText(artifactId);
        setText(nextPreview.text ?? source);
        setDraft(source);
      } else {
        setText(nextPreview.text ?? null);
        setDraft("");
        setBlobUrl((current) => {
          if (current) URL.revokeObjectURL(current);
          return null;
        });
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Artifact could not be loaded.");
    }
  }, [artifactId, canPublish]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => { if (blobUrl) URL.revokeObjectURL(blobUrl); }, [blobUrl]);

  const loadPdfPage = useCallback(async (page: number) => {
    const blob = await getPreviewPageBlob(artifactId, page);
    return URL.createObjectURL(blob);
  }, [artifactId]);

  const loadVersions = useCallback(async () => {
    const vs = await listVersions(artifactId);
    setVersions(vs);
    if (vs.length >= 2) setDiff(await getDiff(artifactId, vs[1].version, vs[0].version));
    else setDiff(null);
  }, [artifactId]);

  useEffect(() => { if (tab === "versions" || tab === "diff") void loadVersions(); }, [tab, loadVersions]);

  // Live versioning: when the agent writes a new version of the artifact that's
  // open, refresh the preview/version list in place (Claude-style), but never
  // clobber an in-progress manual edit.
  useEffect(() => {
    function onUpdated(e: Event) {
      const id = (e as CustomEvent<{ id: string }>).detail?.id;
      if (id !== artifactId || tab === "edit") return;
      void load();
      if (tab === "versions" || tab === "diff") void loadVersions();
    }
    window.addEventListener("chronos:artifact-updated", onUpdated as EventListener);
    return () => window.removeEventListener("chronos:artifact-updated", onUpdated as EventListener);
  }, [artifactId, tab, load, loadVersions]);

  async function save() { setBusy(true); setActionError(null); try { await editArtifact(artifactId, draft, "manual edit"); await load(); onChanged(); setTab("preview"); } catch (error) { setActionError(error instanceof Error ? error.message : "Artifact could not be saved."); } finally { setBusy(false); } }
  async function aiEdit() { if (!aiInstruction.trim()) return; setBusy(true); setActionError(null); try { await aiEditArtifact(artifactId, aiInstruction); setAiInstruction(""); await load(); onChanged(); setTab("preview"); } catch (error) { setActionError(error instanceof Error ? error.message : "AI edit could not be completed."); } finally { setBusy(false); } }
  async function restore(v: number) { if (!confirm(`Restore version ${v} as the current artifact? A new version will preserve the existing content.`)) return; setBusy(true); setActionError(null); try { await restoreVersion(artifactId, v); await load(); await loadVersions(); onChanged(); } catch (error) { setActionError(error instanceof Error ? error.message : "Version could not be restored."); } finally { setBusy(false); } }
  async function duplicate() { setBusy(true); setActionError(null); try { await duplicateArtifact(artifactId); onChanged(); } catch (error) { setActionError(error instanceof Error ? error.message : "Artifact could not be duplicated."); } finally { setBusy(false); } }
  async function rename() { const t = prompt("Rename artifact", meta?.title ?? ""); if (t != null && t.trim()) { setBusy(true); setActionError(null); try { await renameArtifact(artifactId, t.trim()); await load(); onChanged(); } catch (error) { setActionError(error instanceof Error ? error.message : "Artifact could not be renamed."); } finally { setBusy(false); } } }
  async function remove() { if (confirm("Delete this artifact?")) { setBusy(true); setActionError(null); try { await deleteArtifact(artifactId); onDeleted(); } catch (error) { setActionError(error instanceof Error ? error.message : "Artifact could not be deleted."); } finally { setBusy(false); } } }
  async function download() {
    setActionError(null);
    try {
      const blob = await getContentBlob(artifactId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = meta?.title?.trim() || `chronos-artifact-${artifactId}`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 5_000);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Artifact could not be downloaded.");
    }
  }
  async function togglePublish() {
    setBusy(true);
    setActionError(null);
    try {
      if (share.published) { await unpublishArtifact(artifactId); }
      else {
        const duration = shareDurationHours === 24 ? "24 hours" : shareDurationHours === 168 ? "7 days" : "30 days";
        if (!confirm(`Create a public link that anyone can open for ${duration}? Do not share it with people who should not access this artifact.`)) return;
        await publishArtifact(artifactId, shareDurationHours);
      }
      setShare(await getShareStatus(artifactId));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Publication status could not be changed.");
    } finally { setBusy(false); }
  }

  if (!meta) return (
    <div className="p-6 text-[14px]" style={{ color: loadError ? "var(--danger)" : "var(--text-dim)" }}>
      {loadError ?? "Loading..."}
    </div>
  );
  const shareUrl = share.share_path && typeof window !== "undefined" ? `${window.location.origin}${share.share_path}` : "";
  const artifactTabs: Tab[] = ["preview", "edit", "versions", "diff", ...(currentMember ? ["comments" as const] : [])];

  function handleTabKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not([disabled])')];
    if (tabs.length === 0) return;
    event.preventDefault();
    const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
    const next = event.key === "Home"
      ? tabs[0]
      : event.key === "End"
        ? tabs[tabs.length - 1]
        : event.key === "ArrowRight"
          ? tabs[(current + 1 + tabs.length) % tabs.length]
          : tabs[(current - 1 + tabs.length) % tabs.length];
    next.focus();
    next.click();
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-3 sm:gap-3 sm:px-5" style={{ borderColor: "var(--border)" }}>
        <div className="flex-1 min-w-0">
          <div className="text-[15px] font-semibold truncate">{meta.title ?? "Untitled"}</div>
          <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{meta.kind} · v{meta.version}{meta.mime_type ? ` · ${meta.mime_type}` : ""}</div>
        </div>
        {canEdit && <button onClick={rename} disabled={busy} className="btn btn-ghost btn-sm">Rename</button>}
        <button onClick={() => void download()} disabled={busy} className="btn btn-ghost btn-sm">Download</button>
        {canCreate && <button onClick={duplicate} disabled={busy} className="btn btn-ghost btn-sm">Duplicate</button>}
        {canPublish && !share.published && (
          <select
            aria-label="Public link duration"
            value={shareDurationHours}
            onChange={event => setShareDurationHours(Number(event.target.value))}
            className="surface rounded-lg border border-soft px-2 py-1.5 text-[12px]"
          >
            <option value={24}>24 hours</option>
            <option value={168}>7 days</option>
            <option value={720}>30 days</option>
          </select>
        )}
        {canPublish && <button onClick={togglePublish} disabled={busy} className="btn btn-secondary btn-sm">{share.published ? "Revoke link" : "Create public link"}</button>}
        {canEdit && <button onClick={remove} disabled={busy} className="btn btn-ghost btn-sm">Delete</button>}
      </header>

      {share.published && shareUrl && (
        <div className="flex items-center gap-2 border-b px-4 py-2 text-[12px] sm:px-5" style={{ borderColor: "var(--border)", background: "var(--accent-soft)" }}>
          <span>Anyone with this link can view and download until {share.expires_at ? new Date(share.expires_at).toLocaleString() : "it expires"}.</span>
          <code className="truncate flex-1">{shareUrl}</code>
          <button className="btn btn-ghost btn-sm" onClick={() => navigator.clipboard?.writeText(shareUrl)}>Copy</button>
        </div>
      )}

      <nav role="tablist" aria-label="Artifact views" onKeyDown={handleTabKeyDown} className="no-scrollbar flex gap-1 overflow-x-auto border-b px-4 pt-2 sm:px-5" style={{ borderColor: "var(--border)" }}>
        {artifactTabs.map(t => (
          <button key={t} onClick={() => setTab(t)} disabled={t === "edit" && (!isText || !canEdit)}
                  id={`artifact-tab-${t}`}
                  role="tab"
                  aria-selected={tab === t}
                  aria-controls={`artifact-panel-${t}`}
                  tabIndex={tab === t ? 0 : -1}
                  className={`px-3 py-1.5 text-[13px] rounded-t-lg ${tab === t ? "font-semibold" : ""}`}
                  title={t === "edit" && !canEdit ? "Manager role required to edit artifacts" : undefined}
                  style={{ borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent", opacity: t === "edit" && (!isText || !canEdit) ? 0.4 : 1 }}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      <div id={`artifact-panel-${tab}`} role="tabpanel" aria-labelledby={`artifact-tab-${tab}`} tabIndex={0} className="flex-1 overflow-auto p-4 sm:p-5">
        {!canEdit && (
          <div className="mb-3 rounded-lg border border-soft px-3 py-2 text-[13px]" style={{ color: "var(--text-dim)" }}>
            Your role can preview this artifact and its version history. Editing and deletion require a manager role; public sharing requires an administrator.
          </div>
        )}
        {actionError && (
          <div className="mb-3 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }} role="alert">
            {actionError}
          </div>
        )}
        {loadError && (
          <div className="mb-3 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }} role="alert">
            {loadError}
          </div>
        )}
        {tab === "preview" && <ArtifactRenderer kind={meta.kind} mimeType={meta.mime_type} content={text} blobUrl={blobUrl} title={meta.title} preview={preview} previewPageLoader={preview?.renderer === "pdf" ? loadPdfPage : undefined} />}

        {tab === "edit" && isText && (
          <div className="flex flex-col gap-3 h-full">
            <textarea aria-label="Artifact editor" value={draft} onChange={e => setDraft(e.target.value)}
                      className="flex-1 min-h-[280px] w-full font-mono text-[12.5px] p-3 rounded-lg border" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
            <div className="flex flex-col gap-2 sm:flex-row">
              <button onClick={save} disabled={busy} className="btn btn-primary btn-sm">Save new version</button>
              <input aria-label="AI edit instruction" value={aiInstruction} onChange={e => setAiInstruction(e.target.value)} placeholder="Ask AI to edit…"
                     className="flex-1 px-2.5 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
              <button onClick={aiEdit} disabled={busy || !aiInstruction.trim()} className="btn btn-secondary btn-sm">AI edit</button>
            </div>
          </div>
        )}

        {tab === "versions" && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col items-start justify-between gap-3 rounded-lg border px-3 py-2 sm:flex-row sm:items-center" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>
              <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>Use Diff for a focused audit view of the latest version change.</div>
              <button className="btn btn-secondary btn-sm" onClick={() => setTab("diff")}>Open diff</button>
            </div>
            <div className="flex flex-col gap-1">
              {versions.map(v => (
                <div key={v.id} className="flex flex-wrap items-center gap-3 px-3 py-2 rounded-lg border" style={{ borderColor: "var(--border)" }}>
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-medium">v{v.version} {v.version === meta.version ? "(current)" : ""}</div>
                    <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{v.edit_summary ?? ""} · {new Date(v.created_at).toLocaleString()}</div>
                  </div>
                  {v.version !== meta.version && canEdit && <button onClick={() => restore(v.version)} disabled={busy} className="btn btn-ghost btn-sm">Restore</button>}
                </div>
              ))}
            </div>
          </div>
        )}

        {tab === "diff" && (
          <div className="h-full flex flex-col gap-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-[13px] font-semibold">Latest version diff</div>
                <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>
                  {diff ? `Comparing v${diff.from_version} to v${diff.to_version}` : "At least two versions are required."}
                </div>
              </div>
              <button className="btn btn-ghost btn-sm" onClick={() => setTab("versions")}>Version history</button>
            </div>
            {diff && !diff.is_binary ? (
              <pre className="flex-1 min-h-[320px] text-[12px] overflow-auto p-3 rounded-lg font-mono border" style={{ background: "var(--surface)", borderColor: "var(--border)" }}>{diff.diff || "(no changes)"}</pre>
            ) : (
              <div className="rounded-lg border p-4 text-[13px]" style={{ borderColor: "var(--border)", color: "var(--text-dim)" }}>
                {diff?.is_binary ? "Binary artifacts cannot be shown as a text diff." : "No diff is available yet."}
              </div>
            )}
          </div>
        )}

        {tab === "comments" && currentMember && (
          <CommentsThread targetType="artifact" targetId={artifactId} currentMember={currentMember} />
        )}
      </div>
    </div>
  );
}
