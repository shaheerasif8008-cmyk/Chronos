"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { apiBase } from "../../lib/api";
import {
  Artifact, ArtifactVersion, DiffResult,
  aiEditArtifact, deleteArtifact, duplicateArtifact, editArtifact, getArtifact, getContentBlob, getContentText,
  getDiff, getShareStatus, listArtifacts, listVersions,
  publishArtifact, renameArtifact, restoreVersion, unpublishArtifact,
} from "../../lib/artifacts";
import { ArtifactRenderer } from "./ArtifactRenderer";

type Tab = "preview" | "edit" | "versions" | "diff";

export default function ArtifactsScreen() {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<string>("");
  const [query, setQuery] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try { setArtifacts(await listArtifacts(kindFilter ? { kind: kindFilter } : {})); }
    catch (err) { setLoadError(err instanceof Error ? err.message : "Unable to load artifacts."); }
    finally { setLoading(false); }
  }, [kindFilter]);

  useEffect(() => { void refresh(); }, [refresh]);

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    return artifacts.filter(a => !q || (a.title ?? "").toLowerCase().includes(q) || a.kind.includes(q));
  }, [artifacts, query]);

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
    <div className="flex h-full min-h-0">
      <aside className="w-[320px] flex-shrink-0 border-r flex flex-col" style={{ borderColor: "var(--border)" }}>
        <div className="p-3 border-b flex flex-col gap-2" style={{ borderColor: "var(--border)" }}>
          <div className="text-[15px] font-semibold">Artifacts</div>
          <input value={query} onChange={e => setQuery(e.target.value)} placeholder="Search artifacts…"
                 className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
          <select value={kindFilter} onChange={e => setKindFilter(e.target.value)}
                  className="w-full px-2 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>
            <option value="">All types</option>
            {kinds.map(k => <option key={k} value={k}>{k}</option>)}
          </select>
        </div>
        <div className="flex-1 overflow-auto p-2">
          {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading…</div>}
          {!loading && loadError && <div className="text-[12.5px] p-3 m-1 rounded-lg border" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>Couldn’t load artifacts: {loadError}</div>}
          {!loading && !loadError && filtered.length === 0 && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No artifacts yet.</div>}
          {grouped.map(([label, items]) => (
            <div key={label} className="mb-2">
              <div className="px-2 py-1 text-[10.5px] font-semibold uppercase tracking-wide" style={{ color: "var(--text-dim)" }}>{label}</div>
              {items.map(a => (
                <button key={a.id} onClick={() => setSelectedId(a.id)}
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
          ? <ArtifactDetail key={selectedId} artifactId={selectedId} onChanged={refresh} onDeleted={() => { setSelectedId(null); void refresh(); }} />
          : <div className="h-full flex items-center justify-center text-[14px]" style={{ color: "var(--text-dim)" }}>Select an artifact to preview, edit, version, or publish.</div>}
      </section>
    </div>
  );
}

export function ArtifactDetail({ artifactId, onChanged, onDeleted }: { artifactId: string; onChanged: () => void; onDeleted: () => void }) {
  const [meta, setMeta] = useState<Artifact | null>(null);
  const [tab, setTab] = useState<Tab>("preview");
  const [text, setText] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [share, setShare] = useState<{ published: boolean; share_path?: string }>({ published: false });
  const [aiInstruction, setAiInstruction] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const isText = useMemo(() => {
    const m = (meta?.mime_type ?? "").toLowerCase();
    return !m.startsWith("image/") && !m.includes("octet-stream") && !m.includes("pdf");
  }, [meta]);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const m = await getArtifact(artifactId);
      setMeta(m);
      void getShareStatus(artifactId).then(setShare).catch(() => setShare({ published: false }));
      const mime = (m.mime_type ?? "").toLowerCase();
      if (mime.startsWith("image/")) {
        const blob = await getContentBlob(artifactId);
        setBlobUrl(URL.createObjectURL(blob));
        setText(null);
      } else {
        try {
          const t = await getContentText(artifactId);
          setText(t);
          setDraft(t);
        } catch (err) {
          setText(null);
          setLoadError(err instanceof Error ? err.message : "Artifact content could not be loaded.");
        }
      }
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Artifact could not be loaded.");
    }
  }, [artifactId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => () => { if (blobUrl) URL.revokeObjectURL(blobUrl); }, [blobUrl]);

  const loadVersions = useCallback(async () => {
    const vs = await listVersions(artifactId);
    setVersions(vs);
    if (vs.length >= 2) setDiff(await getDiff(artifactId, vs[1].version, vs[0].version));
    else setDiff(null);
  }, [artifactId]);

  useEffect(() => { if (tab === "versions" || tab === "diff") void loadVersions(); }, [tab, loadVersions]);

  async function save() { setBusy(true); try { await editArtifact(artifactId, draft, "manual edit"); await load(); onChanged(); setTab("preview"); } finally { setBusy(false); } }
  async function aiEdit() { if (!aiInstruction.trim()) return; setBusy(true); try { await aiEditArtifact(artifactId, aiInstruction); setAiInstruction(""); await load(); onChanged(); setTab("preview"); } finally { setBusy(false); } }
  async function restore(v: number) { setBusy(true); try { await restoreVersion(artifactId, v); await load(); await loadVersions(); onChanged(); } finally { setBusy(false); } }
  async function duplicate() { setBusy(true); try { await duplicateArtifact(artifactId); onChanged(); } finally { setBusy(false); } }
  async function rename() { const t = prompt("Rename artifact", meta?.title ?? ""); if (t != null) { await renameArtifact(artifactId, t); await load(); onChanged(); } }
  async function remove() { if (confirm("Delete this artifact?")) { await deleteArtifact(artifactId); onDeleted(); } }
  async function togglePublish() {
    setBusy(true);
    try {
      if (share.published) { await unpublishArtifact(artifactId); }
      else { await publishArtifact(artifactId); }
      setShare(await getShareStatus(artifactId));
    } finally { setBusy(false); }
  }

  if (!meta) return (
    <div className="p-6 text-[14px]" style={{ color: loadError ? "var(--danger)" : "var(--text-dim)" }}>
      {loadError ?? "Loading..."}
    </div>
  );
  const shareUrl = share.share_path ? `${apiBase()}${share.share_path}` : "";

  return (
    <div className="flex flex-col h-full min-h-0">
      <header className="px-5 py-3 border-b flex items-center gap-3" style={{ borderColor: "var(--border)" }}>
        <div className="flex-1 min-w-0">
          <div className="text-[15px] font-semibold truncate">{meta.title ?? "Untitled"}</div>
          <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{meta.kind} · v{meta.version}{meta.mime_type ? ` · ${meta.mime_type}` : ""}</div>
        </div>
        <button onClick={rename} className="btn btn-ghost btn-sm">Rename</button>
        <button onClick={duplicate} disabled={busy} className="btn btn-ghost btn-sm">Duplicate</button>
        <button onClick={togglePublish} disabled={busy} className="btn btn-secondary btn-sm">{share.published ? "Unpublish" : "Publish"}</button>
        <button onClick={remove} className="btn btn-ghost btn-sm">Delete</button>
      </header>

      {share.published && shareUrl && (
        <div className="px-5 py-2 text-[12px] flex items-center gap-2 border-b" style={{ borderColor: "var(--border)", background: "var(--accent-soft)" }}>
          <span>Public link:</span>
          <code className="truncate flex-1">{shareUrl}</code>
          <button className="btn btn-ghost btn-sm" onClick={() => navigator.clipboard?.writeText(shareUrl)}>Copy</button>
        </div>
      )}

      <nav className="px-5 pt-2 flex gap-1 border-b" style={{ borderColor: "var(--border)" }}>
        {(["preview", "edit", "versions", "diff"] as Tab[]).map(t => (
          <button key={t} onClick={() => setTab(t)} disabled={t === "edit" && !isText}
                  className={`px-3 py-1.5 text-[13px] rounded-t-lg ${tab === t ? "font-semibold" : ""}`}
                  style={{ borderBottom: tab === t ? "2px solid var(--accent)" : "2px solid transparent", opacity: t === "edit" && !isText ? 0.4 : 1 }}>
            {t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>

      <div className="flex-1 overflow-auto p-5">
        {loadError && (
          <div className="mb-3 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>
            {loadError}
          </div>
        )}
        {tab === "preview" && <ArtifactRenderer kind={meta.kind} mimeType={meta.mime_type} content={text} blobUrl={blobUrl} title={meta.title} />}

        {tab === "edit" && isText && (
          <div className="flex flex-col gap-3 h-full">
            <textarea value={draft} onChange={e => setDraft(e.target.value)}
                      className="flex-1 min-h-[280px] w-full font-mono text-[12.5px] p-3 rounded-lg border" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
            <div className="flex gap-2">
              <button onClick={save} disabled={busy} className="btn btn-primary btn-sm">Save new version</button>
              <input value={aiInstruction} onChange={e => setAiInstruction(e.target.value)} placeholder="Ask AI to edit…"
                     className="flex-1 px-2.5 py-1.5 rounded-lg border text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }} />
              <button onClick={aiEdit} disabled={busy || !aiInstruction.trim()} className="btn btn-secondary btn-sm">AI edit</button>
            </div>
          </div>
        )}

        {tab === "versions" && (
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between rounded-lg border px-3 py-2" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>
              <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>Use Diff for a focused audit view of the latest version change.</div>
              <button className="btn btn-secondary btn-sm" onClick={() => setTab("diff")}>Open diff</button>
            </div>
            <div className="flex flex-col gap-1">
              {versions.map(v => (
                <div key={v.id} className="flex items-center gap-3 px-3 py-2 rounded-lg border" style={{ borderColor: "var(--border)" }}>
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-medium">v{v.version} {v.version === meta.version ? "(current)" : ""}</div>
                    <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{v.edit_summary ?? ""} · {new Date(v.created_at).toLocaleString()}</div>
                  </div>
                  {v.version !== meta.version && <button onClick={() => restore(v.version)} disabled={busy} className="btn btn-ghost btn-sm">Restore</button>}
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
      </div>
    </div>
  );
}
