"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";

type AgentTemplate = {
  id: string;
  name: string;
  role: string;
  category?: string;
  description: string;
  instructions?: string;
  tool_grants: string[];
  connector_grants: string[];
  memory_scopes: string[];
  approval_policy: Record<string, unknown>;
};

type AgentProfile = {
  id: string;
  profile_kind?: "assistant" | "agent";
  name: string;
  role: string;
  template_id?: string | null;
  instructions: string;
  model?: string | null;
  tool_grants: string[];
  connector_grants: string[];
  workflows?: string[];
  connected_accounts?: string[];
  project_ids: string[];
  memory_scopes: Array<{ scope: string; scope_id?: string }>;
  autonomy_level: string;
  approval_policy: Record<string, unknown>;
  schedule_permissions: Record<string, unknown>;
  status: string;
  created_at?: string;
};

const TARGETS = ["slack", "teams", "email", "web", "api"];

function timeLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function AgentsScreen() {
  const [templates, setTemplates] = useState<AgentTemplate[]>([]);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("All");
  const [runGoal, setRunGoal] = useState("");
  const [publishTarget, setPublishTarget] = useState("slack");
  const [externalChannel, setExternalChannel] = useState("");
  const [busy, setBusy] = useState(false);
  const [usingId, setUsingId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const selected = useMemo(
    () => agents.find(agent => agent.id === selectedId) ?? agents[0] ?? null,
    [agents, selectedId],
  );

  const categories = useMemo(() => {
    const set = new Set<string>();
    templates.forEach(t => set.add(t.category || "General"));
    return ["All", ...Array.from(set).sort()];
  }, [templates]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return templates.filter(t => {
      if (category !== "All" && (t.category || "General") !== category) return false;
      if (!q) return true;
      return (
        t.name.toLowerCase().includes(q) ||
        t.role.toLowerCase().includes(q) ||
        t.description.toLowerCase().includes(q)
      );
    });
  }, [templates, query, category]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [templateData, agentData] = await Promise.all([
        apiFetch("/agents/templates").then(res => res.json()) as Promise<AgentTemplate[]>,
        apiFetch("/agents").then(res => res.json()) as Promise<AgentProfile[]>,
      ]);
      setTemplates(templateData);
      setAgents(agentData);
      setSelectedId(current => current && agentData.some(agent => agent.id === current) ? current : agentData[0]?.id ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load agents");
      setAgents([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function useTemplate(template: AgentTemplate) {
    setUsingId(template.id);
    setError(null);
    setNotice(null);
    try {
      const created = await apiFetch("/agents", {
        method: "POST",
        body: JSON.stringify({
          name: template.name,
          profile_kind: "agent",
          role: template.role,
          template_id: template.id,
          instructions: template.instructions || template.description,
          tool_grants: template.tool_grants,
          connector_grants: template.connector_grants,
          workflows: [template.id],
          connected_accounts: template.connector_grants,
          memory_scopes: (template.memory_scopes || ["workspace"]).map(scope => ({ scope, scope_id: "workspace" })),
          autonomy_level: "supervised",
          approval_policy: template.approval_policy,
          schedule_permissions: { allowed: false, max_frequency: "disabled" },
        }),
      }).then(res => res.json()) as AgentProfile;
      await load();
      setSelectedId(created.id);
      setNotice(`${template.name} is ready to use.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to add agent");
    } finally {
      setUsingId(null);
    }
  }

  async function runAgent() {
    if (!selected || !runGoal.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const data = await apiFetch(`/agents/${selected.id}/run`, {
        method: "POST",
        body: JSON.stringify({ goal: runGoal.trim(), project_id: selected.project_ids[0] || undefined }),
      }).then(res => res.json()) as { task_id: string };
      setRunGoal("");
      setNotice(`Task queued: ${data.task_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to run agent");
    } finally {
      setBusy(false);
    }
  }

  async function removeAgent() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await apiFetch(`/agents/${selected.id}`, { method: "DELETE" });
      await load();
      setNotice("Agent removed.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to remove agent");
    } finally {
      setBusy(false);
    }
  }

  async function publishAgent() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const data = await apiFetch(`/agents/${selected.id}/publications`, {
        method: "POST",
        body: JSON.stringify({
          target: publishTarget,
          display_name: selected.name,
          external_channel_id: externalChannel.trim() || undefined,
          config: { reply_mode: "threaded" },
        }),
      }).then(res => res.json()) as { id: string; target: string; inbound_token?: string };
      setNotice(`${data.target} publication ready. Inbound token ${data.inbound_token ? "created" : "hidden"}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to publish agent");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 min-w-0 min-h-0 overflow-hidden flex flex-col" data-testid="agents-screen">
      <header className="px-10 pt-9 pb-5 flex items-start justify-between gap-6 flex-shrink-0">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Agents</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Browse ready-to-use agents and add them in one click. Need something custom? Just type <span className="font-mono">/agent</span> in chat and describe it.
          </p>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => void load()} disabled={busy}>Refresh</button>
      </header>

      {(error || notice) && (
        <div className="mx-10 mb-3 rounded-lg border px-3 py-2 text-[12.5px]" style={{
          borderColor: error ? "var(--danger)" : "var(--ok)",
          background: error ? "var(--danger-soft)" : "var(--ok-soft)",
          color: error ? "var(--danger)" : "var(--text)",
        }}>
          {error || notice}
        </div>
      )}

      <div className="flex-1 min-h-0 px-10 pb-10 grid gap-4" style={{ gridTemplateColumns: "280px minmax(0, 1fr) 320px" }}>
        <aside className="surface border border-soft rounded-lg min-h-0 overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b hairline flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Your agents</span>
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{agents.length}</span>
          </div>
          <div className="overflow-y-auto p-2 space-y-2">
            {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading...</div>}
            {!loading && agents.length === 0 && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No agents yet. Add one from the catalog.</div>}
            {agents.map(agent => (
              <button
                key={agent.id}
                data-testid="agent-profile-row"
                className="w-full rounded-md border border-soft p-3 text-left smooth"
                onClick={() => setSelectedId(agent.id)}
                style={{ background: selected?.id === agent.id ? "var(--surface-2)" : "transparent" }}
              >
                <div className="text-[13px] font-medium truncate">{agent.name}</div>
                <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>{agent.role}</div>
                <div className="mt-2 flex items-center gap-2 text-[11px]" style={{ color: "var(--text-faint)" }}>
                  <span>{agent.profile_kind ?? "agent"}</span>
                  <span>{agent.autonomy_level}</span>
                  <span>{timeLabel(agent.created_at)}</span>
                </div>
              </button>
            ))}
          </div>
        </aside>

        <main className="surface border border-soft rounded-lg min-h-0 overflow-y-auto">
          <div className="px-4 py-3 border-b hairline flex items-center gap-3 flex-wrap sticky top-0 z-10" style={{ background: "var(--surface)" }}>
            <div className="text-[14px] font-medium mr-auto">Agent catalog</div>
            <input
              className="input"
              style={{ maxWidth: 200 }}
              placeholder="Search agents"
              value={query}
              onChange={e => setQuery(e.target.value)}
            />
          </div>

          <div className="px-4 pt-3 flex items-center gap-1.5 flex-wrap">
            {categories.map(cat => (
              <button
                key={cat}
                className="rounded-full border border-soft px-3 py-1 text-[12px] smooth"
                onClick={() => setCategory(cat)}
                style={{
                  background: category === cat ? "var(--accent-soft)" : "transparent",
                  color: category === cat ? "var(--accent)" : "var(--text-dim)",
                }}
              >
                {cat}
              </button>
            ))}
          </div>

          <div className="p-4 grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
            {filtered.map(template => (
              <div key={template.id} data-testid="agent-template" className="rounded-lg border border-soft p-3.5 flex flex-col">
                <div className="text-[13.5px] font-medium">{template.name}</div>
                <div className="text-[11.5px] mt-0.5" style={{ color: "var(--text-faint)" }}>{template.category || "General"}</div>
                <p className="mt-1.5 text-[12px] leading-5 flex-1" style={{ color: "var(--text-dim)" }}>{template.description}</p>
                <button
                  className="btn btn-accent btn-sm mt-3 w-full justify-center"
                  onClick={() => void useTemplate(template)}
                  disabled={usingId !== null}
                >
                  {usingId === template.id ? "Adding…" : "Use agent"}
                </button>
              </div>
            ))}
            {!loading && filtered.length === 0 && (
              <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No agents match your search.</div>
            )}
          </div>
        </main>

        <aside className="surface border border-soft rounded-lg min-h-0 overflow-y-auto">
          <div className="px-4 py-3 border-b hairline">
            <div className="text-[14px] font-medium truncate">{selected?.name || "No agent selected"}</div>
            <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>{selected?.role || "Add an agent from the catalog to run and publish."}</div>
          </div>

          <div className="p-4 space-y-5">
            <section data-testid="agent-policy-summary">
              <div className="text-[12.5px] font-medium">Policy</div>
              <div className="mt-2 rounded-md border border-soft p-3 text-[12px] space-y-1" style={{ color: "var(--text-dim)" }}>
                <div>Autonomy: {selected?.autonomy_level || "—"}</div>
                <div>Tools: {(selected?.tool_grants || []).join(", ") || "—"}</div>
                <div>Memory: {(selected?.memory_scopes || []).map(scope => scope.scope).join(", ") || "—"}</div>
              </div>
            </section>

            <section>
              <div className="text-[12.5px] font-medium">Run</div>
              <textarea className="input mt-2 min-h-[88px]" value={runGoal} onChange={e => setRunGoal(e.target.value)} placeholder="Goal for this agent" />
              <button className="btn btn-accent btn-sm mt-2 w-full" onClick={runAgent} disabled={busy || !selected || !runGoal.trim()}>Queue task</button>
            </section>

            <section data-testid="agent-publishing-panel">
              <div className="text-[12.5px] font-medium">Publishing</div>
              <select className="input mt-2" value={publishTarget} onChange={e => setPublishTarget(e.target.value)}>
                {TARGETS.map(target => <option key={target} value={target}>{target}</option>)}
              </select>
              <input className="input mt-2" value={externalChannel} onChange={e => setExternalChannel(e.target.value)} placeholder="Channel, address, embed, or API key label" />
              <button className="btn btn-ghost btn-sm mt-2 w-full" onClick={publishAgent} disabled={busy || !selected}>Publish target</button>
            </section>

            {selected && (
              <button className="btn btn-ghost btn-sm w-full" style={{ color: "var(--danger)" }} onClick={removeAgent} disabled={busy}>Remove agent</button>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
