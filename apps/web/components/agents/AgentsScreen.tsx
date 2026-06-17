"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";

type AgentTemplate = {
  id: string;
  name: string;
  role: string;
  description: string;
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

type Project = { id: string; name: string };

const MODELS = ["gpt-5.4-mini", "gpt-5.4-nano", "deepseek-v4-pro", "deepseek-v4-flash"];
const TARGETS = ["slack", "teams", "email", "web", "api"];

function splitList(value: string) {
  return value.split(",").map(item => item.trim()).filter(Boolean);
}

function timeLabel(value?: string) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export default function AgentsScreen() {
  const [templates, setTemplates] = useState<AgentTemplate[]>([]);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState("research");
  const [name, setName] = useState("Research Agent");
  const [instructions, setInstructions] = useState("Prepare concise, cited answers and ask for approval before external replies.");
  const [model, setModel] = useState("gpt-5.4-mini");
  const [tools, setTools] = useState("web.search, research.run, artifact.write");
  const [connectors, setConnectors] = useState("slack, google_drive");
  const [projectId, setProjectId] = useState("");
  const [memoryScope, setMemoryScope] = useState("project");
  const [autonomy, setAutonomy] = useState("supervised");
  const [scheduleAllowed, setScheduleAllowed] = useState(false);
  const [runGoal, setRunGoal] = useState("");
  const [publishTarget, setPublishTarget] = useState("slack");
  const [externalChannel, setExternalChannel] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const selected = useMemo(
    () => agents.find(agent => agent.id === selectedId) ?? agents[0] ?? null,
    [agents, selectedId],
  );
  const selectedTemplate = useMemo(
    () => templates.find(template => template.id === selectedTemplateId) ?? templates[0] ?? null,
    [templates, selectedTemplateId],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [templateData, agentData, projectData] = await Promise.all([
        apiFetch("/agents/templates").then(res => res.json()) as Promise<AgentTemplate[]>,
        apiFetch("/agents").then(res => res.json()) as Promise<AgentProfile[]>,
        apiFetch("/projects/").then(res => res.json()).catch(() => []) as Promise<Project[]>,
      ]);
      setTemplates(templateData);
      setAgents(agentData);
      setProjects(projectData);
      setSelectedId(current => current && agentData.some(agent => agent.id === current) ? current : agentData[0]?.id ?? null);
      if (!projectId && projectData[0]?.id) setProjectId(projectData[0].id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load agents");
      setAgents([]);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!selectedTemplate) return;
    setName(selectedTemplate.name);
    setTools(selectedTemplate.tool_grants.join(", "));
    setConnectors(selectedTemplate.connector_grants.join(", "));
    setMemoryScope(selectedTemplate.memory_scopes[0] || "workspace");
  }, [selectedTemplate]);

  async function createAgent() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const projectIds = projectId ? [projectId] : [];
      const created = await apiFetch("/agents", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          profile_kind: "agent",
          role: selectedTemplate?.role || "workspace agent",
          template_id: selectedTemplateId,
          instructions: instructions.trim(),
          model,
          tool_grants: splitList(tools),
          connector_grants: splitList(connectors),
          workflows: [selectedTemplateId],
          connected_accounts: splitList(connectors),
          project_ids: projectIds,
          memory_scopes: [{ scope: memoryScope, scope_id: projectId || "workspace" }],
          autonomy_level: autonomy,
          approval_policy: { risky_writes: "require_approval", external_replies: "require_approval" },
          schedule_permissions: { allowed: scheduleAllowed, max_frequency: scheduleAllowed ? "daily" : "disabled" },
        }),
      }).then(res => res.json()) as AgentProfile;
      await load();
      setSelectedId(created.id);
      setNotice("Agent profile saved.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create agent");
    } finally {
      setBusy(false);
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
        body: JSON.stringify({ goal: runGoal.trim(), project_id: selected.project_ids[0] || projectId || undefined }),
      }).then(res => res.json()) as { task_id: string };
      setRunGoal("");
      setNotice(`Task queued: ${data.task_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to run agent");
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
          <h1 className="h-page tracking-tight">Agent menu</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Configure executable agents here. Use /agent in chat to create or edit agents and assistants conversationally.
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

      <div className="flex-1 min-h-0 px-10 pb-10 grid gap-4" style={{ gridTemplateColumns: "300px minmax(0, 1fr) 340px" }}>
        <aside className="surface border border-soft rounded-lg min-h-0 overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b hairline flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Profiles</span>
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{agents.length}</span>
          </div>
          <div className="overflow-y-auto p-2 space-y-2">
            {loading && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>Loading...</div>}
            {!loading && agents.length === 0 && <div className="text-[13px] p-3" style={{ color: "var(--text-dim)" }}>No agent profiles yet.</div>}
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
          <div className="px-4 py-3 border-b hairline flex items-center justify-between">
            <div>
              <div className="text-[14px] font-medium">Agent builder</div>
              <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>Templates are starting policy, not hidden defaults.</div>
            </div>
            <button className="btn btn-accent btn-sm" onClick={createAgent} disabled={busy || !name.trim() || !instructions.trim()}>Save profile</button>
          </div>

          <div className="p-4 grid gap-4">
            <section>
              <div className="text-[12.5px] font-medium mb-2">Templates</div>
              <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
                {templates.map(template => (
                  <button
                    key={template.id}
                    data-testid="agent-template"
                    className="rounded-md border border-soft p-3 text-left"
                    onClick={() => setSelectedTemplateId(template.id)}
                    style={{ background: selectedTemplateId === template.id ? "var(--accent-soft)" : "transparent" }}
                  >
                    <div className="text-[13px] font-medium">{template.name}</div>
                    <div className="mt-1 text-[11.5px] leading-5" style={{ color: "var(--text-dim)" }}>{template.description}</div>
                  </button>
                ))}
              </div>
            </section>

            <section className="grid gap-3" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
              <label className="grid gap-1 text-[12.5px] font-medium">Name
                <input className="input" value={name} onChange={e => setName(e.target.value)} />
              </label>
              <label className="grid gap-1 text-[12.5px] font-medium">Model
                <select className="input" value={model} onChange={e => setModel(e.target.value)}>
                  {MODELS.map(option => <option key={option} value={option}>{option}</option>)}
                </select>
              </label>
              <label className="grid gap-1 text-[12.5px] font-medium">Project
                <select className="input" value={projectId} onChange={e => setProjectId(e.target.value)}>
                  <option value="">Workspace only</option>
                  {projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
                </select>
              </label>
              <label className="grid gap-1 text-[12.5px] font-medium">Autonomy
                <select className="input" value={autonomy} onChange={e => setAutonomy(e.target.value)}>
                  <option value="manual">Manual</option>
                  <option value="supervised">Supervised</option>
                  <option value="approval_required">Approval required</option>
                  <option value="autonomous">Autonomous by policy</option>
                </select>
              </label>
              <label className="grid gap-1 text-[12.5px] font-medium">Tool grants
                <input className="input" data-testid="agent-tool-grants" value={tools} onChange={e => setTools(e.target.value)} />
              </label>
              <label className="grid gap-1 text-[12.5px] font-medium">Connector grants
                <input className="input" data-testid="agent-connector-grants" value={connectors} onChange={e => setConnectors(e.target.value)} />
              </label>
              <label className="grid gap-1 text-[12.5px] font-medium">Memory scope
                <select className="input" data-testid="agent-memory-scope" value={memoryScope} onChange={e => setMemoryScope(e.target.value)}>
                  <option value="personal">Personal</option>
                  <option value="project">Project</option>
                  <option value="workspace">Workspace</option>
                  <option value="org">Organization</option>
                  <option value="task">Task scratchpad</option>
                </select>
              </label>
              <label className="flex items-center gap-2 text-[12.5px] font-medium pt-6">
                <input type="checkbox" checked={scheduleAllowed} onChange={e => setScheduleAllowed(e.target.checked)} />
                Schedule permission
              </label>
            </section>

            <label className="grid gap-1 text-[12.5px] font-medium">Instructions
              <textarea className="input min-h-[120px]" value={instructions} onChange={e => setInstructions(e.target.value)} />
            </label>
          </div>
        </main>

        <aside className="surface border border-soft rounded-lg min-h-0 overflow-y-auto">
          <div className="px-4 py-3 border-b hairline">
            <div className="text-[14px] font-medium truncate">{selected?.name || "No agent selected"}</div>
            <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>{selected?.role || "Create a profile to run and publish."}</div>
          </div>

          <div className="p-4 space-y-5">
            <section data-testid="agent-policy-summary">
              <div className="text-[12.5px] font-medium">Policy</div>
              <div className="mt-2 rounded-md border border-soft p-3 text-[12px] space-y-1" style={{ color: "var(--text-dim)" }}>
                <div>Autonomy: {selected?.autonomy_level || autonomy}</div>
                <div>Approvals: risky writes and external replies</div>
                <div>Memory: {(selected?.memory_scopes || [{ scope: memoryScope }]).map(scope => scope.scope).join(", ")}</div>
                <div>Schedules: {String(selected?.schedule_permissions?.allowed ?? scheduleAllowed)}</div>
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
          </div>
        </aside>
      </div>
    </div>
  );
}
