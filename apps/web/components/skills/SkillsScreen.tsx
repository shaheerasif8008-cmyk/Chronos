"use client";
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";

// ─── Types ────────────────────────────────────────────────────────────────────
type Skill = {
  id: string;
  slug: string;
  name: string;
  description?: string | null;
  source?: string | null;
  current_version: number;
  requires_connectors?: string[] | null;
  spawns_sub_agent?: boolean | null;
};

type SkillDetail = Skill & {
  content?: string | null;
  version_metadata?: Record<string, unknown> | null;
};

type SkillVersion = {
  version: number;
  created_at?: string | null;
  created_by?: string | null;
};

// ─── Main Component ───────────────────────────────────────────────────────────
export default function SkillsScreen() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [composerOpen, setComposerOpen] = useState(false);

  const loadSkills = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch("/skills");
      const data: Skill[] = await res.json();
      setSkills(data.sort((a, b) => a.slug.localeCompare(b.slug)));
    } catch {
      // silent – list stays empty
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadSkills(); }, [loadSkills]);

  function onSaved(slug: string) {
    setComposerOpen(false);
    void loadSkills().then(() => setSelectedSlug(slug));
  }

  return (
    <div className="flex h-full min-h-0">
      {/* ── Left aside ── */}
      <aside className="w-[320px] flex-shrink-0 border-r flex flex-col" style={{ borderColor: "var(--border)" }}>
        <div className="p-3 border-b flex flex-col gap-2" style={{ borderColor: "var(--border)" }}>
          <div className="flex items-center justify-between">
            <div className="text-[15px] font-semibold">Skills</div>
            <button
              data-testid="skills-new"
              onClick={() => setComposerOpen(v => !v)}
              className="btn btn-primary btn-sm"
            >
              {composerOpen ? "Cancel" : "Upload / New version"}
            </button>
          </div>
          {composerOpen && (
            <SkillComposer onSaved={onSaved} onCancel={() => setComposerOpen(false)} />
          )}
        </div>
        <div className="flex-1 overflow-auto p-2">
          {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading…</div>}
          {!loading && skills.length === 0 && (
            <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No skills yet.</div>
          )}
          {skills.map(skill => (
            <button
              key={skill.id}
              onClick={() => setSelectedSlug(skill.slug)}
              className="w-full text-left px-3 py-2 rounded-lg mb-1"
              style={{ background: selectedSlug === skill.slug ? "var(--accent-soft)" : "transparent" }}
            >
              <div className="text-[13.5px] font-medium truncate">{skill.name}</div>
              <div className="text-[11.5px] flex items-center gap-2 mt-0.5" style={{ color: "var(--text-dim)" }}>
                <SourceBadge source={skill.source} />
                <span className="truncate">{skill.slug}</span>
                <span>v{skill.current_version}</span>
              </div>
            </button>
          ))}
        </div>
      </aside>

      {/* ── Detail pane ── */}
      <section className="flex-1 min-w-0">
        {selectedSlug
          ? <SkillDetailPane key={selectedSlug} slug={selectedSlug} onDeleted={() => { setSelectedSlug(null); void loadSkills(); }} />
          : <div className="h-full flex items-center justify-center text-[14px]" style={{ color: "var(--text-dim)" }}>
              Select a skill or upload a new one.
            </div>
        }
      </section>
    </div>
  );
}

function SourceBadge({ source }: { source?: string | null }) {
  const isFs = source === "filesystem";
  return (
    <span
      className="inline-block rounded px-1.5 py-0.5 text-[10.5px] font-semibold"
      style={{
        background: isFs ? "var(--surface)" : "var(--accent-soft)",
        color: isFs ? "var(--text-dim)" : "var(--accent)",
      }}
    >
      {isFs ? "filesystem" : "uploaded"}
    </span>
  );
}

// ─── Composer ────────────────────────────────────────────────────────────────
function SkillComposer({ onSaved, onCancel }: { onSaved: (slug: string) => void; onCancel: () => void }) {
  const [slug, setSlug] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [requiresConnectors, setRequiresConnectors] = useState("");
  const [spawnsSubAgent, setSpawnsSubAgent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!slug.trim() || !name.trim() || !content.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const metadata: Record<string, unknown> = {
        requires_connectors: requiresConnectors
          .split(",")
          .map(s => s.trim())
          .filter(Boolean),
        spawns_sub_agent: spawnsSubAgent,
      };
      const res = await apiFetch("/skills", {
        method: "POST",
        body: JSON.stringify({
          slug: slug.trim(),
          name: name.trim(),
          description: description.trim(),
          content,
          metadata,
        }),
      });
      const data: { slug: string } = await res.json();
      onSaved(data.slug);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save skill.");
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
      <input
        value={slug}
        onChange={e => setSlug(e.target.value)}
        placeholder="slug (e.g. sdr-outreach)"
        className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      />
      <input
        value={name}
        onChange={e => setName(e.target.value)}
        placeholder="Name"
        className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      />
      <input
        value={description}
        onChange={e => setDescription(e.target.value)}
        placeholder="Description"
        className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      />
      <textarea
        value={content}
        onChange={e => setContent(e.target.value)}
        placeholder="SKILL.md content…"
        rows={6}
        className="w-full px-2.5 py-1.5 rounded-lg border text-[12.5px] font-mono resize-none"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      />
      <input
        value={requiresConnectors}
        onChange={e => setRequiresConnectors(e.target.value)}
        placeholder="requires_connectors (comma separated)"
        className="w-full px-2.5 py-1.5 rounded-lg border text-[13px]"
        style={{ borderColor: "var(--border)", background: "var(--surface)" }}
      />
      <label className="flex items-center gap-2 cursor-pointer text-[12.5px]">
        <input type="checkbox" checked={spawnsSubAgent} onChange={e => setSpawnsSubAgent(e.target.checked)} />
        Spawns sub-agent
      </label>
      <div className="flex gap-2">
        <button
          onClick={submit}
          disabled={busy || !slug.trim() || !name.trim() || !content.trim()}
          className="btn btn-primary btn-sm flex-1"
        >
          {busy ? "Saving…" : "Save skill"}
        </button>
        <button onClick={onCancel} disabled={busy} className="btn btn-ghost btn-sm">
          Cancel
        </button>
      </div>
    </div>
  );
}

// ─── Detail pane ──────────────────────────────────────────────────────────────
function SkillDetailPane({ slug, onDeleted }: { slug: string; onDeleted: () => void }) {
  const [skill, setSkill] = useState<SkillDetail | null>(null);
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const res = await apiFetch(`/skills/${slug}`);
      const data: SkillDetail = await res.json();
      setSkill(data);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Failed to load skill.");
      return;
    }
    try {
      const res = await apiFetch(`/skills/${slug}/versions`);
      const data: SkillVersion[] = await res.json();
      setVersions(data);
    } catch {
      setVersions([]);
    }
  }, [slug]);

  useEffect(() => { void load(); }, [load]);

  async function remove() {
    setDeleting(true);
    setDeleteError(null);
    try {
      await apiFetch(`/skills/${slug}`, { method: "DELETE" });
      onDeleted();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : "Failed to delete skill.");
    } finally {
      setDeleting(false);
    }
  }

  if (loadError) {
    return <div className="p-6 text-[14px]" style={{ color: "var(--danger)" }}>{loadError}</div>;
  }
  if (!skill) {
    return <div className="p-6 text-[14px]" style={{ color: "var(--text-dim)" }}>Loading…</div>;
  }

  const isFilesystem = skill.source === "filesystem";

  return (
    <div className="flex flex-col h-full min-h-0 overflow-auto">
      <header className="px-5 py-3 border-b flex items-start gap-3" style={{ borderColor: "var(--border)" }}>
        <div className="flex-1 min-w-0">
          <div className="text-[15px] font-semibold">{skill.name}</div>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <SourceBadge source={skill.source} />
            <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>{skill.slug}</span>
            <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>v{skill.current_version}</span>
          </div>
        </div>
        {!isFilesystem && (
          <button
            onClick={remove}
            disabled={deleting}
            className="btn btn-ghost btn-sm"
            style={{ color: "var(--danger)" }}
          >
            {deleting ? "Deleting…" : "Delete"}
          </button>
        )}
      </header>

      <div className="flex-1 overflow-auto p-5 flex flex-col gap-5">
        {deleteError && (
          <div className="rounded-lg border px-4 py-3 text-[13px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>
            {deleteError}
          </div>
        )}

        {isFilesystem && (
          <div className="rounded-lg border px-4 py-2.5 text-[12.5px]" style={{ borderColor: "var(--border)", color: "var(--text-dim)" }}>
            This is a built-in filesystem skill and is read-only. Update it through the filesystem.
          </div>
        )}

        {skill.description && (
          <div>
            <div className="text-[13px] font-semibold mb-1">Description</div>
            <div className="text-[13px]" style={{ color: "var(--text)" }}>{skill.description}</div>
          </div>
        )}

        <div className="flex flex-col gap-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
          <div><span className="font-semibold">Requires connectors:</span> {skill.requires_connectors?.length ? skill.requires_connectors.join(", ") : "none"}</div>
          <div><span className="font-semibold">Spawns sub-agent:</span> {skill.spawns_sub_agent ? "yes" : "no"}</div>
        </div>

        {skill.content != null && (
          <div>
            <div className="text-[13px] font-semibold mb-2">SKILL.md</div>
            <pre
              className="text-[12.5px] font-mono overflow-auto p-4 rounded-lg whitespace-pre-wrap"
              style={{ background: "var(--surface)", color: "var(--text)", maxHeight: "400px" }}
            >
              {skill.content}
            </pre>
          </div>
        )}

        <div>
          <div className="text-[13px] font-semibold mb-2">Version history</div>
          {versions.length === 0 ? (
            <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>No versions recorded.</div>
          ) : (
            <div className="overflow-x-auto rounded-lg border" style={{ borderColor: "var(--border)" }}>
              <table className="w-full text-[12px]">
                <thead>
                  <tr style={{ background: "var(--surface-2, var(--surface))" }}>
                    {["Version", "Created", "By"].map(h => (
                      <th key={h} className="text-left px-3 py-2 font-semibold" style={{ color: "var(--text-dim)" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {versions.map(v => (
                    <tr key={v.version} className="border-t" style={{ borderColor: "var(--border)" }}>
                      <td className="px-3 py-2 font-mono">v{v.version}</td>
                      <td className="px-3 py-2">{v.created_at ? new Date(v.created_at).toLocaleString() : "—"}</td>
                      <td className="px-3 py-2" style={{ color: "var(--text-dim)" }}>{v.created_by ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
