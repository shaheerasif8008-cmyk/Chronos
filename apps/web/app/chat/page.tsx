"use client";

import { Component, ReactNode, useEffect, useRef, useState, useMemo, useCallback } from "react";
import { usePathname, useRouter } from "next/navigation";
import ArtifactsScreen from "../../components/artifacts/ArtifactsScreen";
import BrowserOperatorScreen from "../../components/browser/BrowserOperatorScreen";
import InChatArtifactPanel from "../../components/artifacts/InChatArtifactPanel";
import ResearchScreen from "../../components/research/ResearchScreen";

// ─── Config ──────────────────────────────────────────────────────────────────
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

function formatFileSize(bytes: number) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ─── Types ───────────────────────────────────────────────────────────────────
type Route = "chat" | "activity" | "approvals" | "memory" | "connectors" | "assistants" | "settings" | "projects" | "research" | "browser" | "tasks" | "artifacts" | "agents" | "workflows" | "audit";
type SettingsTab = "general" | "profile" | "organization" | "members" | "permissions" | "employees" | "runtime" | "memory-settings" | "tools-settings" | "approval-settings" | "notifications" | "security" | "billing" | "audit" | "developer" | "danger";
type Conversation = { id: string; title: string | null; updated_at?: string; created_at?: string };
type MessageRole = "user" | "assistant" | "system" | "tool";
type MessageStatus = "streaming" | "complete" | "paused" | "approval_pending" | "error";
type Message = {
  id?: string;
  role: MessageRole;
  content: string;
  status?: MessageStatus;
  created_at?: string;
  tool_traces?: ToolTrace[];
  reasoning_summaries?: ReasoningSummary[];
  artifacts?: ArtifactRef[];
  citations?: Array<{ url?: string; text?: string; [key: string]: unknown }>;
  model?: string;
  mode?: string;
  thinking?: boolean;
  pinned?: boolean;
  parent_message_id?: string | null;
  structured_response?: StructuredResponse | null;
};
type ToolTrace = { id: string; tool: string; summary: string; status: MessageStatus };
type ReasoningSummary = { id: string; iteration?: number; summary: string; status: MessageStatus };
type ArtifactRef = { id: string; title: string; kind: string; mime_type?: string; size_bytes?: number };
type PendingAttachment = {
  id: string;
  name: string;
  size: number;
  type: string;
  previewUrl?: string;
};
type ResponseActionRecord = { verb: string; description: string; target?: string | null };
type StructuredResponse = {
  response_type: string;
  status: string;
  summary: string;
  key_findings?: string[];
  assumptions?: string[];
  risks?: string[];
  next_action?: string | null;
  confidence?: string | null;
  artifacts?: ArtifactRef[];
  actions?: ResponseActionRecord[];
  approval_status?: string | null;
};
type ProjectSource = { id: string; title?: string | null; source_type?: string | null; parse_status?: string | null; index_status?: string | null; uri?: string | null; created_at?: string };
type SourceChunkPreview = { chunk_index: number; content: string; token_count?: number | null };
type SourceDetail = { id: string; title?: string | null; source_type?: string | null; uri?: string | null; artifact_id?: string | null; parse_status?: string | null; index_status?: string | null; warning?: string | null; chunk_count: number; chunks: SourceChunkPreview[] };
type MemoryEntry = { id: string; scope: string; scope_id: string; content: string; source: string; importance_score?: number; created_by?: string | null; created_at?: string; is_pinned?: boolean; is_archived?: boolean; is_sensitive?: boolean; superseded_by?: string | null };
type MemoryConflict = { stale_id: string; survivor_id: string; similarity: number; scope: string; stale_content: string; survivor_content: string };
type Connector = {
  id: string;
  name?: string;
  provider: string;
  description?: string;
  type?: string;
  category?: string;
  auth_type?: string;
  scopes?: string[];
  actions?: string[];
  account_handle?: string | null;
  status: string;
  connected_at?: string | null;
  last_used_at?: string | null;
};
type ConnectorAction = {
  name: string;
  description: string;
  parameters_schema: Record<string, unknown>;
  required_permissions: string[];
  risk_level: string;
  approval_required: boolean;
};
type ConnectorExecutionLog = {
  id: string;
  connector_id: string;
  action_name: string;
  arguments_redacted: Record<string, unknown>;
  result_status: string;
  error_message?: string | null;
  duration_ms: number;
  created_at?: string | null;
};
type ConnectorHealth = {
  connector_id: string;
  status: string;
  latency_ms?: number;
  failure_rate?: number;
  timeout_rate?: number;
  updated_at?: string | null;
};
type ConnectorTrace = {
  id: string;
  connector_id: string;
  action_name: string;
  status: string;
  duration_ms?: number;
  started_at?: string | null;
};
type ConnectorApproval = {
  id: string;
  connector_id: string;
  action_name: string;
  risk_level: string;
  status: string;
  approval_mode: string;
  justification?: string;
  created_at?: string | null;
};
type Task = { id: string; status: string; goal: string; current_step: number; plan?: TaskStep[] | { steps?: TaskStep[] }; agent_state?: Record<string, unknown>; result?: Record<string, unknown>; error?: string | null; created_at?: string; parent_task_id?: string | null; depth?: number; iteration_count?: number };
type TaskStep = { id: string; action: string; description: string; tool?: string | null };
type ChatModel = { id: string; label: string; model: string; description?: string; capabilities?: string[]; status?: string; tool_use?: boolean; fallback_for?: string[]; policy?: string };
type ChatMode = { id: string; label: string; description?: string; capabilities?: string[]; status?: string; creates_task?: boolean };
type TaskStreamEvent = {
  type: string;
  task_id?: string;
  ts?: string;
  created_at?: string | null;
  task?: Task;
  events?: TaskStreamEvent[];
  step?: TaskStep;
  step_index?: number;
  approval_ids?: string[];
  approval_id?: string | null;
  step_id?: string;
  error?: string;
  result?: unknown;
  attempt?: number;
  tool?: string | null;
  summary?: string | null;
  artifact_id?: string | null;
  artifact_title?: string | null;
  artifact_kind?: string | null;
  mime_type?: string;
  size_bytes?: number;
  payload?: Record<string, unknown>;
};
type Approval = { id: string; task_id: string; step_id: string; action_type: string; action_payload: Record<string, unknown>; requested_at?: string; status: string };
type ActivityAction = {
  id: string;
  type: string;
  status: string;
  summary: string;
  actor_id?: string | null;
  task_id?: string | null;
  task_goal?: string | null;
  task_status?: string | null;
  tool?: string | null;
  approval_id?: string | null;
  approval_status?: string | null;
  artifact_id?: string | null;
  artifact_title?: string | null;
  artifact_kind?: string | null;
  error?: string | null;
  created_at?: string | null;
  payload?: Record<string, unknown>;
};

const MODEL_STORAGE_KEY = "chronos.chat.selectedModel";
const DEFAULT_MODEL_ID = "gpt-5.4-mini";
const MODE_STORAGE_KEY = "chronos.chat.selectedMode";
const REASONING_STORAGE_KEY = "chronos.chat.reasoningEffort";
const REASONING_OPTIONS = [
  { id: "low", label: "Low", title: "Use lighter reasoning for faster replies." },
  { id: "medium", label: "Medium", title: "Use balanced reasoning." },
  { id: "high", label: "High", title: "Use deeper reasoning for harder requests." },
] as const;
type ReasoningEffort = typeof REASONING_OPTIONS[number]["id"];
const DEFAULT_REASONING_EFFORT: ReasoningEffort = "medium";

function modelOptionText(model: ChatModel) {
  if (model.id === "unavailable") return model.label;
  if (model.model === model.id || model.model === model.label) return model.model;
  const status = model.status && model.status !== "available" ? ` (${model.status})` : "";
  return `${model.label}: ${model.model}${status}`;
}

function modelOptionTitle(model: ChatModel) {
  const capabilities = model.capabilities?.length ? ` Capabilities: ${model.capabilities.join(", ")}.` : "";
  const policy = model.policy ? ` ${model.policy}` : "";
  return `${model.description || model.model}.${capabilities}${policy}`.trim();
}

function modeOptionTitle(mode: ChatMode) {
  const capabilities = mode.capabilities?.length ? ` Capabilities: ${mode.capabilities.join(", ")}.` : "";
  const task = mode.creates_task ? " Creates a durable task when selected." : " Runs in the chat turn unless routing escalates it.";
  return `${mode.description || mode.label}.${capabilities}${task}`.trim();
}

function taskSteps(task: Task | null | undefined): TaskStep[] {
  if (!task?.plan) return [];
  return Array.isArray(task.plan) ? task.plan : task.plan.steps ?? [];
}

function taskMessageStatus(task: Task): MessageStatus {
  if (task.status === "failed") return "error";
  if (task.status === "awaiting_approval" || task.status === "paused") return "approval_pending";
  if (task.status === "complete") return "complete";
  return "streaming";
}

function taskMessageContent(task: Task): string {
  if (task.status === "failed") return `The task stopped: ${task.error ?? "unknown error"}`;
  if (task.status === "awaiting_approval") return "Waiting for approval before Chronos continues.";
  if (task.status === "paused") return "Paused. Chronos will continue when the pause is cleared.";
  if (task.status === "complete") {
    const answer = task.result?.answer;
    return typeof answer === "string" && answer.trim() ? answer.trim() : "Task completed.";
  }
  if (task.status === "queued" || task.status === "pending" || task.status === "planning") return "Queued. Chronos is preparing the task.";
  return "";
}

function taskToolTraces(task: Task): ToolTrace[] {
  const steps = taskSteps(task);
  if (!steps.length) {
    return [{
      id: `${task.id}-task-state`,
      tool: "task",
      summary: task.status === "queued" || task.status === "pending" || task.status === "planning" ? "Preparing task plan..." : "Working...",
      status: taskMessageStatus(task),
    }];
  }
  return steps.map((step, index) => {
    let status: MessageStatus = "complete";
    if (task.status === "failed") status = "error";
    else if (task.status === "awaiting_approval" && step.action === "approval_gate" && index >= task.current_step) status = "approval_pending";
    else if (task.status !== "complete" && index >= task.current_step) status = index === task.current_step ? "streaming" : "paused";
    return {
      id: `${task.id}-${step.id}`,
      tool: step.tool || step.action || "task",
      summary: step.description,
      status,
    };
  });
}

function traceToolLabel(traceType: string, event: { tool?: string | null }) {
  if (event.tool) return event.tool;
  if (traceType === "route_decision") return "orchestrator.route";
  if (traceType === "model_step" || traceType === "thinking" || traceType === "model_result") return "agent.loop";
  if (traceType === "step_start" || traceType === "step_done") return "planner.step";
  if (traceType === "awaiting_approval") return "approval.gate";
  if (traceType === "sub_agent_spawned" || traceType === "sub_agent_complete") return "sub_agent";
  return traceType || "runtime";
}

function traceSummary(
  traceType: string,
  event: {
    tool?: string | null;
    summary?: string;
    error?: string;
    confidence?: number;
    iteration?: number;
    args_preview?: Record<string, unknown>;
    step?: { description?: string };
    approval_ids?: string[];
    goal?: string;
  },
) {
  if (traceType === "route_decision") {
    const confidence = typeof event.confidence === "number" && event.confidence > 0
      ? ` (${Math.round(event.confidence * 100)}%)`
      : "";
    return event.tool
      ? `Selected first tool: ${event.tool.replace(/[._]/g, " ")}${confidence}`
      : event.summary || "No first tool forced; using model-native loop";
  }
  if (traceType === "model_step" || traceType === "thinking") {
    const iter = typeof event.iteration === "number" ? ` ${event.iteration}` : "";
    return event.summary || `Reasoning iteration${iter}: context, memory, tools, and next action`;
  }
  if (traceType === "model_result") return event.summary || "Model returned a final answer";
  if (traceType === "tool_call") return `${traceToolLabel(traceType, event).replace(/[._]/g, " ")}...`;
  if (traceType === "tool_result") return event.summary ?? `${traceToolLabel(traceType, event)} done`;
  if (traceType === "tool_error") return event.error ?? `${traceToolLabel(traceType, event)} failed`;
  if (traceType === "step_start") return event.step?.description ?? "Planner step running";
  if (traceType === "step_done") return event.summary ?? "Planner step complete";
  if (traceType === "awaiting_approval") return `Waiting for approval on ${event.approval_ids?.length ?? 0} item(s)`;
  if (traceType === "sub_agent_spawned") return `Sub-agent: ${event.goal ?? "working"}`;
  if (traceType === "sub_agent_complete") return "Sub-agent finished";
  return event.summary || traceType.replace(/_/g, " ");
}

function taskLiveMessage(task: Task): Message | null {
  if (!["queued", "pending", "planning", "running", "awaiting_approval", "paused", "failed", "complete"].includes(task.status)) return null;
  return {
    id: `task-${task.id}`,
    role: "assistant",
    content: taskMessageContent(task),
    status: taskMessageStatus(task),
    created_at: task.created_at,
    tool_traces: taskToolTraces(task),
    thinking: ["queued", "pending", "planning", "running"].includes(task.status),
  };
}

function taskIsTerminal(task: Task | null | undefined): boolean {
  return task?.status === "complete" || task?.status === "failed" || task?.status === "cancelled";
}

function messageIdentity(message: Message): string | null {
  if (message.id) return message.id;
  return null;
}

function dedupeMessages(messages: Message[]): Message[] {
  const out: Message[] = [];
  const seen = new Set<string>();
  for (const message of messages) {
    const identity = messageIdentity(message);
    if (identity && seen.has(identity)) continue;
    if (identity) seen.add(identity);
    out.push(message);
  }
  return out;
}

function taskAnswer(task: Task | null, event?: TaskStreamEvent): string {
  const eventResult = event?.result;
  if (eventResult && typeof eventResult === "object" && "answer" in eventResult) {
    const answer = String((eventResult as Record<string, unknown>).answer ?? "").trim();
    if (answer) return answer;
  }
  const answer = task?.result?.answer;
  return typeof answer === "string" && answer.trim() ? answer.trim() : "";
}

function taskEventArtifact(event: TaskStreamEvent): ArtifactRef | null {
  const payload = event.payload ?? {};
  const artifactId = event.artifact_id ?? (typeof payload.artifact_id === "string" ? payload.artifact_id : undefined);
  if (!artifactId) return null;
  return {
    id: artifactId,
    title: event.artifact_title ?? (typeof payload.title === "string" ? payload.title : "artifact"),
    kind: event.artifact_kind ?? (typeof payload.kind === "string" ? payload.kind : "file"),
    mime_type: event.mime_type ?? (typeof payload.mime_type === "string" ? payload.mime_type : undefined),
    size_bytes: event.size_bytes ?? (typeof payload.size_bytes === "number" ? payload.size_bytes : undefined),
  };
}

function taskEventTrace(event: TaskStreamEvent): ToolTrace | null {
  if (event.type === "catch_up") return null;
  if (event.type === "artifact") {
    const artifact = taskEventArtifact(event);
    return {
      id: `artifact-${artifact?.id ?? event.created_at ?? event.ts ?? Date.now()}`,
      tool: "artifact",
      summary: artifact ? `Created artifact ${artifact.title}` : event.summary || "Created artifact",
      status: "complete",
    };
  }
  const traceEvent = {
    tool: event.tool,
    summary: event.summary ?? undefined,
    error: event.error,
    iteration: typeof event.payload?.iteration === "number" ? event.payload.iteration : undefined,
    args_preview: event.payload?.args_preview as Record<string, unknown> | undefined,
    step: event.step,
    approval_ids: event.approval_ids,
    goal: typeof event.payload?.goal === "string" ? event.payload.goal : undefined,
  };
  const tool = traceToolLabel(event.type, traceEvent);
  let status: MessageStatus = "complete";
  if (event.type === "tool_call" || event.type === "sub_agent_spawned" || event.type === "model_step" || event.type === "thinking") status = "streaming";
  if (event.type === "tool_error" || event.type === "task_failed") status = "error";
  if (event.type === "awaiting_approval") status = "approval_pending";
  return {
    id: `${event.type}-${tool}-${event.created_at ?? event.ts ?? Date.now()}`,
    tool,
    summary: event.summary || traceSummary(event.type, traceEvent),
    status,
  };
}

function applyTaskEventToMessage(message: Message, task: Task | null, event: TaskStreamEvent): Message {
  const artifact = taskEventArtifact(event);
  const nextArtifacts = artifact && !(message.artifacts ?? []).some(a => a.id === artifact.id)
    ? [...(message.artifacts ?? []), artifact]
    : message.artifacts;
  const trace = taskEventTrace(event);
  let traces = message.tool_traces ?? [];

  if (trace) {
    if (event.type === "tool_result" || event.type === "tool_error" || event.type === "sub_agent_complete" || event.type === "model_result") {
      const idx = [...traces].reverse().findIndex(t => t.tool === trace.tool && t.status === "streaming");
      if (idx >= 0) {
        const actualIdx = traces.length - 1 - idx;
        traces = [...traces];
        traces[actualIdx] = { ...traces[actualIdx], summary: trace.summary, status: trace.status };
      } else {
        traces = [...traces, trace];
      }
    } else if (!traces.some(t => t.id === trace.id)) {
      traces = [...traces, trace];
    }
  }

  if (event.type === "task_complete") {
    return {
      ...message,
      status: "complete",
      content: taskAnswer(task, event) || message.content || "Task completed.",
      thinking: false,
      artifacts: nextArtifacts,
      tool_traces: traces.map(t => t.status === "streaming" ? { ...t, status: "complete" as MessageStatus } : t),
    };
  }
  if (event.type === "task_failed") {
    return {
      ...message,
      status: "error",
      content: message.content || `The task stopped: ${formatTaskError(event.error)}`,
      thinking: false,
      artifacts: nextArtifacts,
      tool_traces: traces,
    };
  }
  return {
    ...message,
    status: message.status === "complete" ? "streaming" : message.status,
    thinking: event.type === "model_step" || event.type === "thinking" || event.type === "tool_call" || event.type === "sub_agent_spawned",
    artifacts: nextArtifacts,
    tool_traces: traces,
  };
}

function taskMessageFromStream(task: Task, events: TaskStreamEvent[]): Message {
  const base = taskLiveMessage(task) ?? {
    id: `task-${task.id}`,
    role: "assistant" as MessageRole,
    content: "",
    status: "streaming" as MessageStatus,
    tool_traces: [],
    thinking: true,
  };
  return events.reduce((message, event) => applyTaskEventToMessage(message, task, event), base);
}
type SettingsOverview = {
  member: { id: string; email: string; name?: string | null; role: string; can_admin: boolean };
  organization: Record<string, unknown>;
  sections: Record<string, Record<string, unknown>>;
  members: Array<{ id: string; name: string; email: string; role: string; status: string; is_self: boolean }>;
  connectors: Array<Connector & { scopes?: string[]; policy?: Record<string, unknown> }>;
  memory_stats: { active: number; deleted: number };
  runtime_health: Record<string, unknown> & {
    connectors?: Record<string, { status?: string; tier?: string; reason?: string; setup?: string | null }>;
  };
  capabilities: Record<string, { supported: boolean; reason: string }>;
};

function routeFromPath(pathname: string | null): Route {
  if (pathname === "/activity") return "activity";
  if (pathname === "/approvals") return "approvals";
  if (pathname === "/memory") return "memory";
  if (pathname === "/connectors") return "connectors";
  if (pathname === "/assistants") return "assistants";
  if (pathname === "/artifacts") return "artifacts";
  if (pathname === "/settings") return "settings";
  if (pathname === "/projects") return "projects";
  if (pathname === "/research") return "research";
  if (pathname === "/browser") return "browser";
  if (pathname === "/tasks") return "tasks";
  if (pathname === "/agents") return "agents";
  if (pathname === "/workflows") return "workflows";
  if (pathname === "/audit") return "audit";
  return "chat";
}

function pathForRoute(route: Route) {
  return route === "chat" ? "/chat" : `/${route}`;
}

const ACCENT_PALETTES: Record<string, { accent: string; hover: string; soft: string; text: string }> = {
  coral:  { accent: "oklch(0.66 0.135 45)",  hover: "oklch(0.60 0.145 45)",  soft: "oklch(0.94 0.04 50)",  text: "oklch(0.40 0.13 45)" },
  forest: { accent: "oklch(0.55 0.13 155)",  hover: "oklch(0.50 0.14 155)",  soft: "oklch(0.94 0.04 155)", text: "oklch(0.32 0.12 155)" },
  indigo: { accent: "oklch(0.55 0.16 270)",  hover: "oklch(0.50 0.17 270)",  soft: "oklch(0.94 0.04 270)", text: "oklch(0.35 0.15 270)" },
  slate:  { accent: "oklch(0.42 0.025 240)", hover: "oklch(0.36 0.03 240)",  soft: "oklch(0.94 0.01 240)", text: "oklch(0.30 0.025 240)" },
};

// ─── Search palette types (module scope) ─────────────────────────────────────
type SearchResult = { type: string; id: string; title: string; snippet: string; url: string };

const PALETTE_TYPE_LABELS: Record<string, string> = {
  conversations: "Conversations",
  messages: "Messages",
  tasks: "Tasks",
  artifacts: "Artifacts",
  memory: "Memory",
  sources: "Sources",
};

const DEFAULT_CHAT_MODES: ChatMode[] = [
  { id: "default",  label: "Default" },
  { id: "research", label: "Research" },
  { id: "agent",    label: "Agent" },
  { id: "browser",  label: "Browser" },
  { id: "computer", label: "Computer" },
  { id: "data",     label: "Data" },
  { id: "image",    label: "Image" },
  { id: "voice",    label: "Voice" },
  { id: "coding",   label: "Coding" },
];

// ─── Auth helpers ─────────────────────────────────────────────────────────────
function getToken() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("chronos_token") ?? "";
}

async function apiFetch(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(`${apiBase()}${path}`, { ...init, headers });
  if (res.status === 401) {
    localStorage.removeItem("chronos_token");
    window.location.href = "/login";
  }
  if (!res.ok) throw new Error(await res.text());
  return res;
}

// ─── Icons ───────────────────────────────────────────────────────────────────
type IcProps = { size?: number; stroke?: number; style?: React.CSSProperties };
function Ic({ size = 18, stroke = 1.6, style = {}, children }: IcProps & { children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor"
         strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" style={style} aria-hidden>
      {children}
    </svg>
  );
}

const IC = {
  Logo:       (p: IcProps) => <Ic {...p}><circle cx="10" cy="10" r="7"/><path d="M10 5v5l3.2 2"/></Ic>,
  Chat:       (p: IcProps) => <Ic {...p}><path d="M3.5 6c0-1.4 1.1-2.5 2.5-2.5h8c1.4 0 2.5 1.1 2.5 2.5v5c0 1.4-1.1 2.5-2.5 2.5H9l-3.5 3v-3c-1.1 0-2-.9-2-2z"/></Ic>,
  Activity:   (p: IcProps) => <Ic {...p}><path d="M3 10h2.5l2-5 4 10 2-5H17"/></Ic>,
  Approvals:  (p: IcProps) => <Ic {...p}><rect x="3" y="4" width="14" height="12" rx="2"/><path d="M3 8h14M6 5.5h.01M8 5.5h.01"/></Ic>,
  Memory:     (p: IcProps) => <Ic {...p}><path d="M4 8c0-3 2.5-5 5.5-5S15 5 15 8v6c0 1-1 2-2 2H7c-1 0-2-1-2-2"/><path d="M4 8c-1 .5-1 2 0 2.5"/><path d="M9 7h3M9 9.5h3"/></Ic>,
  Connectors: (p: IcProps) => <Ic {...p}><path d="M7 3v3M13 3v3M5 6h10v3a5 5 0 01-10 0zM10 14v3"/></Ic>,
  Personas:   (p: IcProps) => <Ic {...p}><circle cx="10" cy="7" r="3"/><path d="M4 17c0-3 2.5-5 6-5s6 2 6 5"/></Ic>,
  Settings:   (p: IcProps) => <Ic {...p}><circle cx="10" cy="10" r="2.4"/><path d="M10 2v2M10 16v2M18 10h-2M4 10H2M15.7 4.3l-1.4 1.4M5.7 14.3l-1.4 1.4M15.7 15.7l-1.4-1.4M5.7 5.7l-1.4-1.4"/></Ic>,
  Plus:       (p: IcProps) => <Ic {...p}><path d="M10 4v12M4 10h12"/></Ic>,
  X:          (p: IcProps) => <Ic {...p}><path d="M5 5l10 10M15 5L5 15"/></Ic>,
  Check:      (p: IcProps) => <Ic {...p}><path d="M4 10.5l3.5 3.5L16 5.5"/></Ic>,
  ChevronDown:(p: IcProps) => <Ic {...p}><path d="M5 8l5 5 5-5"/></Ic>,
  Chevron:    (p: IcProps) => <Ic {...p}><path d="M8 5l5 5-5 5"/></Ic>,
  More:       (p: IcProps) => <Ic {...p}><circle cx="5" cy="10" r="1.2" fill="currentColor"/><circle cx="10" cy="10" r="1.2" fill="currentColor"/><circle cx="15" cy="10" r="1.2" fill="currentColor"/></Ic>,
  Sparkles:   (p: IcProps) => <Ic {...p}><path d="M10 3l1.2 3 3 1.2-3 1.2L10 11.5 8.8 8.4l-3-1.2 3-1.2zM15 13l.6 1.5 1.5.6-1.5.6-.6 1.5-.6-1.5-1.5-.6 1.5-.6z"/></Ic>,
  ArrowUp:    (p: IcProps) => <Ic {...p}><path d="M10 16V4M5 9l5-5 5 5"/></Ic>,
  Pause:      (p: IcProps) => <Ic {...p}><rect x="6" y="4.5" width="2.5" height="11"/><rect x="11.5" y="4.5" width="2.5" height="11"/></Ic>,
  Attach:     (p: IcProps) => <Ic {...p}><path d="M14 7l-6 6c-1.4 1.4-3.6 1.4-5 0s-1.4-3.6 0-5l7-7c.9-.9 2.6-.9 3.5 0s.9 2.6 0 3.5l-6.5 6.5c-.5.5-1.3.5-1.8 0s-.5-1.3 0-1.8L11 4"/></Ic>,
  Search:     (p: IcProps) => <Ic {...p}><circle cx="9" cy="9" r="5"/><path d="M13 13l3 3"/></Ic>,
  Globe:      (p: IcProps) => <Ic {...p}><circle cx="10" cy="10" r="7"/><path d="M3 10h14M10 3c2.5 2.5 2.5 11.5 0 14M10 3c-2.5 2.5-2.5 11.5 0 14"/></Ic>,
  Mail:       (p: IcProps) => <Ic {...p}><rect x="3" y="4.5" width="14" height="11" rx="1.5"/><path d="M3.5 5.5L10 11l6.5-5.5"/></Ic>,
  Pencil:     (p: IcProps) => <Ic {...p}><path d="M3 17l1-3 9-9 3 3-9 9-3 1zM11 7l3 3"/></Ic>,
  Trash:      (p: IcProps) => <Ic {...p}><path d="M4 6h12M7 6V4.5c0-.7.6-1.5 1.5-1.5h3c.9 0 1.5.8 1.5 1.5V6M5.5 6l.7 10.5c0 .7.6 1.5 1.5 1.5h4.5c.9 0 1.5-.8 1.5-1.5L14.5 6"/></Ic>,
  Lock:       (p: IcProps) => <Ic {...p}><rect x="4.5" y="9" width="11" height="8" rx="1.5"/><path d="M7 9V6.5a3 3 0 016 0V9"/></Ic>,
  Bell:       (p: IcProps) => <Ic {...p}><path d="M5 13h10l-1.2-2V8c0-2.1-1.7-3.8-3.8-3.8S6.2 5.9 6.2 8v3zM8 13a2 2 0 004 0"/></Ic>,
  Briefcase:  (p: IcProps) => <Ic {...p}><rect x="3" y="6.5" width="14" height="10" rx="1.5"/><path d="M7.5 6.5V5c0-.6.4-1 1-1h3c.6 0 1 .4 1 1v1.5M3 11.5h14"/></Ic>,
  Filter:     (p: IcProps) => <Ic {...p}><path d="M3 5h14l-5 7v5l-4-2v-3z"/></Ic>,
  Info:       (p: IcProps) => <Ic {...p}><circle cx="10" cy="10" r="7"/><path d="M10 9v5M10 6.5h.01"/></Ic>,
  External:   (p: IcProps) => <Ic {...p}><path d="M9 4H4v12h12v-5M11 4h5v5M16 4l-7 7"/></Ic>,
  ArrowRight: (p: IcProps) => <Ic {...p}><path d="M4 10h12M11 5l5 5-5 5"/></Ic>,
  Audit:      (p: IcProps) => <Ic {...p}><path d="M4 3h8l3 3v11H4z"/><path d="M12 3v3h3"/><path d="M6.5 9.5h6M6.5 12h6M6.5 14.5h3"/></Ic>,
  Help:       (p: IcProps) => <Ic {...p}><circle cx="10" cy="10" r="7"/><path d="M8 8c0-1 1-2 2-2s2 1 2 2-2 1-2 2.5M10 14h.01"/></Ic>,
  Sun:        (p: IcProps) => <Ic {...p}><circle cx="10" cy="10" r="3.5"/><path d="M10 2v2M10 16v2M18 10h-2M4 10H2M15.7 4.3l-1.4 1.4M5.7 14.3l-1.4 1.4M15.7 15.7l-1.4-1.4M5.7 5.7l-1.4-1.4"/></Ic>,
  Moon:       (p: IcProps) => <Ic {...p}><path d="M17 12.5A7 7 0 017.5 3a7 7 0 109.5 9.5z"/></Ic>,
  PanelClose: (p: IcProps) => <Ic {...p}><rect x="3" y="4" width="14" height="12" rx="1.5"/><path d="M12 4v12M15.5 8L14 10l1.5 2"/></Ic>,
  PanelOpen:  (p: IcProps) => <Ic {...p}><rect x="3" y="4" width="14" height="12" rx="1.5"/><path d="M12 4v12M14 8l1.5 2-1.5 2"/></Ic>,
  Clock:      (p: IcProps) => <Ic {...p}><circle cx="10" cy="10" r="7"/><path d="M10 6v4l2.5 1.5"/></Ic>,
  Refresh:    (p: IcProps) => <Ic {...p}><path d="M3.5 9a6.5 6.5 0 0111-3.5L17 8M16.5 11a6.5 6.5 0 01-11 3.5L3 12M14 4v4h-4M6 16v-4h4"/></Ic>,
  Mic:        (p: IcProps) => <Ic {...p}><rect x="8" y="3" width="4" height="9" rx="2"/><path d="M5 10c0 2.8 2.2 5 5 5s5-2.2 5-5M10 15v2"/></Ic>,
  Folder:     (p: IcProps) => <Ic {...p}><path d="M3 6c0-.6.4-1 1-1h3.5l1.5 1.5H16c.6 0 1 .4 1 1v8c0 .6-.4 1-1 1H4c-.6 0-1-.4-1-1z"/></Ic>,
  Lightbulb:  (p: IcProps) => <Ic {...p}><path d="M7 13c-.7-1-1.5-2-1.5-4 0-2.5 2-4.5 4.5-4.5s4.5 2 4.5 4.5c0 2-.8 3-1.5 4M8 13h4M8.5 16h3"/></Ic>,
  Eye:        (p: IcProps) => <Ic {...p}><path d="M2 10s2.8-5 8-5 8 5 8 5-2.8 5-8 5-8-5-8-5z"/><circle cx="10" cy="10" r="2.2"/></Ic>,
  Stop:       (p: IcProps) => <Ic {...p}><rect x="5" y="5" width="10" height="10" rx="1"/></Ic>,
  Volume:     (p: IcProps) => <Ic {...p}><path d="M6 8H4c-.6 0-1 .4-1 1v2c0 .6.4 1 1 1h2l4 3V5L6 8z"/><path d="M13.5 6.5a5 5 0 010 7M16 4a8.5 8.5 0 010 12"/></Ic>,
};

// ─── Primitives ───────────────────────────────────────────────────────────────
function Dot({ color = "var(--accent)", size = 8, pulse = false, ring = false }: { color?: string; size?: number; pulse?: boolean; ring?: boolean }) {
  return (
    <span
      className={`inline-block flex-shrink-0 ${pulse ? "pulse-dot" : ""} ${ring ? "pulse-ring" : ""}`}
      style={{ width: size, height: size, borderRadius: 999, background: color }}
    />
  );
}

function StatusDot({ status }: { status: string }) {
  const map: Record<string, { c: string; pulse?: boolean; ring?: boolean }> = {
    working:   { c: "var(--accent)", pulse: true, ring: true },
    awaiting:  { c: "var(--warn)" },
    done:      { c: "var(--ok)" },
    failed:    { c: "var(--danger)" },
    queued:    { c: "var(--text-faint)" },
    connected: { c: "var(--ok)" },
    available: { c: "var(--text-faint)" },
    error:     { c: "var(--danger)" },
  };
  const m = map[status] || { c: "var(--text-faint)" };
  return <Dot color={m.c} pulse={!!m.pulse} ring={!!m.ring} />;
}

function Tag({ children, variant = "default" }: { children: ReactNode; variant?: "default" | "accent" | "ok" | "warn" | "danger" | "info" }) {
  const cls = { default: "", accent: "tag-accent", ok: "tag-ok", warn: "tag-warn", danger: "tag-danger", info: "tag-info" }[variant];
  return <span className={`tag ${cls}`}>{children}</span>;
}

function PersonaAvatar({ name, color = "var(--accent)", size = 28 }: { name?: string; color?: string; size?: number }) {
  return (
    <div className="rounded-full flex items-center justify-center font-semibold flex-shrink-0"
         style={{ width: size, height: size, background: color, color: "white", fontSize: size * 0.42, letterSpacing: "-0.02em" }}>
      {name?.[0] ?? "C"}
    </div>
  );
}

function PageHeader({ title, subtitle, right }: { title: string; subtitle?: string; right?: ReactNode }) {
  return (
    <header className="px-10 pt-9 pb-6 flex items-start justify-between gap-6 flex-shrink-0">
      <div className="min-w-0">
        <h1 className="h-page tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>{subtitle}</p>}
      </div>
      {right && <div className="flex items-center gap-2 flex-shrink-0">{right}</div>}
    </header>
  );
}

function EmptyState({ icon, title, sub }: { icon?: ReactNode; title: string; sub?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-20 px-6 text-center">
      {icon && <div className="w-12 h-12 rounded-full flex items-center justify-center mb-4"
                    style={{ background: "var(--surface-2)", color: "var(--text-dim)" }}>{icon}</div>}
      <div className="text-[16px] font-medium mb-1.5">{title}</div>
      {sub && <div className="text-[13.5px] max-w-[400px]" style={{ color: "var(--text-dim)" }}>{sub}</div>}
    </div>
  );
}

// ─── Mock personas ─────────────────────────────────────────────────────────────
const PERSONAS = [
  { id: "p_chronos", name: "Chronos", role: "General assistant", color: "var(--accent)",
    prompt: "An all-purpose operations assistant. Acts confidently, escalates when uncertain, always asks before sending anything outside the organization.",
    skills: ["general"], connectors: ["gmail", "browser"] },
  { id: "p_jordan",  name: "Jordan",  role: "Sales outreach",   color: "var(--info)",
    prompt: "Researches leads against your ICP, writes personalized cold email. Never sends anything without your approval.",
    skills: ["general", "sdr-outreach"], connectors: ["gmail", "browser"] },
  { id: "p_morgan",  name: "Morgan",  role: "Research analyst", color: "var(--ok)",
    prompt: "Synthesizes markets, competitors, and customer segments. Always cites sources. Writes briefs, not opinions.",
    skills: ["general"], connectors: ["browser"] },
];

const SKILLS = [
  { id: "general",      name: "General",       description: "Default reasoning, writing, and research." },
  { id: "sdr-outreach", name: "Sales outreach", description: "Lead research, ICP qualification, personalized cold email drafting." },
];

function initialPersonaId() {
  if (typeof window === "undefined") return PERSONAS[0].id;
  const personaId = new URLSearchParams(window.location.search).get("persona");
  return PERSONAS.some(persona => persona.id === personaId) ? personaId! : PERSONAS[0].id;
}

function initialNewConversationOpen() {
  if (typeof window === "undefined") return false;
  const params = new URLSearchParams(window.location.search);
  return window.location.pathname === "/chat" && params.get("new") === "1";
}

// ─── Error Boundary ────────────────────────────────────────────────────────────
type ErrorBoundaryProps = { children: ReactNode };
type ErrorBoundaryState = { hasError: boolean; error: Error | null };
class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="flex items-center justify-center" style={{ height: "100vh", background: "var(--bg)", color: "var(--text)" }}>
          <div className="text-center max-w-md px-6">
            <div className="w-12 h-12 rounded-full flex items-center justify-center mx-auto mb-4"
                 style={{ background: "var(--danger-soft)", color: "var(--danger)" }}>
              <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round"><circle cx="10" cy="10" r="7"/><path d="M10 7v4M10 13.5h.01"/></svg>
            </div>
            <h1 className="h-page mb-2">Something went wrong</h1>
            <p className="text-[14px] mb-6" style={{ color: "var(--text-dim)" }}>{this.state.error?.message ?? "An unexpected error occurred."}</p>
            <button onClick={() => { this.setState({ hasError: false, error: null }); window.location.href = "/chat"; }}
                    className="btn btn-accent">Reload app</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

// ─── Root App ─────────────────────────────────────────────────────────────────
function ChronosAppInner() {
  const router = useRouter();
  const pathname = usePathname();
  const [route, setRoute] = useState<Route>(() => routeFromPath(pathname));
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const [activeConvoId, setActiveConvoId] = useState<string | null>(null);
  const [activePersonaId, setActivePersonaId] = useState(() => initialPersonaId());
  const [newConversationOpen, setNewConversationOpen] = useState(() => initialNewConversationOpen());
  const newConversationOpenRef = useRef(newConversationOpen);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationsLoading, setConversationsLoading] = useState(false);
  const [pendingApprovals, setPendingApprovals] = useState(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">("dark");
  const [accent, setAccent] = useState("coral");
  const [searchPaletteOpen, setSearchPaletteOpen] = useState(false);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  useEffect(() => {
    const p = ACCENT_PALETTES[accent] ?? ACCENT_PALETTES.coral;
    document.documentElement.style.setProperty("--accent", p.accent);
    document.documentElement.style.setProperty("--accent-hover", p.hover);
    document.documentElement.style.setProperty("--accent-soft", p.soft);
    document.documentElement.style.setProperty("--accent-text", p.text);
  }, [accent]);

  useEffect(() => {
    if (!getToken()) { router.replace("/login"); return; }
    void loadPendingApprovals();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setRoute(routeFromPath(pathname));
  }, [pathname]);

  useEffect(() => {
    void loadConversations();
  }, [route]);

  useEffect(() => {
    newConversationOpenRef.current = newConversationOpen;
  }, [newConversationOpen]);

  function navigateRoute(next: Route) {
    setRoute(next);
    router.push(pathForRoute(next));
  }

  async function loadConversations(selectId?: string) {
    setConversationsLoading(true);
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const data = (await (await apiFetch("/chat/conversations")).json()) as Conversation[];
        setConversations(data);
        if (selectId) {
          setActiveConvoId(selectId);
          setNewConversationOpen(false);
          newConversationOpenRef.current = false;
        } else if (!activeConvoId && !newConversationOpenRef.current && data[0]) {
          setActiveConvoId(data[0].id);
        }
        setConversationsLoading(false);
        return;
      } catch {
        if (attempt < 2) await new Promise(r => setTimeout(r, 200 * (attempt + 1)));
      }
    }
    setConversationsLoading(false);
  }

  async function loadPendingApprovals() {
    try {
      const data = (await (await apiFetch("/approvals/?status=pending")).json()) as Approval[];
      setPendingApprovals(data.length);
    } catch { setPendingApprovals(0); }
  }

  function openSettings(tab: SettingsTab) {
    setSettingsTab(tab);
    navigateRoute("settings");
  }

  function signOut() {
    localStorage.removeItem("chronos_token");
    router.replace("/login");
  }

  async function deleteConversation(id: string) {
    try {
      await apiFetch(`/chat/conversations/${id}`, { method: "DELETE" });
      setConversations(prev => prev.filter(c => c.id !== id));
      if (activeConvoId === id) setActiveConvoId(null);
    } catch { /* silently */ }
  }

  return (
    <div className="flex" style={{ height: "100vh", background: "var(--bg)", color: "var(--text)" }}>
      <Sidebar
        collapsed={sidebarCollapsed}
        onCollapse={() => setSidebarCollapsed(true)}
        onExpand={() => setSidebarCollapsed(false)}
        route={route}
        onNavigate={navigateRoute}
        conversations={conversations}
        conversationsLoading={conversationsLoading}
        activeConvoId={activeConvoId}
        onSelectConvo={(id) => { setActiveConvoId(id); setNewConversationOpen(false); newConversationOpenRef.current = false; navigateRoute("chat"); }}
        onNewConvo={() => { setActiveConvoId(null); setNewConversationOpen(true); newConversationOpenRef.current = true; navigateRoute("chat"); }}
        onDeleteConvo={deleteConversation}
        pendingApprovals={pendingApprovals}
        onOpenSettings={openSettings}
        onSignOut={signOut}
        onOpenSearch={() => setSearchPaletteOpen(true)}
        onRefreshConversations={() => void loadConversations()}
      />

      <main className="flex-1 min-w-0 flex flex-col" style={{ background: "var(--bg)" }}>
        {route === "chat"       && <ChatScreen activeConvoId={activeConvoId} activePersonaId={activePersonaId} onPersonaChange={setActivePersonaId} onConvoCreated={(id) => loadConversations(id)} onApprovalsChanged={loadPendingApprovals} paletteOpen={searchPaletteOpen} onPaletteOpenChange={setSearchPaletteOpen} />}
        {route === "activity"   && <ActivityScreen />}
        {route === "approvals"  && <ApprovalsScreen onDecision={loadPendingApprovals} />}
        {route === "memory"     && <MemoryScreen />}
        {route === "artifacts"  && <ArtifactsScreen />}
        {route === "connectors" && <ConnectorsScreen />}
        {route === "assistants" && <AssistantsScreen onStartConversation={(personaId) => {
          setActivePersonaId(personaId);
          setActiveConvoId(null);
          setNewConversationOpen(true);
          newConversationOpenRef.current = true;
          setRoute("chat");
          router.replace("/chat");
        }} />}
        {route === "settings"   && <SettingsScreen tab={settingsTab} setTab={setSettingsTab} theme={theme} setTheme={setTheme} accent={accent} setAccent={setAccent} signOut={signOut} />}
        {route === "projects"   && <ProjectsScreen />}
        {route === "research"   && <ResearchScreen />}
        {route === "browser"    && <BrowserOperatorScreen />}
        {route === "tasks"      && <EmptyPanel label="Tasks" />}
        {route === "agents"     && <EmptyPanel label="Agents" />}
        {route === "workflows"  && <EmptyPanel label="Workflows" />}
        {route === "audit"      && <EmptyPanel label="Audit" />}
      </main>
      <InChatArtifactPanel />
    </div>
  );
}

// ─── Sidebar ──────────────────────────────────────────────────────────────────
function Sidebar({
  collapsed, onCollapse, onExpand, route, onNavigate, conversations, conversationsLoading, activeConvoId,
  onSelectConvo, onNewConvo, onDeleteConvo, pendingApprovals, onOpenSettings, onSignOut, onRefreshConversations,
  onOpenSearch,
}: {
  collapsed: boolean; onCollapse: () => void; onExpand: () => void;
  route: Route; onNavigate: (r: Route) => void;
  conversations: Conversation[]; conversationsLoading: boolean; activeConvoId: string | null;
  onSelectConvo: (id: string) => void; onNewConvo: () => void;
  onDeleteConvo: (id: string) => void; pendingApprovals: number;
  onOpenSettings: (tab: SettingsTab) => void; onSignOut: () => void;
  onOpenSearch?: () => void;
  onRefreshConversations: () => void;
}) {
  const [accountOpen, setAccountOpen] = useState(false);
  const [convoMenu, setConvoMenu] = useState<string | null>(null);

  const nav = [
    { id: "activity"   as Route, icon: <IC.Activity size={15}/>,   label: "Activity" },
    { id: "approvals"  as Route, icon: <IC.Approvals size={15}/>,  label: "Approvals",  badge: pendingApprovals || null, badgeKind: "warn" },
    { id: "artifacts"  as Route, icon: <IC.Folder size={15}/>,     label: "Artifacts" },
    { id: "connectors" as Route, icon: <IC.Connectors size={15}/>, label: "Connectors" },
    { id: "assistants" as Route, icon: <IC.Personas size={15}/>,   label: "Assistants" },
    { id: "projects"   as Route, icon: <IC.Folder size={15}/>,     label: "Projects" },
    { id: "research"   as Route, icon: <IC.Search size={15}/>,     label: "Research" },
    { id: "browser"    as Route, icon: <IC.Globe size={15}/>,      label: "Browser" },
    { id: "tasks"      as Route, icon: <IC.Check size={15}/>,      label: "Tasks" },
    { id: "agents"     as Route, icon: <IC.Sparkles size={15}/>,   label: "Agents" },
    { id: "workflows"  as Route, icon: <IC.Refresh size={15}/>,    label: "Workflows" },
  ];

  // Group conversations by recency
  const groups = useMemo(() => {
    const today: Conversation[] = [];
    const earlier: Conversation[] = [];
    conversations.forEach(c => {
      const ts = c.updated_at ?? c.created_at ?? "";
      const d = new Date(ts);
      const now = new Date();
      const isToday = d.toDateString() === now.toDateString();
      (isToday ? today : earlier).push(c);
    });
    return [
      ...(today.length ? [{ label: "Today", items: today }] : []),
      ...(earlier.length ? [{ label: "Earlier", items: earlier }] : []),
    ];
  }, [conversations]);

  if (collapsed) {
    return (
      <aside className="flex-shrink-0 flex flex-col items-center py-3 gap-2 border-r hairline"
             style={{ width: 56, background: "var(--bg-deep)" }}>
        <button onClick={onExpand} className="btn btn-ghost btn-icon" title="Expand sidebar">
          <IC.PanelOpen size={16}/>
        </button>
        <button onClick={onNewConvo} className="btn btn-secondary btn-icon" title="New conversation">
          <IC.Plus size={16}/>
        </button>
        <div className="w-8 h-px" style={{ background: "var(--border-soft)" }}/>
        <div className="flex flex-col items-center gap-2 overflow-y-auto no-scrollbar w-full px-2">
          {nav.map(it => (
            <button key={it.id}
                    onClick={() => onNavigate(it.id)}
                    title={it.label}
                    className="btn btn-ghost btn-icon relative flex-shrink-0"
                    style={{ background: route === it.id ? "var(--surface-2)" : "transparent",
                             color: route === it.id ? "var(--text)" : "var(--text-muted)" }}>
              {it.icon}
              {it.badge ? (
                <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-1 rounded-full text-[9px] font-semibold flex items-center justify-center"
                      style={{ background: "var(--warn)", color: "white" }}>{it.badge}</span>
              ) : null}
            </button>
          ))}
        </div>
      </aside>
    );
  }

  return (
    <aside className="flex-shrink-0 flex flex-col border-r hairline relative"
           style={{ width: 256, background: "var(--bg-deep)" }}>
      {/* Brand + collapse */}
      <div className="px-3 pt-3 pb-2 flex items-center justify-between">
        <div className="flex items-center gap-2 pl-1">
          <div className="w-6 h-6 rounded-md flex items-center justify-center"
               style={{ background: "var(--text)", color: "var(--bg)" }}>
            <IC.Logo size={13} stroke={2.2}/>
          </div>
          <span className="text-[14.5px] font-semibold tracking-tight">Chronos</span>
        </div>
        <button onClick={onCollapse} className="btn btn-ghost btn-icon" title="Collapse sidebar">
          <IC.PanelClose size={16}/>
        </button>
      </div>

      {/* New conversation */}
      <div className="px-3 pt-1 pb-2 flex gap-1.5">
        <button onClick={onNewConvo}
                className="flex-1 flex items-center gap-2.5 px-3 py-2 rounded-lg smooth surface border border-soft hover:border-[var(--border)] whitespace-nowrap">
          <IC.Plus size={15} stroke={2}/>
          <span className="text-[13.5px] font-medium">New conversation</span>
        </button>
        <button
          onClick={() => onOpenSearch?.()}
          title="Search (⌘K)"
          className="btn btn-ghost btn-icon flex-shrink-0 surface border border-soft hover:border-[var(--border)]"
        >
          <IC.Search size={14}/>
        </button>
      </div>

      {/* Top nav */}
      <div className="px-3 pt-2 pb-1 space-y-0.5 overflow-y-auto no-scrollbar">
        {nav.map(it => (
          <button key={it.id}
                  onClick={() => onNavigate(it.id)}
                  className={`nav-item w-full ${route === it.id ? "active" : ""}`}>
            <span className="nav-icon flex-shrink-0">{it.icon}</span>
            <span className="flex-1 text-left">{it.label}</span>
            {it.badge ? (
              <span className="min-w-[18px] h-[18px] px-1.5 rounded-full text-[10.5px] font-semibold tabular flex items-center justify-center"
                    style={{ background: it.badgeKind === "warn" ? "var(--warn)" : "var(--accent)", color: "white" }}>
                {it.badge}
              </span>
            ) : null}
          </button>
        ))}
      </div>

      <div className="mt-3 mb-1 mx-3 border-t hairline"/>

      <div className="px-3 flex items-center justify-between">
        <span className="text-[11.5px] font-medium uppercase tracking-wider" style={{ color: "var(--text-dim)" }}>Conversations</span>
        <button onClick={onRefreshConversations} className="btn btn-ghost btn-icon" title="Refresh conversations" style={{ width: 22, height: 22 }}>
          <IC.Refresh size={12}/>
        </button>
      </div>

      {/* Conversation list */}
      <div className="flex-1 overflow-y-auto px-2 pt-1 no-scrollbar">
        {groups.length === 0 && conversationsLoading && (
          <p className="px-2.5 py-2 text-[13px] flex items-center gap-2" style={{ color: "var(--text-dim)" }}>
            <span className="typing-wave inline-flex" style={{ height: 12 }}><span/><span/><span/></span>
            Loading...
          </p>
        )}
        {groups.length === 0 && !conversationsLoading && (
          <p className="px-2.5 py-2 text-[13px]" style={{ color: "var(--text-dim)" }}>No conversations yet.</p>
        )}
        {groups.map(g => (
          <div key={g.label} className="mb-3">
            <div className="px-2.5 py-1 text-[11.5px] font-medium uppercase tracking-wider" style={{ color: "var(--text-dim)" }}>
              {g.label}
            </div>
            <div className="space-y-0.5">
              {g.items.map(c => {
                const isActive = c.id === activeConvoId && route === "chat";
                return (
                  <div key={c.id} className="relative group">
                    <button onClick={() => onSelectConvo(c.id)}
                            className={`convo-row w-full pr-8 ${isActive ? "active" : ""}`}>
                      <span className="flex-1 truncate text-left">{c.title ?? "Untitled"}</span>
                    </button>
                    <button
                      onClick={e => { e.stopPropagation(); setConvoMenu(convoMenu === c.id ? null : c.id); }}
                      className="absolute right-1 top-1/2 -translate-y-1/2 p-1.5 rounded-md opacity-0 group-hover:opacity-100 smooth hover:bg-[var(--surface-2)]"
                      style={{ color: "var(--text-dim)" }}>
                      <IC.More size={13}/>
                    </button>
                    {convoMenu === c.id && (
                      <div className="surface absolute right-1 top-8 z-30 w-36 overflow-hidden rounded-lg border shadow-lg"
                           style={{ borderColor: "var(--border)" }}>
                        <button onClick={() => { onDeleteConvo(c.id); setConvoMenu(null); }}
                                className="flex w-full items-center gap-2 px-3 py-2 text-[13px] text-left hover:bg-[var(--danger-soft)]"
                                style={{ color: "var(--danger)" }}>
                          <IC.Trash size={13}/> Delete
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {/* Account footer */}
      <div className="px-2 pb-2 pt-1 border-t hairline relative">
        <button onClick={() => setAccountOpen(v => !v)}
                className="w-full flex items-center gap-2.5 px-2 py-2 rounded-lg smooth hover:bg-[var(--surface-2)]"
                style={{ background: accountOpen ? "var(--surface-2)" : "transparent" }}>
          <div className="avatar-u" style={{ width: 28, height: 28 }}>A</div>
          <div className="flex-1 min-w-0 text-left">
            <div className="text-[13px] font-medium truncate">My Workspace</div>
            <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>Owner</div>
          </div>
          <IC.ChevronDown size={14} style={{ color: "var(--text-dim)", transform: accountOpen ? "rotate(180deg)" : undefined, transition: "transform .15s" }}/>
        </button>

        {accountOpen && (
          <AccountMenu
            onClose={() => setAccountOpen(false)}
            onSettings={(tab) => { setAccountOpen(false); onOpenSettings(tab); }}
            onSignOut={onSignOut}
          />
        )}
      </div>
    </aside>
  );
}

function AccountMenu({ onClose, onSettings, onSignOut }: { onClose: () => void; onSettings: (tab: SettingsTab) => void; onSignOut: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onClick = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) onClose(); };
    setTimeout(() => document.addEventListener("click", onClick), 0);
    return () => document.removeEventListener("click", onClick);
  }, [onClose]);

  return (
    <div ref={ref} className="absolute bottom-[64px] left-2 right-2 surface border rounded-xl fadeup overflow-hidden z-50"
         style={{ borderColor: "var(--border)", boxShadow: "0 1px 2px color-mix(in oklch, var(--text) 8%, transparent), 0 12px 32px color-mix(in oklch, var(--text) 12%, transparent)" }}>
      <div className="px-3 py-3 border-b hairline">
        <div className="text-[13.5px] font-semibold">My Workspace</div>
        <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>owner · Chronos workspace</div>
      </div>

      {[
        { label: "Profile",            icon: <IC.Personas size={14}/>, tab: "profile" as SettingsTab },
        { label: "General settings",   icon: <IC.Settings size={14}/>, tab: "general" as SettingsTab },
        { label: "Organization",       icon: <IC.Briefcase size={14}/>, tab: "organization" as SettingsTab },
        { label: "Memory",             icon: <IC.Memory size={14}/>, tab: "memory-settings" as SettingsTab },
        { label: "Notifications",      icon: <IC.Bell size={14}/>, tab: "notifications" as SettingsTab },
        { label: "Audit log",          icon: <IC.Audit size={14}/>, tab: "audit" as SettingsTab },
      ].map(it => (
        <button key={it.tab} onClick={() => onSettings(it.tab)}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-[13px] smooth hover:bg-[var(--surface-2)]"
                style={{ color: "var(--text)" }}>
          <span style={{ color: "var(--text-dim)" }}>{it.icon}</span>
          <span className="flex-1 text-left font-medium">{it.label}</span>
        </button>
      ))}

      <div className="border-t hairline"/>
      <button onClick={() => {}} className="w-full flex items-center gap-2.5 px-3 py-2 text-[13px] smooth hover:bg-[var(--surface-2)]" style={{ color: "var(--text)" }}>
        <span style={{ color: "var(--text-dim)" }}><IC.Help size={14}/></span>
        <span className="flex-1 text-left font-medium">Help & feedback</span>
      </button>

      <div className="border-t hairline"/>
      <button onClick={onSignOut} className="w-full flex items-center gap-2.5 px-3 py-2 text-[13px] smooth hover:bg-[var(--danger-soft)]" style={{ color: "var(--danger)" }}>
        <IC.ArrowRight size={14}/> Sign out
      </button>

      <div className="px-3 py-2 border-t hairline">
        <div className="flex items-center justify-between text-[11px]" style={{ color: "var(--text-dim)" }}>
        <span>Chronos · Sprint 4</span>
          <span className="flex items-center gap-1"><Dot color="var(--ok)" size={5}/> Healthy</span>
        </div>
      </div>
    </div>
  );
}

// ─── Chat Screen ──────────────────────────────────────────────────────────────
function ChatScreen({
  activeConvoId,
  activePersonaId,
  onPersonaChange,
  onConvoCreated,
  onApprovalsChanged,
  paletteOpen,
  onPaletteOpenChange,
}: {
  activeConvoId: string | null;
  activePersonaId: string;
  onPersonaChange: (personaId: string) => void;
  onConvoCreated: (id: string) => void;
  onApprovalsChanged: () => void;
  paletteOpen: boolean;
  onPaletteOpenChange: (open: boolean) => void;
}) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatModels, setChatModels] = useState<ChatModel[]>([]);
  const [chatModes, setChatModes] = useState<ChatMode[]>(DEFAULT_CHAT_MODES);
  const [selectedModel, setSelectedModel] = useState(DEFAULT_MODEL_ID);
  const [modelsLoadError, setModelsLoadError] = useState("");
  const [selectedMode, setSelectedMode] = useState<string>("default");
  const [modesLoadError, setModesLoadError] = useState("");
  const [selectedReasoningEffort, setSelectedReasoningEffort] = useState<ReasoningEffort>(DEFAULT_REASONING_EFFORT);
  const [streaming, setStreaming] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [activeTaskEvents, setActiveTaskEvents] = useState<TaskStreamEvent[]>([]);
  const [activeTaskStreamError, setActiveTaskStreamError] = useState("");
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [personaMenuOpen, setPersonaMenuOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const streamingRef = useRef(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const voiceInputRef = useRef<HTMLInputElement>(null);
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [voiceBusy, setVoiceBusy] = useState(false);
  const attachmentPreviewUrlsRef = useRef<Set<string>>(new Set());
  const isEmpty = !activeConvoId && messages.length === 0;
  const inlineApprovalIds = useMemo(() => {
    const ids = new Set<string>();
    const decided = new Set<string>();
    for (const event of activeTaskEvents) {
      if (event.type === "awaiting_approval") {
        for (const id of event.approval_ids ?? []) ids.add(id);
      }
      if ((event.type === "approval_decided" || event.type === "approval_rejected") && event.approval_id) {
        decided.add(event.approval_id);
      }
    }
    for (const id of decided) ids.delete(id);
    return [...ids];
  }, [activeTaskEvents]);

  useEffect(() => {
    return () => {
      attachmentPreviewUrlsRef.current.forEach(url => URL.revokeObjectURL(url));
      attachmentPreviewUrlsRef.current.clear();
    };
  }, []);

  // ── Command palette (⌘K) ──────────────────────────────────────────────────
  const router = useRouter();
  const [paletteQuery, setPaletteQuery] = useState("");
  const [paletteResults, setPaletteResults] = useState<SearchResult[]>([]);
  const [paletteSelectedIdx, setPaletteSelectedIdx] = useState(-1);
  const paletteInputRef = useRef<HTMLInputElement>(null);
  const paletteDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const paletteAbortRef = useRef<AbortController | null>(null);
  const palettePrevFocusRef = useRef<Element | null>(null);

  // Cmd/Ctrl+K global listener — toggles palette
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        onPaletteOpenChange(!paletteOpen);
      }
      if (e.key === "Escape" && paletteOpen) {
        onPaletteOpenChange(false);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [paletteOpen, onPaletteOpenChange]);

  useEffect(() => {
    if (paletteOpen) {
      // Capture previously focused element so we can restore it on close
      palettePrevFocusRef.current = document.activeElement;
      setPaletteQuery("");
      setPaletteResults([]);
      setPaletteSelectedIdx(-1);
      setTimeout(() => paletteInputRef.current?.focus(), 0);
    } else {
      // Abort any in-flight request
      paletteAbortRef.current?.abort();
      // Restore focus to previously focused element (best-effort)
      if (palettePrevFocusRef.current && palettePrevFocusRef.current instanceof HTMLElement) {
        palettePrevFocusRef.current.focus();
      }
      palettePrevFocusRef.current = null;
    }
  }, [paletteOpen]);

  useEffect(() => {
    if (paletteDebounceRef.current) clearTimeout(paletteDebounceRef.current);
    if (!paletteQuery.trim()) { setPaletteResults([]); setPaletteSelectedIdx(-1); return; }

    paletteDebounceRef.current = setTimeout(async () => {
      paletteAbortRef.current?.abort();
      const ctrl = new AbortController();
      paletteAbortRef.current = ctrl;
      try {
        const res = await apiFetch(`/search?q=${encodeURIComponent(paletteQuery.trim())}`, { signal: ctrl.signal });
        if (!res.ok) return;
        const data = (await res.json()) as SearchResult[];
        if (!ctrl.signal.aborted) { setPaletteResults(data); setPaletteSelectedIdx(-1); }
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        console.error("[palette] search error:", err);
        setPaletteResults([]);
      }
    }, 200);

    return () => {
      if (paletteDebounceRef.current) clearTimeout(paletteDebounceRef.current);
    };
  }, [paletteQuery]);

  // Flat list of all results for keyboard index math
  const paletteFlatResults = paletteResults;

  function handlePaletteSelect(result: SearchResult) {
    onPaletteOpenChange(false);
    setPaletteQuery("");
    router.push(result.url);
  }

  function handlePaletteKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!paletteFlatResults.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setPaletteSelectedIdx(i => Math.min(i + 1, paletteFlatResults.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setPaletteSelectedIdx(i => Math.max(i - 1, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (paletteSelectedIdx >= 0 && paletteSelectedIdx < paletteFlatResults.length) {
        handlePaletteSelect(paletteFlatResults[paletteSelectedIdx]);
      }
    }
  }

  // Group results by type in a stable order (uses module-scope PALETTE_TYPE_LABELS)
  const paletteGrouped = paletteResults.reduce<Record<string, SearchResult[]>>((acc, r) => {
    (acc[r.type] ??= []).push(r);
    return acc;
  }, {});
  const paletteGroups = Object.keys(PALETTE_TYPE_LABELS)
    .filter(k => paletteGrouped[k]?.length)
    .map(k => ({ type: k, label: PALETTE_TYPE_LABELS[k], items: paletteGrouped[k] }));

  // Build a flat index map so we can highlight the right item in grouped view
  let _paletteFlatIdx = 0;
  const paletteGroupsWithIdx = paletteGroups.map(g => ({
    ...g,
    items: g.items.map(item => ({ item, flatIdx: _paletteFlatIdx++ })),
  }));
  // ── End command palette ───────────────────────────────────────────────────

  const activePersona = PERSONAS.find(p => p.id === activePersonaId) ?? PERSONAS[0];

  const loadMessagesFromServer = useCallback(async (
    conversationId: string,
    opts?: { streamedContent?: string; preserveLocal?: boolean },
  ) => {
    const [rawMsgs, arts, latestTask] = await Promise.all([
      apiFetch(`/chat/conversations/${conversationId}/messages`).then(r => r.json()),
      apiFetch(`/artifacts?conversation_id=${conversationId}`).then(r => r.json()).catch(() => []),
      apiFetch(`/chat/conversations/${conversationId}/latest-task`).then(r => r.json()).catch(() => null),
    ]) as [
      Array<Record<string, unknown>>,
      Array<{ id: string; message_id?: string | null; title?: string; kind: string; mime_type?: string; size_bytes?: number }>,
      (Task & { id?: string }) | null,
    ];
    const byMessage = new Map<string, ArtifactRef[]>();
    for (const a of arts || []) {
      if (!a.message_id) continue;
      const ref: ArtifactRef = { id: a.id, title: a.title ?? "artifact", kind: a.kind, mime_type: a.mime_type, size_bytes: a.size_bytes };
      byMessage.set(a.message_id, [...(byMessage.get(a.message_id) ?? []), ref]);
    }
    const normalized: Message[] = rawMsgs.map(m => {
      const id = m.id != null ? String(m.id) : undefined;
      const role = String(m.role ?? "assistant") as MessageRole;
      const content = String(m.content ?? "");
      // runtime_status overrides "complete" when present (e.g. "error", "paused").
      // Validate against the known union to guard against stale or unknown values.
      const KNOWN_STATUSES: ReadonlySet<string> = new Set<MessageStatus>(["streaming", "complete", "paused", "approval_pending", "error"]);
      const rawStatus = m.runtime_status != null ? String(m.runtime_status) : "complete";
      const status: MessageStatus = KNOWN_STATUSES.has(rawStatus) ? (rawStatus as MessageStatus) : "complete";
      const base: Message = {
        id,
        role,
        content,
        status,
        created_at: m.created_at != null ? String(m.created_at) : undefined,
        model: m.model != null ? String(m.model) : undefined,
        mode: m.mode != null ? String(m.mode) : undefined,
        citations: Array.isArray(m.citations) ? (m.citations as Array<{ url?: string; text?: string }>) : undefined,
        // Persisted tool_traces from DB (overridden by live SSE during streaming)
        tool_traces: Array.isArray(m.tool_traces)
          ? (m.tool_traces as ToolTrace[])
          : undefined,
        structured_response: (m.structured_response as StructuredResponse | undefined) ?? null,
        pinned: m.pinned === true,
        parent_message_id: m.parent_message_id != null ? String(m.parent_message_id) : null,
      };
      // artifact_refs from the message row are the authoritative refresh-time source.
      // Merge with any per-message artifacts from the /artifacts endpoint (live-SSE path
      // may have set message_id on artifact rows; artifact_refs is the loop-persisted copy).
      const persistedRefs: ArtifactRef[] = Array.isArray(m.artifact_refs)
        ? (m.artifact_refs as ArtifactRef[])
        : [];
      const byMessageRefs: ArtifactRef[] = (id && byMessage.has(id)) ? (byMessage.get(id) ?? []) : [];
      // Deduplicate by id; prefer byMessage (has full metadata from artifacts table)
      const seenIds = new Set(byMessageRefs.map(a => a.id));
      const merged = [
        ...byMessageRefs,
        ...persistedRefs.filter(a => !seenIds.has(a.id)),
      ];
      if (merged.length > 0) return { ...base, artifacts: merged };
      return base;
    });

    const hasAssistant = normalized.some(m => m.role === "assistant" && m.content.trim());
    const shouldShowTaskFallback = !!latestTask && (
      !taskIsTerminal(latestTask) || !hasAssistant
    );
    if (!hasAssistant && opts?.streamedContent?.trim()) {
      normalized.push({ role: "assistant", content: opts.streamedContent.trim(), status: "complete" });
    }
    if (shouldShowTaskFallback && !hasAssistant && !opts?.streamedContent?.trim() && latestTask) {
      const liveMessage = taskLiveMessage(latestTask);
      if (liveMessage) normalized.push(liveMessage);
    }

    setMessages(prev => {
      if (opts?.preserveLocal && normalized.length < prev.length) return prev;
      // Preserve a just-streamed structured_response if the refreshed DB row
      // doesn't have one yet (avoids cards vanishing on a resync that wins a race).
      const prevById = new Map(prev.filter(m => m.id).map(m => [m.id!, m]));
      const merged = normalized.map(message => {
        if (!message.id) return message;
        const existing = prevById.get(message.id);
        return !message.structured_response && existing?.structured_response
          ? { ...message, structured_response: existing.structured_response }
          : message;
      });
      return dedupeMessages(merged);
    });
    if (latestTask?.id && shouldShowTaskFallback && !taskIsTerminal(latestTask)) {
      setActiveTaskId(latestTask.id);
      setActiveTask(latestTask);
    } else if (latestTask?.id && taskIsTerminal(latestTask)) {
      setActiveTaskId(null);
      setActiveTask(null);
    }
  }, []);

  useEffect(() => {
    apiFetch("/chat/models")
      .then(r => r.json())
      .then((data: ChatModel[]) => {
        setModelsLoadError("");
        setChatModels(data);
        const preferred = data.find(model => model.id === DEFAULT_MODEL_ID) ?? data[0];
        const stored = window.localStorage.getItem(MODEL_STORAGE_KEY) || selectedModel;
        const nextModel = data.some(model => model.id === stored) ? stored : preferred?.id;
        if (nextModel && nextModel !== selectedModel) {
          setSelectedModel(nextModel);
        }
        if (nextModel) {
          window.localStorage.setItem(MODEL_STORAGE_KEY, nextModel);
        }
      })
      .catch((err) => {
        setModelsLoadError(err instanceof Error ? err.message : "Unable to load models");
        setChatModels([]);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    apiFetch("/chat/modes")
      .then(r => r.json())
      .then((data: ChatMode[]) => {
        const modes = Array.isArray(data) && data.length ? data : DEFAULT_CHAT_MODES;
        setModesLoadError("");
        setChatModes(modes);
        const stored = window.localStorage.getItem(MODE_STORAGE_KEY) || selectedMode;
        const nextMode = modes.some(mode => mode.id === stored) ? stored : "default";
        if (nextMode !== selectedMode) {
          setSelectedMode(nextMode);
        }
        window.localStorage.setItem(MODE_STORAGE_KEY, nextMode);
      })
      .catch((err) => {
        setModesLoadError(err instanceof Error ? err.message : "Unable to load modes");
        setChatModes(DEFAULT_CHAT_MODES);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const stored = window.localStorage.getItem(REASONING_STORAGE_KEY);
    if (REASONING_OPTIONS.some(option => option.id === stored)) {
      setSelectedReasoningEffort(stored as ReasoningEffort);
    } else {
      window.localStorage.setItem(REASONING_STORAGE_KEY, DEFAULT_REASONING_EFFORT);
    }
  }, []);

  useEffect(() => {
    if (!activeConvoId) {
      if (!streamingRef.current) setMessages([]);
      setActiveTaskId(null);
      setActiveTask(null);
      setActiveTaskEvents([]);
      setActiveTaskStreamError("");
      return;
    }
    // Don't clobber in-flight SSE updates when a new conversation id arrives mid-stream.
    if (streamingRef.current) return;
    setActivityOpen(false);
    setActiveTaskId(null);
    setActiveTask(null);
    setActiveTaskEvents([]);
    setActiveTaskStreamError("");
    void loadMessagesFromServer(activeConvoId).catch(() => {});
  }, [activeConvoId, loadMessagesFromServer]);

  useEffect(() => {
    if (!activeTaskId) {
      setActiveTask(null);
      setActiveTaskEvents([]);
      setActiveTaskStreamError("");
      return;
    }
    const controller = new AbortController();

    async function connectTaskStream() {
      setActiveTaskEvents([]);
      setActiveTaskStreamError("");
      try {
        const res = await apiFetch(`/tasks/${activeTaskId}/stream`, { signal: controller.signal });
        const reader = res.body?.getReader();
        if (!reader) throw new Error("Task stream did not return a readable body");
        const decoder = new TextDecoder();
        let buffer = "";

        function consumeFrame(frame: string) {
          const line = frame.split("\n").find(part => part.startsWith("data: "));
          if (!line) return;
          const event = JSON.parse(line.slice(6)) as TaskStreamEvent;
          if (event.type === "catch_up") {
            if (event.task) {
              const replay = event.events ?? [];
              setActiveTask(replay.reduce<Task | null>((task, item) => mergeTaskEvent(task, item), event.task));
            }
            setActiveTaskEvents(event.events ?? []);
            return;
          }
          setActiveTaskEvents(prev => [...prev, event]);
          setActiveTask(prev => mergeTaskEvent(prev, event));
        }

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) consumeFrame(frame);
        }
        if (buffer.trim()) consumeFrame(buffer.trim());
      } catch (exc) {
        if ((exc as Error).name !== "AbortError") {
          setActiveTaskStreamError(exc instanceof Error ? exc.message : "Task stream disconnected");
        }
      }
    }

    void connectTaskStream();
    return () => controller.abort();
  }, [activeTaskId]);

  useEffect(() => {
    if (!activeTaskId || !activeTask) return;
    const liveMessage = taskMessageFromStream(activeTask, activeTaskEvents);
    setMessages(prev => {
      if (prev.length === 0) return [liveMessage];
      const idx = prev.findIndex(message =>
        message.id === `task-${activeTaskId}` ||
        (message.role === "assistant" && (message.tool_traces ?? []).some(trace => trace.id === `task-created-${activeTaskId}` || trace.summary.includes(activeTaskId.slice(0, 8))))
      );
      if (idx >= 0) {
        const updated = [...prev];
        const current = updated[idx];
        updated[idx] = {
          ...current,
          ...liveMessage,
          id: current.id ?? liveMessage.id,
          content: liveMessage.content || current.content,
          tool_traces: liveMessage.tool_traces?.length ? liveMessage.tool_traces : current.tool_traces,
          artifacts: liveMessage.artifacts?.length ? liveMessage.artifacts : current.artifacts,
        };
        return updated;
      }
      const last = prev[prev.length - 1];
      if (last?.role === "assistant" && last.status === "streaming" && !last.content.trim()) {
        return [...prev.slice(0, -1), { ...last, ...liveMessage, id: last.id }];
      }
      return dedupeMessages([...prev, liveMessage]);
    });
  }, [activeTaskId, activeTask, activeTaskEvents]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function uploadFiles(files: FileList) {
    // Must use bare fetch, NOT apiFetch: apiFetch forces Content-Type: application/json
    // on any request with a body (see apiFetch line ~178), which breaks multipart
    // uploads — the browser needs to set the multipart boundary itself.
    const token = getToken();
    const base = apiBase();
    setUploadError(null);
    for (const file of Array.from(files)) {
      const previewUrl = file.type.startsWith("image/") ? URL.createObjectURL(file) : undefined;
      if (previewUrl) attachmentPreviewUrlsRef.current.add(previewUrl);
      const form = new FormData();
      form.append("file", file);
      if (activeConvoId) form.append("conversation_id", activeConvoId);
      try {
        const res = await fetch(`${base}/attachments`, {
          method: "POST",
          body: form,
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (res.ok) {
          const data = await res.json() as { attachment_id: string; filename: string; size_bytes: number };
          setAttachments(prev => [...prev, { id: data.attachment_id, name: data.filename, size: data.size_bytes, type: file.type, previewUrl }]);
        } else {
          if (previewUrl) {
            URL.revokeObjectURL(previewUrl);
            attachmentPreviewUrlsRef.current.delete(previewUrl);
          }
          let detail = `Upload failed (${res.status})`;
          try { const body = await res.json() as { detail?: string }; if (body.detail) detail = body.detail; } catch { /* ignore */ }
          setUploadError(detail);
        }
      } catch (err) {
        if (previewUrl) {
          URL.revokeObjectURL(previewUrl);
          attachmentPreviewUrlsRef.current.delete(previewUrl);
        }
        setUploadError(err instanceof Error ? err.message : "Upload failed — please try again");
      }
    }
  }

  async function uploadVoice(files: FileList) {
    // Upload audio file via /attachments then call /chat/voice/transcribe.
    // On success, populate the composer draft with the transcript text.
    // On unavailable (no STT provider), show an honest degraded error message.
    const token = getToken();
    const base = apiBase();
    setVoiceError(null);
    const file = files[0];
    if (!file) return;
    setVoiceBusy(true);
    try {
      // Step 1: Upload audio as attachment
      const form = new FormData();
      form.append("file", file);
      if (activeConvoId) form.append("conversation_id", activeConvoId);
      const uploadRes = await fetch(`${base}/attachments`, {
        method: "POST",
        body: form,
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!uploadRes.ok) {
        const body = await uploadRes.json().catch(() => ({})) as { detail?: string };
        setVoiceError(body.detail || `Upload failed (${uploadRes.status})`);
        return;
      }
      const uploadData = await uploadRes.json() as { attachment_id: string };

      // Step 2: Transcribe via broker-routed endpoint
      const transcribeRes = await apiFetch("/chat/voice/transcribe", {
        method: "POST",
        body: JSON.stringify({
          artifact_id: uploadData.attachment_id,
          conversation_id: activeConvoId ?? undefined,
        }),
      }).catch(err => { throw err instanceof Error ? err : new Error(String(err)); });
      const transcribeData = await transcribeRes.json() as { transcript?: string };
      if (transcribeData.transcript) {
        setDraft(prev => prev ? `${prev} ${transcribeData.transcript}` : transcribeData.transcript!);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Transcription unavailable";
      setVoiceError(msg.includes("422") || msg.toLowerCase().includes("not configured")
        ? "Transcription unavailable: no STT provider is configured."
        : msg);
    } finally {
      setVoiceBusy(false);
    }
  }

  function removeAttachment(id: string) {
    setAttachments(prev => {
      const removed = prev.find(attachment => attachment.id === id);
      if (removed?.previewUrl) {
        URL.revokeObjectURL(removed.previewUrl);
        attachmentPreviewUrlsRef.current.delete(removed.previewUrl);
      }
      return prev.filter(attachment => attachment.id !== id);
    });
  }

  function clearAttachments() {
    setAttachments(prev => {
      prev.forEach(attachment => {
        if (attachment.previewUrl) {
          URL.revokeObjectURL(attachment.previewUrl);
          attachmentPreviewUrlsRef.current.delete(attachment.previewUrl);
        }
      });
      return [];
    });
  }

  function cycleReasoningEffort() {
    setSelectedReasoningEffort(current => {
      const index = REASONING_OPTIONS.findIndex(option => option.id === current);
      const next = REASONING_OPTIONS[(index + 1) % REASONING_OPTIONS.length]?.id ?? DEFAULT_REASONING_EFFORT;
      window.localStorage.setItem(REASONING_STORAGE_KEY, next);
      return next;
    });
  }

  async function sendMessage() {
    if (!draft.trim() || streaming) return;
    const text = draft.trim();
    setDraft("");
    const pendingAttachmentIds = attachments.map(a => a.id);
    clearAttachments();
    setUploadError(null);
    setMessages(prev => [
      ...prev,
      { role: "user", content: text, status: "complete" },
      { role: "assistant", content: "", status: "streaming", thinking: true },
    ]);
    streamingRef.current = true;
    setStreaming(true);

    const ab = new AbortController();
    abortRef.current = ab;
    let convoId = activeConvoId;

    type StreamEvent = {
      type: string; content?: string; conversation_id?: string; task_id?: string;
      event?: { type: string; tool?: string | null; summary?: string; error?: string; confidence?: number; iteration?: number; args_preview?: Record<string, unknown>; step?: { description?: string }; approval_ids?: string[]; goal?: string };
      artifact?: { artifact_id: string; title?: string; kind?: string; mime_type?: string; size_bytes?: number };
      structured_response?: StructuredResponse;
    };

    let partial = "";
    let createdTaskId: string | null = null;

    function handleStreamEvent(ev: StreamEvent) {
      if (ev.type === "conversation" && ev.conversation_id) {
        convoId = ev.conversation_id;
        onConvoCreated(ev.conversation_id);
      }
      if (ev.type === "token" && ev.content) {
        partial += ev.content;
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === "assistant") updated[updated.length - 1] = { ...last, content: partial, thinking: false };
          return updated;
        });
      } else if (ev.type === "trace" && ev.event && ev.event.type === "thinking") {
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === "assistant") {
            const existing = last.tool_traces ?? [];
            const hasLoopTrace = existing.some(t => t.tool === "agent.loop" && t.status === "streaming");
            updated[updated.length - 1] = {
              ...last,
              thinking: true,
              tool_traces: hasLoopTrace
                ? existing
                : [...existing, {
                    id: `thinking-agent.loop-${Date.now()}`,
                    tool: "agent.loop",
                    summary: "Reasoning over context, memory, tools, and next action",
                    status: "streaming",
                  }],
            };
          }
          return updated;
        });
      } else if (ev.type === "trace" && ev.event && ev.event.type === "reasoning_summary") {
        const te = ev.event;
        const summary = String(te.summary ?? "").trim();
        if (!summary) return;
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role !== "assistant") return updated;
          const existing = last.reasoning_summaries ?? [];
          const id = `reasoning-${te.iteration ?? existing.length + 1}-${Date.now()}`;
          updated[updated.length - 1] = {
            ...last,
            thinking: true,
            reasoning_summaries: [...existing, { id, iteration: te.iteration, summary, status: "streaming" }],
          };
          return updated;
        });
      } else if (ev.type === "trace" && ev.event) {
        const te = ev.event;
        const traceType = te.type ?? "";
        const tool = traceToolLabel(traceType, te);
        let summary = traceSummary(traceType, te);
        let traceStatus: MessageStatus = "complete";
        if (traceType === "tool_call" || traceType === "step_start" || traceType === "sub_agent_spawned" || traceType === "model_step" || traceType === "thinking") {
          traceStatus = "streaming";
        } else if (traceType === "tool_result" || traceType === "step_done" || traceType === "sub_agent_complete" || traceType === "route_decision" || traceType === "model_result") {
          traceStatus = "complete";
        } else if (traceType === "tool_error") {
          traceStatus = "error";
        } else if (traceType === "awaiting_approval") {
          traceStatus = "approval_pending";
        }
        const traceId = `${traceType}-${tool}-${Date.now()}`;
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role !== "assistant") return updated;
          const existing = last.tool_traces ?? [];
          if (traceType === "tool_result" || traceType === "tool_error") {
            const idx = [...existing].reverse().findIndex(t => t.tool === tool && t.status === "streaming");
            if (idx >= 0) {
              const actualIdx = existing.length - 1 - idx;
              const newTraces = [...existing];
              newTraces[actualIdx] = { ...newTraces[actualIdx], summary, status: traceStatus };
              return [...updated.slice(0, -1), { ...last, tool_traces: newTraces, thinking: false }];
            }
          }
          if (traceType === "step_done" || traceType === "model_result") {
            const matchTool = traceType === "model_result" ? "agent.loop" : "planner.step";
            const idx = [...existing].reverse().findIndex(t => t.tool === matchTool && t.status === "streaming");
            if (idx >= 0) {
              const actualIdx = existing.length - 1 - idx;
              const newTraces = [...existing];
              newTraces[actualIdx] = { ...newTraces[actualIdx], summary, status: "complete" };
              return [...updated.slice(0, -1), { ...last, tool_traces: newTraces, thinking: false }];
            }
          }
          return [...updated.slice(0, -1), { ...last, tool_traces: [...existing, { id: traceId, tool, summary, status: traceStatus }], thinking: false }];
        });
      } else if (ev.type === "artifact" && ev.artifact) {
        const a = ev.artifact;
        const ref: ArtifactRef = {
          id: a.artifact_id,
          title: a.title ?? "artifact",
          kind: a.kind ?? "file",
          mime_type: a.mime_type,
          size_bytes: a.size_bytes,
        };
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role !== "assistant") return updated;
          const existing = last.artifacts ?? [];
          if (existing.some(x => x.id === ref.id)) return updated;
          return [...updated.slice(0, -1), { ...last, artifacts: [...existing, ref] }];
        });
      } else if (ev.type === "structured_response" && ev.structured_response) {
        const sr = ev.structured_response;
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role !== "assistant") return updated;
          return [...updated.slice(0, -1), { ...last, structured_response: sr }];
        });
      } else if (ev.type === "task_created" && ev.task_id) {
        const taskId = ev.task_id;
        createdTaskId = taskId;
        setActiveTaskId(taskId);
        setActivityOpen(true);
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role !== "assistant") return updated;
          const existing = last.tool_traces ?? [];
          return [...updated.slice(0, -1), {
            ...last,
            status: "streaming",
            thinking: true,
            tool_traces: [
              ...existing,
              {
                id: `task-created-${taskId}`,
                tool: "task.runtime",
                summary: `Created task ${taskId.slice(0, 8)} and attached live activity stream`,
                status: "streaming",
              },
            ],
          }];
        });
      } else if (ev.type === "done") {
        setMessages(prev => {
          const updated = [...prev];
          const last = updated[updated.length - 1];
          if (last?.role === "assistant") {
            const traces = (last.tool_traces ?? []).map(trace =>
              !createdTaskId && trace.tool === "task.runtime" && trace.status === "streaming"
                ? { ...trace, summary: "Task stream finished", status: "complete" as MessageStatus }
                : trace
            );
            const reasoning = (last.reasoning_summaries ?? []).map(item => ({ ...item, status: "complete" as MessageStatus }));
            updated[updated.length - 1] = {
              ...last,
              status: createdTaskId ? "streaming" : "complete",
              content: partial || last.content,
              thinking: Boolean(createdTaskId),
              tool_traces: traces,
              reasoning_summaries: reasoning,
            };
          }
          return updated;
        });
        if (convoId && !createdTaskId) {
          const syncId = convoId;
          const streamed = partial;
          void (async () => {
            for (let attempt = 0; attempt < 8; attempt++) {
              try {
                const rawMsgs = (await apiFetch(`/chat/conversations/${syncId}/messages`).then(r => r.json())) as Array<Record<string, unknown>>;
                const hasAssistant = rawMsgs.some(
                  m => String(m.role) === "assistant" && String(m.content ?? "").trim(),
                );
                await loadMessagesFromServer(syncId, { streamedContent: streamed });
                if (hasAssistant || streamed.trim()) return;
              } catch {
                /* retry */
              }
              if (attempt < 7) await new Promise(r => setTimeout(r, 350));
            }
          })();
        }
      }
    }

    function consumeSseLine(line: string) {
      if (!line.startsWith("data: ")) return;
      try {
        handleStreamEvent(JSON.parse(line.slice(6)) as StreamEvent);
      } catch { /* bad JSON */ }
    }

    const myConvoId = convoId;

    try {
      const pendingAttachmentIds = attachments.map(a => a.id);
      clearAttachments();
      const resp = await apiFetch("/chat/message", {
        method: "POST",
        headers: { Accept: "text/event-stream" },
        body: JSON.stringify({
          message: text,
          conversation_id: convoId,
          model: selectedModel,
          mode: selectedMode,
          reasoning_effort: selectedReasoningEffort,
          persona_id: activePersonaId,
          attachment_ids: pendingAttachmentIds,
        }),
        signal: ab.signal,
      });

      const reader = resp.body?.getReader();
      if (!reader) return;

      const decoder = new TextDecoder();
      let sseBuffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        sseBuffer += decoder.decode(value, { stream: true });
        const lines = sseBuffer.split("\n");
        sseBuffer = lines.pop() ?? "";
        for (const line of lines) {
          // Skip stale events — user navigated to a different conversation
          if (activeConvoId !== myConvoId && convoId !== myConvoId) {
            ab.abort();
            return;
          }
          consumeSseLine(line);
        }
      }
      if (sseBuffer.trim()) consumeSseLine(sseBuffer.trim());
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        const errText = e instanceof Error ? e.message : "Something went wrong.";
        setMessages(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant") {
            return [...prev.slice(0, -1), { ...last, status: "error", content: last.content || errText }];
          }
          return [...prev, { role: "assistant", content: errText, status: "error" }];
        });
      }
    } finally {
      streamingRef.current = false;
      setStreaming(false);
    }
  }

  return (
    <div className="flex-1 flex min-w-0 relative overflow-hidden">
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <div className="px-6 h-[52px] flex items-center justify-between flex-shrink-0 border-b hairline" style={{ background: "var(--bg)" }}>
          <div className="flex items-center gap-3 min-w-0">
            <div className="relative">
            <button
              type="button"
              aria-haspopup="menu"
              aria-expanded={personaMenuOpen}
              onClick={() => setPersonaMenuOpen(open => !open)}
              className="flex items-center gap-2.5 px-2 py-1 rounded-md smooth hover:bg-[var(--surface-2)]"
            >
              <PersonaAvatar name={activePersona.name} color={activePersona.color} size={22}/>
              <span className="text-[14px] font-medium">{activePersona.name}</span>
              <IC.ChevronDown size={13} style={{ color: "var(--text-dim)" }}/>
            </button>
            {personaMenuOpen && (
              <div
                role="menu"
                className="absolute left-0 top-[34px] z-50 w-[280px] surface border border-soft rounded-lg shadow-xl overflow-hidden py-1"
              >
                {PERSONAS.map(persona => {
                  const selected = persona.id === activePersona.id;
                  return (
                    <button
                      key={persona.id}
                      type="button"
                      role="menuitemradio"
                      aria-checked={selected}
                      onClick={() => {
                        onPersonaChange(persona.id);
                        setPersonaMenuOpen(false);
                      }}
                      className="w-full px-3 py-2.5 text-left flex items-start gap-3 smooth hover:bg-[var(--surface-2)]"
                    >
                      <PersonaAvatar name={persona.name} color={persona.color} size={28}/>
                      <span className="flex-1 min-w-0">
                        <span className="flex items-center gap-2">
                          <span className="text-[13.5px] font-medium">{persona.name}</span>
                          {selected && <IC.Check size={13} style={{ color: "var(--accent)" }}/>}
                        </span>
                        <span className="block text-[12px] mt-0.5" style={{ color: "var(--text-dim)" }}>{persona.role}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
            </div>
          </div>
          <div className="flex items-center gap-1">
            {!isEmpty && activeTaskId && !activityOpen && (
              <button onClick={() => setActivityOpen(true)}
                      className="flex items-center gap-2 px-2.5 py-1.5 rounded-md smooth hover:bg-[var(--surface-2)]">
                {streaming ? <StatusDot status="working"/> : <IC.Activity size={13} style={{ color: "var(--text-dim)" }}/>}
                <span className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
                  {streaming ? "Working" : "Task"}
                </span>
              </button>
            )}
            <button className="btn btn-ghost btn-icon"><IC.More size={15}/></button>
          </div>
        </div>

        {/* Messages or empty state */}
        {isEmpty ? (
          <EmptyChatState persona={activePersona} onSubmit={q => { setDraft(q); setTimeout(() => void sendMessage(), 0); }} />
        ) : (
          <div className="flex-1 overflow-y-auto px-6 py-10">
            <div className="max-w-[1120px] mx-auto space-y-8">
              {messages.map((m, i) => (
                m.role === "user"
                  ? <UserMessage key={m.id ?? `user-${i}`} message={m} conversationId={activeConvoId ?? ""} onRefresh={() => { if (activeConvoId) void loadMessagesFromServer(activeConvoId); }} onBranch={(newConvoId) => { onConvoCreated(newConvoId); }}/>
                  : <AssistantMessage key={m.id ?? `assistant-${i}`} message={m} content={m.content} conversationId={activeConvoId ?? ""} status={m.status ?? "complete"} persona={activePersona} toolTraces={m.tool_traces} reasoningSummaries={m.reasoning_summaries} artifacts={m.artifacts} thinking={m.thinking} mode={m.mode} structuredResponse={m.structured_response} onRefresh={() => { if (activeConvoId) void loadMessagesFromServer(activeConvoId); }} onBranch={(newConvoId) => { onConvoCreated(newConvoId); }}/>
              ))}
              {streaming && messages[messages.length - 1]?.role !== "assistant" && (
                <div className="flex gap-4">
                  <PersonaAvatar name={activePersona.name} color={activePersona.color} size={28}/>
                  <div className="typing-wave mt-2"><span/><span/><span/></div>
                </div>
              )}
              <InlineTaskApprovals
                approvalIds={inlineApprovalIds}
                taskId={activeTaskId}
                onChanged={() => {
                  onApprovalsChanged();
                  if (activeConvoId) void loadMessagesFromServer(activeConvoId, { preserveLocal: true });
                }}
              />
              <div ref={bottomRef}/>
            </div>
          </div>
        )}

        {/* Composer */}
        <div className="px-6 pb-6 pt-2" style={{ background: "var(--bg)" }}>
          <div className="max-w-[1120px] mx-auto">
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={e => { if (e.target.files) void uploadFiles(e.target.files); e.target.value = ""; }}
            />
            <input
              ref={voiceInputRef}
              type="file"
              accept="audio/*"
              className="hidden"
              onChange={e => { if (e.target.files) void uploadVoice(e.target.files); e.target.value = ""; }}
            />
            <div className="composer-shell">
              {uploadError && (
                <div
                  className="mx-4 mt-3 flex items-center gap-2 rounded-md px-3 py-2 text-[12.5px]"
                  style={{ background: "var(--danger-bg, rgba(239,68,68,.1))", color: "var(--danger, #ef4444)", border: "1px solid var(--danger-border, rgba(239,68,68,.25))" }}
                  role="alert"
                >
                  <IC.X size={13} style={{ flexShrink: 0 }} />
                  <span className="flex-1">{uploadError}</span>
                  <button type="button" aria-label="Dismiss" onClick={() => setUploadError(null)} className="smooth hover:opacity-70"><IC.X size={13} /></button>
                </div>
              )}
              {voiceError && (
                <div
                  className="mx-4 mt-3 flex items-center gap-2 rounded-md px-3 py-2 text-[12.5px]"
                  style={{ background: "var(--danger-bg, rgba(239,68,68,.1))", color: "var(--danger, #ef4444)", border: "1px solid var(--danger-border, rgba(239,68,68,.25))" }}
                  role="alert"
                >
                  <IC.Mic size={13} style={{ flexShrink: 0 }} />
                  <span className="flex-1">{voiceError}</span>
                  <button type="button" aria-label="Dismiss" onClick={() => setVoiceError(null)} className="smooth hover:opacity-70"><IC.X size={13} /></button>
                </div>
              )}
              {attachments.length > 0 && (
                <div className="flex gap-2 overflow-x-auto px-4 pt-3 pb-2">
                  {attachments.map(a => (
                    <div
                      key={a.id}
                      className="relative flex h-[72px] min-w-[168px] max-w-[220px] items-center gap-2 rounded-md border border-soft surface px-2 py-2"
                    >
                      <div className="flex h-12 w-12 flex-shrink-0 items-center justify-center overflow-hidden rounded-md border border-soft" style={{ background: "var(--surface-2)" }}>
                        {a.previewUrl ? (
                          <img src={a.previewUrl} alt="" className="h-full w-full object-cover" />
                        ) : (
                          <IC.Audit size={20} style={{ color: "var(--text-dim)" }} />
                        )}
                      </div>
                      <div className="min-w-0 flex-1 pr-5">
                        <div className="truncate text-[12.5px] font-medium" style={{ color: "var(--text)" }}>{a.name}</div>
                        <div className="mt-0.5 text-[11px]" style={{ color: "var(--text-dim)" }}>{formatFileSize(a.size)}</div>
                      </div>
                      <button
                        type="button"
                        aria-label={`Remove ${a.name}`}
                        onClick={() => removeAttachment(a.id)}
                        className="absolute right-1.5 top-1.5 flex h-5 w-5 items-center justify-center rounded-full smooth hover:bg-[var(--surface-3)]"
                      >
                        <IC.X size={12} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <textarea
                value={draft}
                onChange={e => setDraft(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void sendMessage(); } }}
                placeholder={isEmpty ? "Ask Chronos anything…" : "Reply…"}
                rows={1}
                className="w-full bg-transparent px-5 pt-4 pb-2 text-[15px] outline-none resize-none"
                style={{ minHeight: 52, maxHeight: 200, color: "var(--text)" }}
              />
              <div className="flex items-center justify-between px-3 pb-2.5 pt-1">
                <div className="flex items-center gap-1">
                  <button type="button" aria-label="Attach files" className="btn btn-ghost btn-sm btn-icon" onClick={() => fileInputRef.current?.click()}><IC.Attach size={15}/></button>
                  <button
                    type="button"
                    aria-label="Transcribe audio (upload audio file)"
                    title="Upload an audio file to transcribe"
                    disabled={voiceBusy}
                    className={`btn btn-ghost btn-sm btn-icon${voiceBusy ? " opacity-50" : ""}`}
                    onClick={() => voiceInputRef.current?.click()}
                  >
                    {voiceBusy ? <IC.Refresh size={15} style={{ animation: "spin 1s linear infinite" }}/> : <IC.Mic size={15}/>}
                  </button>
                  <button className="btn btn-ghost btn-sm">
                    <IC.Sparkles size={14} style={{ color: "var(--accent)" }}/> Skills · 1
                  </button>
                  <button
                    type="button"
                    aria-label={`Reasoning effort: ${selectedReasoningEffort}`}
                    aria-pressed={selectedReasoningEffort !== DEFAULT_REASONING_EFFORT}
                    onClick={cycleReasoningEffort}
                    disabled={streaming}
                    className="btn btn-ghost btn-sm"
                    title={REASONING_OPTIONS.find(option => option.id === selectedReasoningEffort)?.title}
                    data-reasoning-effort={selectedReasoningEffort}
                  >
                    <IC.Lightbulb size={14}/>
                    Reasoning · {REASONING_OPTIONS.find(option => option.id === selectedReasoningEffort)?.label ?? selectedReasoningEffort}
                  </button>
                  <label className="sr-only" htmlFor="chat-model-select">Model</label>
                  <select
                    id="chat-model-select"
                    aria-label="Model"
                    value={modelsLoadError ? "unavailable" : selectedModel}
                    onChange={event => {
                      setSelectedModel(event.target.value);
                      window.localStorage.setItem(MODEL_STORAGE_KEY, event.target.value);
                    }}
                    disabled={streaming || !!modelsLoadError || chatModels.length === 0}
                    className="surface border border-soft rounded-md px-2 py-1.5 text-[12.5px] outline-none disabled:opacity-60 w-[300px] max-w-[42vw]"
                    data-selected-model={chatModels.find(model => model.id === selectedModel)?.model ?? selectedModel}
                    data-selected-model-status={chatModels.find(model => model.id === selectedModel)?.status ?? "unknown"}
                    style={{ color: "var(--text)" }}
                    title={modelsLoadError || modelOptionTitle(chatModels.find(model => model.id === selectedModel) ?? { id: selectedModel, label: selectedModel, model: selectedModel })}
                  >
                    {(modelsLoadError
                      ? [{ id: "unavailable", label: "Models unavailable", model: "" }]
                      : chatModels.length
                      ? chatModels
                      : [{ id: selectedModel, label: "Loading models", model: selectedModel }]
                    ).map(model => (
                      <option key={model.id} value={model.id}>{modelOptionText(model)}</option>
                    ))}
                  </select>
                  <label className="sr-only" htmlFor="chat-mode-select">Mode</label>
                  <select
                    id="chat-mode-select"
                    aria-label="Mode"
                    value={selectedMode}
                    onChange={event => {
                      setSelectedMode(event.target.value);
                      window.localStorage.setItem(MODE_STORAGE_KEY, event.target.value);
                    }}
                    disabled={streaming || !!modesLoadError || chatModes.length === 0}
                    className="surface border border-soft rounded-md px-2 py-1.5 text-[12.5px] outline-none disabled:opacity-60"
                    data-selected-mode-status={chatModes.find(mode => mode.id === selectedMode)?.status ?? "unknown"}
                    title={modesLoadError || modeOptionTitle(chatModes.find(mode => mode.id === selectedMode) ?? { id: selectedMode, label: selectedMode })}
                    style={{ color: "var(--text)" }}
                  >
                    {(modesLoadError ? [{ id: "default", label: "Modes unavailable", status: "unavailable" }] : chatModes).map(m => (
                      <option key={m.id} value={m.id}>{m.status && m.status !== "available" ? `${m.label} (${m.status})` : m.label}</option>
                    ))}
                  </select>
                </div>
                <div className="flex items-center gap-2">
                  {streaming ? (
                    <button onClick={() => abortRef.current?.abort()} className="btn btn-ghost btn-sm">
                      <IC.Stop size={14}/> Stop
                    </button>
                  ) : (
                    <button onClick={() => void sendMessage()} disabled={!draft.trim()}
                            className="btn btn-accent btn-sm">
                      <IC.ArrowUp size={14} stroke={2.2}/>
                    </button>
                  )}
                </div>
              </div>
            </div>
            <p className="text-center text-[11.5px] mt-3" style={{ color: "var(--text-faint)" }}>
              Chronos requires approval for external sends (email, social posts, publishing) and reports failed searches honestly.
            </p>
          </div>
        </div>
      </div>

      {activityOpen && (
        <ActivityDrawer
          taskId={activeTaskId}
          task={activeTask}
          events={activeTaskEvents}
          streamError={activeTaskStreamError}
          onClose={() => setActivityOpen(false)}
        />
      )}

      {/* ⌘K Command palette */}
      {paletteOpen && (
        <div
          role="dialog"
          aria-modal="true"
          className="fixed inset-0 z-50 flex items-start justify-center pt-[15vh]"
          style={{ background: "color-mix(in oklch, var(--text) 20%, transparent)" }}
          onClick={() => onPaletteOpenChange(false)}
        >
          <div
            className="surface border rounded-xl shadow-2xl w-full max-w-[560px] mx-4 overflow-hidden"
            style={{ borderColor: "var(--border)" }}
            onClick={e => e.stopPropagation()}
          >
            {/* Search input */}
            <div className="flex items-center gap-2.5 px-4 py-3 border-b hairline">
              <IC.Search size={15} style={{ color: "var(--text-dim)", flexShrink: 0 }}/>
              <input
                ref={paletteInputRef}
                type="text"
                value={paletteQuery}
                onChange={e => setPaletteQuery(e.target.value)}
                onKeyDown={handlePaletteKeyDown}
                placeholder="Search conversations, memory, tasks…"
                aria-label="Search"
                className="flex-1 bg-transparent text-[14px] outline-none"
                style={{ color: "var(--text)" }}
              />
              <kbd className="text-[11px] px-1.5 py-0.5 rounded border border-soft"
                   style={{ color: "var(--text-dim)", background: "var(--surface-2)" }}>Esc</kbd>
            </div>

            {/* Results */}
            <div className="max-h-[400px] overflow-y-auto">
              {paletteQuery.trim() && paletteGroupsWithIdx.length === 0 && (
                <div className="px-4 py-8 text-center text-[13.5px]" style={{ color: "var(--text-dim)" }}>
                  No results for &ldquo;{paletteQuery}&rdquo;
                </div>
              )}
              {paletteGroupsWithIdx.map(group => (
                <div key={group.type}>
                  <div className="px-4 pt-3 pb-1 text-[11.5px] font-medium uppercase tracking-wider"
                       style={{ color: "var(--text-dim)" }}>
                    {group.label}
                  </div>
                  {group.items.map(({ item: result, flatIdx }) => {
                    const isSelected = flatIdx === paletteSelectedIdx;
                    return (
                      <button
                        key={`${result.type}-${result.id}`}
                        onClick={() => handlePaletteSelect(result)}
                        className="w-full flex items-start gap-3 px-4 py-2.5 text-left smooth hover:bg-[var(--surface-2)]"
                        style={{ background: isSelected ? "var(--accent-soft)" : undefined }}
                      >
                        <div className="flex-1 min-w-0">
                          <div className="text-[13.5px] font-medium truncate">{result.title}</div>
                          {result.snippet && result.snippet !== result.title && (
                            <div className="text-[12px] truncate mt-0.5" style={{ color: "var(--text-dim)" }}>
                              {result.snippet}
                            </div>
                          )}
                        </div>
                        <IC.ArrowRight size={13} style={{ color: "var(--text-dim)", flexShrink: 0, marginTop: 3 }}/>
                      </button>
                    );
                  })}
                </div>
              ))}
              {!paletteQuery.trim() && (
                <div className="px-4 py-6 text-center text-[13px]" style={{ color: "var(--text-dim)" }}>
                  Type to search across conversations, memory, tasks, and more.
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Message Action Menu ──────────────────────────────────────────────────────

type MessageActionMenuProps = {
  message: Message;
  conversationId: string;
  onRefresh: () => void;
  onBranch: (newConvoId: string) => void;
};

function MessageActionMenu({ message, conversationId, onRefresh, onBranch }: MessageActionMenuProps) {
  const [open, setOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [editMode, setEditMode] = useState(false);
  const [editContent, setEditContent] = useState(message.content);
  // Fix 3: track in-flight actions to prevent double-submits
  const [inflight, setInflight] = useState<Set<string>>(new Set());
  const menuRef = useRef<HTMLDivElement>(null);
  // Fix 5: keep a ref to the trigger so we can return focus after Escape
  const triggerRef = useRef<HTMLButtonElement>(null);
  const mid = message.id;

  // Close menu on outside click
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  // Fix 6: wrap toast cleanup in useEffect so the timer is cancelled on unmount
  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(() => setToast(null), 3000);
    return () => clearTimeout(id);
  }, [toast]);

  function showToast(msg: string) {
    setToast(msg);
  }

  // Fix 3: helpers for in-flight tracking
  function startAction(key: string) {
    setInflight(prev => new Set(prev).add(key));
  }
  function endAction(key: string) {
    setInflight(prev => { const s = new Set(prev); s.delete(key); return s; });
  }
  function isBusy(key: string) { return inflight.has(key); }

  async function handlePin() {
    if (!mid || isBusy("pin")) return;
    const endpoint = message.pinned ? "unpin" : "pin";
    startAction("pin");
    try {
      await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}/${endpoint}`, { method: "POST" });
      onRefresh();
      showToast(message.pinned ? "Unpinned" : "Pinned");
    } catch { showToast("Failed"); }
    finally { endAction("pin"); }
    setOpen(false);
  }

  async function handleCopy() {
    try { await navigator.clipboard.writeText(message.content); showToast("Copied"); } catch { showToast("Failed to copy"); }
    setOpen(false);
  }

  async function handleEdit() {
    setEditMode(true);
    setEditContent(message.content);
    setOpen(false);
  }

  async function submitEdit() {
    if (!mid || isBusy("edit")) return;
    startAction("edit");
    try {
      await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}`, {
        method: "PATCH",
        body: JSON.stringify({ content: editContent }),
      });
      onRefresh();
      setEditMode(false);
      showToast("Saved");
    } catch { showToast("Failed to save"); }
    finally { endAction("edit"); }
  }

  async function handleBranch() {
    if (!mid || isBusy("branch")) return;
    startAction("branch");
    try {
      const res = await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}/branch`, { method: "POST" });
      const data = await res.json() as { conversation_id: string };
      onBranch(data.conversation_id);
      showToast("Branched into new conversation");
    } catch { showToast("Branch failed"); }
    finally { endAction("branch"); }
    setOpen(false);
  }

  async function handleSaveMemory() {
    if (!mid || isBusy("save-memory")) return;
    startAction("save-memory");
    try {
      await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}/save-to-memory`, {
        method: "POST",
        body: JSON.stringify({ scope: "org" }),
      });
      showToast("Saved to memory");
    } catch { showToast("Failed"); }
    finally { endAction("save-memory"); }
    setOpen(false);
  }

  async function handleConvertTask() {
    if (!mid || isBusy("convert-task")) return;
    startAction("convert-task");
    try {
      const res = await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}/convert-to-task`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      const data = await res.json() as { task_id: string };
      showToast(`Task created: ${data.task_id.slice(0, 8)}…`);
    } catch { showToast("Failed"); }
    finally { endAction("convert-task"); }
    setOpen(false);
  }

  async function handleConvertWorkflow() {
    if (!mid || isBusy("convert-workflow")) return;
    startAction("convert-workflow");
    try {
      const res = await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}/convert-to-workflow`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      const data = await res.json() as { workflow_id: string };
      showToast(`Workflow created: ${data.workflow_id.slice(0, 8)}…`);
    } catch { showToast("Failed"); }
    finally { endAction("convert-workflow"); }
    setOpen(false);
  }

  async function handleRegenerate() {
    if (!mid || isBusy("regenerate")) return;
    startAction("regenerate");
    try {
      await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}/regenerate`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      onRefresh();
      showToast("Regenerated");
    } catch { showToast("Regenerate failed"); }
    finally { endAction("regenerate"); }
    setOpen(false);
  }

  async function handleRetryFromHere() {
    if (!mid || isBusy("retry-from-here")) return;
    startAction("retry-from-here");
    try {
      await apiFetch(`/chat/conversations/${conversationId}/messages/${mid}/retry-from-here`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      onRefresh();
      showToast("Retried from here");
    } catch { showToast("Retry failed"); }
    finally { endAction("retry-from-here"); }
    setOpen(false);
  }

  function handleExport() {
    // Client-side: export the single message content as JSON
    const blob = new Blob([JSON.stringify({ id: mid, role: message.role, content: message.content }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `message-${mid ?? "unknown"}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Fix 6: revoke immediately after the click gesture — no timer needed
    URL.revokeObjectURL(url);
    setOpen(false);
  }

  // Fix 5: Escape closes menu and returns focus to trigger
  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape" && open) {
      e.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    }
  }

  if (editMode) {
    return (
      <div className="mt-2">
        <textarea
          value={editContent}
          onChange={e => setEditContent(e.target.value)}
          className="w-full border rounded-lg px-3 py-2 text-[14px] outline-none resize-none"
          style={{ borderColor: "var(--border)", background: "var(--surface)", color: "var(--text)", minHeight: 72 }}
          rows={3}
        />
        <div className="flex gap-2 mt-1.5">
          <button onClick={submitEdit} disabled={isBusy("edit")} className="btn btn-accent btn-sm">Save</button>
          <button onClick={() => setEditMode(false)} className="btn btn-ghost btn-sm">Cancel</button>
        </div>
      </div>
    );
  }

  return (
    // Fix 5 + Fix 7: focus-within keeps the trigger visible while the dropdown is open
    // and when keyboard focus moves inside the menu, without needing JS state.
    <div className="relative inline-block" ref={menuRef} onKeyDown={handleKeyDown}>
      {toast && (
        <div className="absolute -top-8 left-1/2 -translate-x-1/2 text-[12px] px-2.5 py-1 rounded-md whitespace-nowrap z-50"
             style={{ background: "var(--surface-2)", color: "var(--text-muted)", border: "1px solid var(--border-soft)" }}>
          {toast}
        </div>
      )}
      {/* Fix 5: ARIA trigger attributes */}
      <button
        ref={triggerRef}
        onClick={() => setOpen(o => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Message actions"
        className={`p-1 rounded-md opacity-100 smooth hover:bg-[var(--surface-2)] ${open ? "bg-[var(--surface-2)]" : ""}`}
        style={{ color: "var(--text-dim)" }}
        title="Message actions"
      >
        <IC.More size={14}/>
      </button>
      {/* Fix 5: role="menu" on dropdown */}
      {open && (
        <div role="menu" className="absolute right-0 top-7 z-40 rounded-xl shadow-lg border py-1 min-w-[160px]"
             style={{ background: "var(--surface)", borderColor: "var(--border)" }}>
          {/* Fix 3 + Fix 5: role="menuitem" + disabled when in-flight */}
          <button role="menuitem" onClick={handlePin} disabled={isBusy("pin")} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5 disabled:opacity-50" style={{ color: "var(--text)" }}>
            <IC.Lock size={13}/>{message.pinned ? "Unpin" : "Pin"}
          </button>
          <button role="menuitem" onClick={handleCopy} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5" style={{ color: "var(--text)" }}>
            <IC.External size={13}/>Copy
          </button>
          {message.role === "user" && (
            <button role="menuitem" onClick={handleEdit} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5" style={{ color: "var(--text)" }}>
              <IC.Pencil size={13}/>Edit
            </button>
          )}
          {message.role === "assistant" && (
            <button role="menuitem" onClick={handleRegenerate} disabled={isBusy("regenerate")} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5 disabled:opacity-50" style={{ color: "var(--text)" }}>
              <IC.Refresh size={13}/>Regenerate
            </button>
          )}
          <button role="menuitem" onClick={handleRetryFromHere} disabled={isBusy("retry-from-here")} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5 disabled:opacity-50" style={{ color: "var(--text)" }}>
            <IC.Refresh size={13}/>Retry from here
          </button>
          <button role="menuitem" onClick={handleBranch} disabled={isBusy("branch")} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5 disabled:opacity-50" style={{ color: "var(--text)" }}>
            <IC.ArrowRight size={13}/>Branch here
          </button>
          <button role="menuitem" onClick={handleSaveMemory} disabled={isBusy("save-memory")} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5 disabled:opacity-50" style={{ color: "var(--text)" }}>
            <IC.Memory size={13}/>Save to memory
          </button>
          <button role="menuitem" onClick={handleConvertTask} disabled={isBusy("convert-task")} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5 disabled:opacity-50" style={{ color: "var(--text)" }}>
            <IC.Briefcase size={13}/>Convert to task
          </button>
          <button role="menuitem" onClick={handleConvertWorkflow} disabled={isBusy("convert-workflow")} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5 disabled:opacity-50" style={{ color: "var(--text)" }}>
            <IC.Refresh size={13}/>Convert to workflow
          </button>
          <div className="mx-2 my-1 border-t" style={{ borderColor: "var(--border-soft)" }}/>
          <button role="menuitem" onClick={handleExport} className="w-full text-left px-3 py-1.5 text-[13px] smooth hover:bg-[var(--surface-2)] flex items-center gap-2.5" style={{ color: "var(--text-dim)" }}>
            <IC.Folder size={13}/>Export
          </button>
        </div>
      )}
    </div>
  );
}

// ─── User Message ─────────────────────────────────────────────────────────────

type MsgProps = { message: Message; conversationId: string; onRefresh: () => void; onBranch: (id: string) => void };

/** Chip for a single persisted attachment on a user message.
 *  Image attachments: fetches the blob via apiFetch (Bearer-authed) and renders
 *  a small thumbnail.  Non-image attachments: filename chip, same as before.
 */
function AttachmentChip({ artifact }: { artifact: ArtifactRef }) {
  const [thumbUrl, setThumbUrl] = useState<string | null>(null);
  const isImage = (artifact.mime_type ?? "").startsWith("image/");

  useEffect(() => {
    if (!isImage) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    apiFetch(`/artifacts/${artifact.id}/content`)
      .then(r => r.blob())
      .then(blob => {
        if (cancelled) return;  // unmounted/re-ran before fetch resolved
        objectUrl = URL.createObjectURL(blob);
        setThumbUrl(objectUrl);
      })
      .catch(() => { /* silently fall back to chip */ });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifact.id, isImage]);

  if (isImage && thumbUrl) {
    return (
      <div
        className="rounded-md border overflow-hidden"
        style={{ borderColor: "var(--border-soft)", width: 56, height: 56, flexShrink: 0 }}
        title={artifact.title}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={thumbUrl} alt={artifact.title} style={{ width: "100%", height: "100%", objectFit: "cover" }} />
      </div>
    );
  }

  return (
    <div
      className="flex items-center gap-1.5 rounded-md border px-2 py-1 text-[12px]"
      style={{ background: "var(--surface-2)", borderColor: "var(--border-soft)", color: "var(--text-muted)" }}
    >
      <IC.Attach size={12} style={{ flexShrink: 0, color: "var(--text-dim)" }} />
      <span className="max-w-[140px] truncate">{artifact.title}</span>
    </div>
  );
}

function UserMessage({ message, conversationId, onRefresh, onBranch }: MsgProps) {
  return (
    <div className="flex justify-end fadein group">
      <div className="max-w-[72%] min-w-0">
        {message.artifacts && message.artifacts.length > 0 && (
          <div className="mb-2 flex flex-wrap justify-end gap-1.5">
            {message.artifacts.map(a => (
              <AttachmentChip key={a.id} artifact={a} />
            ))}
          </div>
        )}
        <div className="rounded-xl px-4 py-3 text-[15px] leading-relaxed" style={{ background: "var(--surface-2)", color: "var(--text)" }}>
          {message.content}
        </div>
        <div className="mt-1.5 flex justify-end transition-opacity opacity-0 group-hover:opacity-100 focus-within:opacity-100">
          {message.pinned && <IC.Lock size={12} style={{ color: "var(--accent)" }}/>}
          <MessageActionMenu message={message} conversationId={conversationId} onRefresh={onRefresh} onBranch={onBranch}/>
        </div>
      </div>
    </div>
  );
}

function TraceRow({ trace }: { trace: ToolTrace }) {
  const [open, setOpen] = useState(false);
  const isRunning = trace.status === "streaming";
  const isError = trace.status === "error";
  const isPending = trace.status === "approval_pending";
  const dotColor = isRunning ? "var(--accent)" : isError ? "var(--danger)" : isPending ? "var(--warn)" : "var(--ok)";
  const toolLabel = trace.tool.replace(/[_]/g, ".").replace(/\./g, " › ");

  return (
    <div className="rounded-lg border overflow-hidden" style={{ borderColor: "var(--border-soft)" }}>
      <button
        className="w-full flex items-center gap-2.5 px-3 py-2 text-left smooth hover:bg-[var(--surface-2)]"
        style={{ background: "var(--surface)", color: "var(--text-muted)" }}
        onClick={() => setOpen(o => !o)}
      >
        <Dot color={dotColor} size={6} pulse={isRunning} ring={isRunning} />
        <span className="font-mono text-[11px]" style={{ color: "var(--text-dim)" }}>{toolLabel}</span>
        <span className="flex-1 text-[12.5px] truncate" style={{ color: "var(--text-muted)" }}>{trace.summary}</span>
        <IC.ChevronDown size={12} style={{ color: "var(--text-dim)", transform: open ? "rotate(180deg)" : undefined, transition: "transform .15s", flexShrink: 0 }}/>
      </button>
    </div>
  );
}

function ReasoningPanel({ summaries, streaming }: { summaries: ReasoningSummary[]; streaming: boolean }) {
  const [open, setOpen] = useState(true);
  if (!summaries.length) return null;
  const latest = summaries[summaries.length - 1];
  const isRunning = streaming && summaries.some(item => item.status === "streaming");

  return (
    <div className="mb-3 rounded-lg border overflow-hidden" style={{ borderColor: "var(--border-soft)", background: "var(--surface)" }}>
      <button
        type="button"
        onClick={() => setOpen(value => !value)}
        className="w-full flex items-center gap-2.5 px-3 py-2 text-left smooth hover:bg-[var(--surface-2)]"
        style={{ color: "var(--text-muted)" }}
      >
        <Dot color="var(--accent)" size={6} pulse={isRunning} ring={isRunning} />
        <span className="font-mono text-[11px]" style={{ color: "var(--text-dim)" }}>reasoning</span>
        <span className="flex-1 text-[12.5px] truncate" style={{ color: "var(--text-muted)" }}>
          {latest.iteration ? `Iteration ${latest.iteration}: ` : ""}{latest.summary.split("\n")[0].replace(/^[-*]\s*/, "")}
        </span>
        <IC.ChevronDown size={12} style={{ color: "var(--text-dim)", transform: open ? "rotate(180deg)" : undefined, transition: "transform .15s", flexShrink: 0 }}/>
      </button>
      {open && (
        <div className="px-3 pb-3 pt-1 space-y-2 border-t hairline">
          {summaries.map(item => (
            <div key={item.id} className="text-[12.5px] leading-relaxed whitespace-pre-wrap" style={{ color: "var(--text-muted)" }}>
              {item.iteration && <span className="font-mono text-[11px] mr-2" style={{ color: "var(--text-dim)" }}>#{item.iteration}</span>}
              {item.summary}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * VoicePlayButton — small speaker button on assistant messages.
 * Calls POST /chat/voice/speak via the broker, then plays the returned
 * audio artifact from GET /artifacts/{id}/content.
 * Shows an honest "TTS unavailable" tooltip when the provider is not configured.
 */
function VoicePlayButton({ text, conversationId }: { text: string; conversationId: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);

  // Stop current playback and revoke its blob URL — used on re-click and unmount.
  function stopAndRevoke() {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
  }

  // Revoke any outstanding blob URL when the component unmounts.
  useEffect(() => stopAndRevoke, []);

  async function handleSpeak() {
    if (busy) return;
    stopAndRevoke();  // stop any prior playback and free its URL
    setError(null);
    setBusy(true);
    try {
      const res = await apiFetch("/chat/voice/speak", {
        method: "POST",
        body: JSON.stringify({ text: text.slice(0, 4096), conversation_id: conversationId || undefined }),
      });
      const data = await res.json() as { audio_artifact_id?: string };
      if (!data.audio_artifact_id) { setError("No audio returned"); return; }

      // Fetch audio content through the artifact endpoint (auth-gated, tenant-scoped).
      const audioRes = await apiFetch(`/artifacts/${data.audio_artifact_id}/content`);
      const blob = await audioRes.blob();
      const url = URL.createObjectURL(blob);
      urlRef.current = url;
      const audio = new Audio(url);
      audioRef.current = audio;
      audio.addEventListener("ended", stopAndRevoke);
      audio.play().catch(() => { setError("Audio playback failed (browser blocked autoplay)"); stopAndRevoke(); });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg.includes("422") || msg.toLowerCase().includes("not configured")
        ? "TTS unavailable: no TTS provider configured"
        : `TTS error: ${msg.slice(0, 80)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        aria-label="Read aloud"
        title={error ?? "Read aloud"}
        disabled={busy}
        className="btn btn-ghost btn-sm btn-icon opacity-60 hover:opacity-100"
        onClick={() => void handleSpeak()}
      >
        {busy
          ? <IC.Refresh size={13} style={{ animation: "spin 1s linear infinite" }}/>
          : <IC.Volume size={13}/>}
      </button>
      {error && (
        <span className="text-[11px]" style={{ color: "var(--danger, #ef4444)" }} title={error}>
          <IC.Info size={11}/>
        </span>
      )}
    </span>
  );
}

function ArtifactCard({ artifact }: { artifact: ArtifactRef }) {
  const [busy, setBusy] = useState(false);
  const mime = artifact.mime_type ?? "";
  const isOpenable = mime.startsWith("text/html") || mime.includes("svg") || mime.startsWith("image/");
  const sizeLabel = artifact.size_bytes ? `${(artifact.size_bytes / 1024).toFixed(1)} KB` : "";

  async function fetchBlob(): Promise<Blob | null> {
    try {
      const res = await apiFetch(`/artifacts/${artifact.id}/content`);
      return await res.blob();
    } catch { return null; }
  }

  async function handleOpen() {
    // Open the tab synchronously (inside the click gesture) so popup blockers
    // don't kill it, then point it at the blob once fetched.
    const tab = window.open("about:blank", "_blank", "noopener,noreferrer");
    setBusy(true);
    const blob = await fetchBlob();
    setBusy(false);
    if (!blob) { tab?.close(); return; }
    const url = URL.createObjectURL(blob);
    if (tab) tab.location.href = url;
    else window.open(url, "_blank", "noopener,noreferrer");  // fallback
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  }

  async function handleDownload() {
    setBusy(true);
    const blob = await fetchBlob();
    setBusy(false);
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = artifact.title;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5_000);
  }

  return (
    <div className="rounded-xl border flex items-center gap-3 px-3.5 py-3"
         style={{ borderColor: "var(--border)", background: "var(--surface)" }}>
      <div className="w-9 h-9 rounded-lg flex items-center justify-center flex-shrink-0"
           style={{ background: "var(--accent-soft)", color: "var(--accent-text)" }}>
        <IC.Folder size={18}/>
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-[13.5px] font-medium truncate">{artifact.title}</div>
        <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>
          {artifact.kind}{sizeLabel ? ` · ${sizeLabel}` : ""}
        </div>
      </div>
      {isOpenable && (
        <button onClick={handleOpen} disabled={busy} className="btn btn-secondary btn-sm">
          <IC.External size={13}/> Open
        </button>
      )}
      <button onClick={() => window.dispatchEvent(new CustomEvent("chronos:open-artifact", { detail: { id: artifact.id } }))} className="btn btn-ghost btn-sm">Open here</button>
      <button onClick={handleDownload} disabled={busy} className="btn btn-ghost btn-sm">Download</button>
    </div>
  );
}

const STATUS_VARIANT: Record<string, "default" | "ok" | "warn" | "danger" | "info" | "accent"> = {
  complete: "ok", in_progress: "accent", needs_input: "warn", needs_approval: "warn",
  partial: "warn", blocked: "danger", failed: "danger", cancelled: "default",
};
const STATUS_LABEL: Record<string, string> = {
  complete: "Complete", in_progress: "In progress", needs_input: "Needs input",
  needs_approval: "Needs approval", partial: "Partially complete", blocked: "Blocked",
  failed: "Failed", cancelled: "Cancelled",
};

function StatusBanner({ sr }: { sr: StructuredResponse }) {
  if (sr.status === "complete") return null;
  const variant = STATUS_VARIANT[sr.status] ?? "info";
  return (
    <div className="flex items-center gap-2 mb-2">
      <Tag variant={variant}>{STATUS_LABEL[sr.status] ?? sr.status}</Tag>
      {sr.approval_status === "drafted_not_sent" && <Tag variant="warn">Not sent</Tag>}
      {sr.status !== "complete" && sr.confidence && <Tag variant="info">{sr.confidence} confidence</Tag>}
    </div>
  );
}

function InlineApprovalCard({ sr }: { sr: StructuredResponse }) {
  if (sr.approval_status !== "drafted_not_sent") return null;
  return (
    <div className="mt-3 surface border rounded-lg p-3 text-[13px]" style={{ borderColor: "var(--warn)" }}>
      <div className="flex items-center gap-2 mb-1">
        <IC.Lock size={13} style={{ color: "var(--warn)" }} />
        <span className="font-medium">Approval required</span>
      </div>
      <div style={{ color: "var(--text-muted)" }}>
        A draft is prepared but not sent. Review and approve it in this chat when the approval request appears.
      </div>
    </div>
  );
}

function InlineTaskApprovals({
  approvalIds,
  taskId,
  onChanged,
}: {
  approvalIds: string[];
  taskId: string | null;
  onChanged: () => void;
}) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const idsKey = approvalIds.join("|");

  useEffect(() => {
    let cancelled = false;
    async function loadInlineApprovals() {
      if (!approvalIds.length) {
        setApprovals([]);
        setError("");
        return;
      }
      setLoading(true);
      setError("");
      try {
        const rows = await Promise.all(
          approvalIds.map(id => apiFetch(`/approvals/${id}`).then(r => r.json()) as Promise<Approval>),
        );
        if (!cancelled) setApprovals(rows);
      } catch (exc) {
        if (!cancelled) setError(exc instanceof Error ? exc.message : "Unable to load approval details");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void loadInlineApprovals();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey]);

  const pending = approvals.filter(item => item.status === "pending");
  if (!approvalIds.length && approvals.length === 0) return null;
  if (!loading && pending.length === 0) return null;

  async function decide(id: string, decision: "approved" | "rejected", batch = false) {
    const key = batch ? `batch-${decision}` : id;
    setBusyId(key);
    setError("");
    try {
      await apiFetch(`/approvals/${id}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision, batch }),
      });
      setApprovals(prev => batch
        ? prev.map(item => item.status === "pending" ? { ...item, status: decision } : item)
        : prev.map(item => item.id === id ? { ...item, status: decision } : item));
      onChanged();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to update approval");
    } finally {
      setBusyId(null);
    }
  }

  const firstPending = pending[0];

  return (
    <div className="flex gap-4 fadein">
      <div className="w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0"
           style={{ background: "var(--warn-soft)", color: "var(--warn)" }}>
        <IC.Approvals size={15}/>
      </div>
      <div className="flex-1 min-w-0">
        <div className="surface border rounded-xl p-4" style={{ borderColor: "var(--warn)" }}>
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 mb-1">
                <Tag variant="warn">Approval required</Tag>
                {taskId && <span className="text-[11.5px] font-mono" style={{ color: "var(--text-dim)" }}>task {taskId.slice(0, 8)}</span>}
              </div>
              <div className="text-[14px] font-medium">Review this action before Chronos continues</div>
              <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
                Chronos is paused until you approve or reject the pending action below.
              </div>
            </div>
            {pending.length > 1 && firstPending && (
              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  onClick={() => void decide(firstPending.id, "approved", true)}
                  disabled={!!busyId}
                  className="btn btn-ok-soft btn-sm disabled:opacity-50"
                >
                  <IC.Check size={13} stroke={2.2}/> {busyId === "batch-approved" ? "Approving..." : "Approve all"}
                </button>
                <button
                  onClick={() => void decide(firstPending.id, "rejected", true)}
                  disabled={!!busyId}
                  className="btn btn-danger-soft btn-sm disabled:opacity-50"
                >
                  <IC.X size={13}/> {busyId === "batch-rejected" ? "Rejecting..." : "Reject all"}
                </button>
              </div>
            )}
          </div>

          {loading && (
            <div className="mt-4 text-[12.5px]" style={{ color: "var(--text-dim)" }}>Loading approval details...</div>
          )}
          {error && (
            <div className="mt-4 rounded-lg border px-3 py-2 text-[12.5px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>
              {error}
            </div>
          )}
          {!loading && pending.length > 0 && (
            <div className="mt-4 space-y-3">
              {pending.map(approval => {
                const payload = approval.action_payload ?? {};
                const args = (payload.args || {}) as Record<string, unknown>;
                const subject = String(args.subject ?? payload.subject ?? approval.action_type);
                const recipient = String(args.to ?? payload.to ?? "");
                const body = String(args.body ?? payload.body ?? "");
                return (
                  <div key={approval.id} className="rounded-lg border border-soft p-3" style={{ background: "var(--bg)" }}>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-[13.5px] font-medium truncate">{subject}</div>
                        <div className="mt-1 flex items-center gap-2 flex-wrap text-[12px]" style={{ color: "var(--text-dim)" }}>
                          <Tag>{approval.action_type}</Tag>
                          {recipient && <span>To {recipient}</span>}
                          {approval.requested_at && <span>{labelTime(approval.requested_at)}</span>}
                        </div>
                      </div>
                      <div className="flex items-center gap-2 flex-shrink-0">
                        <button
                          onClick={() => void decide(approval.id, "approved")}
                          disabled={!!busyId}
                          className="btn btn-ok-soft btn-sm disabled:opacity-50"
                        >
                          <IC.Check size={13} stroke={2.2}/> {busyId === approval.id ? "Saving..." : "Approve"}
                        </button>
                        <button
                          onClick={() => void decide(approval.id, "rejected")}
                          disabled={!!busyId}
                          className="btn btn-danger-soft btn-sm disabled:opacity-50"
                        >
                          <IC.X size={13}/> Reject
                        </button>
                      </div>
                    </div>
                    {body ? (
                      <div className="mt-3 max-h-40 overflow-auto rounded-md px-3 py-2 text-[12.5px] whitespace-pre-wrap" style={{ background: "var(--surface-2)", color: "var(--text-muted)" }}>
                        {body}
                      </div>
                    ) : (
                      <pre className="mt-3 max-h-40 overflow-auto rounded-md px-3 py-2 text-[11.5px]" style={{ background: "var(--surface-2)", color: "var(--text-muted)" }}>
                        {JSON.stringify(payload, null, 2)}
                      </pre>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ToolActivityLine({
  traces,
  reasoning,
  streaming,
}: {
  traces?: ToolTrace[];
  reasoning?: ReasoningSummary[];
  streaming: boolean;
}) {
  const [open, setOpen] = useState(false);
  const visibleTraces = traces ?? [];
  const visibleReasoning = reasoning ?? [];
  if (!visibleTraces.length && !visibleReasoning.length) return null;

  const running = visibleTraces.some(trace => trace.status === "streaming") ||
    visibleReasoning.some(item => item.status === "streaming");
  const errors = visibleTraces.filter(trace => trace.status === "error").length;
  const latestTrace = [...visibleTraces].reverse().find(trace => trace.status === "streaming") ?? visibleTraces[visibleTraces.length - 1];
  const latestReasoning = visibleReasoning[visibleReasoning.length - 1];
  const latestText = latestTrace?.summary || latestReasoning?.summary || "";
  const label = running || streaming ? "Ran a command, used a tool" : errors ? `${errors} tool issue${errors === 1 ? "" : "s"}` : "Used tools";
  const countParts = [
    visibleTraces.length ? `${visibleTraces.length} tool${visibleTraces.length === 1 ? "" : "s"}` : "",
    visibleReasoning.length ? `${visibleReasoning.length} reasoning update${visibleReasoning.length === 1 ? "" : "s"}` : "",
  ].filter(Boolean).join(" · ");

  return (
    <div className="mt-3 text-[13px]" style={{ color: "var(--text-dim)" }}>
      <button
        type="button"
        onClick={() => setOpen(value => !value)}
        className="flex min-w-0 max-w-full items-center gap-2 rounded-md px-0 py-0.5 text-left smooth hover:text-[var(--text-muted)]"
        style={{ color: "var(--text-dim)" }}
      >
        <Dot color={errors ? "var(--danger)" : running ? "var(--accent)" : "var(--text-faint)"} size={5} pulse={running} ring={running} />
        <span className="font-medium">{label}</span>
        {countParts && <span>{countParts}</span>}
        {latestText && (
          <>
            <span aria-hidden="true">·</span>
            <span className="truncate">{latestText.replace(/\s+/g, " ")}</span>
          </>
        )}
        <IC.ChevronDown size={12} style={{ transform: open ? "rotate(180deg)" : undefined, transition: "transform .15s", flexShrink: 0 }} />
      </button>
      {open && (
        <div className="mt-2 rounded-lg px-3 py-2.5 space-y-2" style={{ background: "var(--surface-2)" }}>
          {visibleReasoning.map(item => (
            <div key={item.id} className="min-w-0">
              <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>
                {item.iteration ? `Reasoning ${item.iteration}` : "Reasoning"}
              </div>
              <div className="mt-0.5 whitespace-pre-wrap text-[12.5px] leading-relaxed" style={{ color: "var(--text-dim)" }}>{item.summary}</div>
            </div>
          ))}
          {visibleTraces.map(trace => (
            <div key={trace.id} className="flex min-w-0 items-baseline gap-2">
              <span className="shrink-0 font-mono text-[12px]" style={{ color: "var(--text-muted)" }}>
                {trace.status === "streaming" ? "Running" : trace.status === "error" ? "Failed" : "Ran"}
              </span>
              <span className="shrink-0 font-mono text-[12px]" style={{ color: "var(--text-dim)" }}>
                {trace.tool.replace(/[_]/g, ".").replace(/\./g, " › ")}
              </span>
              <span className="min-w-0 truncate text-[12.5px]" style={{ color: "var(--text-dim)" }}>{trace.summary}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AssistantMessage({ message, content, status, persona, toolTraces, reasoningSummaries, artifacts, thinking, mode, structuredResponse, conversationId, onRefresh, onBranch }: { message: Message; content: string; status: MessageStatus; persona: typeof PERSONAS[0]; toolTraces?: ToolTrace[]; reasoningSummaries?: ReasoningSummary[]; artifacts?: ArtifactRef[]; thinking?: boolean; mode?: string; structuredResponse?: StructuredResponse | null; conversationId: string; onRefresh: () => void; onBranch: (id: string) => void }) {
  const hasTraces = !!(toolTraces && toolTraces.length > 0);
  const hasReasoning = !!(reasoningSummaries && reasoningSummaries.length > 0);
  const isStreaming = status === "streaming";
  const sr = structuredResponse ?? null;
  // Plain answers stay plain. Cards only for real work or unfinished/abnormal state.
  const showCards = !!sr && !isStreaming &&
    (sr.response_type === "task_complete" || sr.status !== "complete");
  return (
    <div className="fadein group">
      <div className="max-w-[920px] min-w-0">
        <div className="mb-1.5 flex items-baseline gap-2">
          {(mode && mode !== "default") || status === "error" || message.pinned ? (
            <span className="sr-only">{persona.name}</span>
          ) : null}
          {mode && mode !== "default" && <Tag variant="info">{mode}</Tag>}
          {status === "error" && <Tag variant="danger">Error</Tag>}
          {message.pinned && <IC.Lock size={12} style={{ color: "var(--accent)" }}/>}
          {/* Fix 7: rely on Tailwind group-hover + focus-within instead of JS state */}
          {!isStreaming && (
            <div className="ml-auto flex items-center gap-1 transition-opacity opacity-0 group-hover:opacity-100 focus-within:opacity-100">
              {content && <VoicePlayButton text={content} conversationId={conversationId}/>}
              <MessageActionMenu message={message} conversationId={conversationId} onRefresh={onRefresh} onBranch={onBranch}/>
            </div>
          )}
        </div>

        {showCards && <StatusBanner sr={sr!} />}

        {/* Thinking indicator — shown during the (non-streaming) model call */}
        {isStreaming && thinking && !content && (
          <div className="flex items-center gap-2 text-[13px] shimmer-text" style={{ color: "var(--text-dim)" }}>
            <Dot color="var(--accent)" size={6} pulse ring /> Thinking…
          </div>
        )}

        {/* Answer */}
        {(content || (isStreaming && !thinking)) && (
          <div className="prose-body" style={{ color: "var(--text)" }}>
            {content}
            {isStreaming && !content && hasTraces
              ? null  /* traces visible; no extra caret until tokens arrive */
              : isStreaming && !content && thinking
              ? null  /* thinking indicator is showing */
              : isStreaming && <span className="caret ml-0.5" style={{ borderLeft: "2px solid var(--text)" }}>&nbsp;</span>}
          </div>
        )}

        {(hasTraces || hasReasoning) && <ToolActivityLine traces={toolTraces} reasoning={reasoningSummaries} streaming={isStreaming} />}

        {/* Artifacts — openable / downloadable files Chronos produced */}
        {artifacts && artifacts.length > 0 && (
          <div className="mt-3 space-y-2">
            {artifacts.map(a => <ArtifactCard key={a.id} artifact={a} />)}
          </div>
        )}

        {showCards && <InlineApprovalCard sr={sr!} />}

        {/* Sources — project knowledge citations grounding this answer */}
        {message.citations && message.citations.length > 0 && (
          <div className="mt-3 surface border border-soft rounded-lg p-2.5">
            <div className="text-[11px] font-medium mb-1.5" style={{ color: "var(--text-dim)" }}>Sources</div>
            <div className="space-y-1">
              {message.citations.map((c, i) => {
                const marker = (c.marker as string) || `S${i + 1}`;
                const title = (c.source_title as string) || (c.text as string) || "Untitled source";
                const snippet = (c.snippet as string) || "";
                return (
                  <details key={i} className="text-[12.5px]">
                    <summary className="cursor-pointer smooth hover:text-[var(--text)]" style={{ color: "var(--text-muted)" }}>
                      <span style={{ color: "var(--accent)" }}>[{marker}]</span> {title}
                    </summary>
                    {snippet && (
                      <div className="mt-1 pl-3 text-[12px] whitespace-pre-wrap" style={{ color: "var(--text-dim)" }}>{snippet}</div>
                    )}
                  </details>
                );
              })}
            </div>
          </div>
        )}

        {/* Typing wave: streaming, nothing else to show yet */}
        {isStreaming && !content && !hasTraces && !thinking && (
          <div className="typing-wave mt-2"><span/><span/><span/></div>
        )}

        {/* Working state — task running, traces/thinking cleared, answer not streamed yet */}
        {isStreaming && !content && hasTraces && !thinking && (
          <div className="flex items-center gap-2 text-[13px] mt-2 shimmer-text" style={{ color: "var(--text-dim)" }}>
            <Dot color="var(--accent)" size={6} pulse ring /> Still working…
          </div>
        )}
      </div>
    </div>
  );
}

function EmptyChatState({ persona, onSubmit }: { persona: typeof PERSONAS[0]; onSubmit: (q: string) => void }) {
  const suggestions = [
    { icon: <IC.Mail size={16}/>, label: "Draft outreach to a list of leads", q: "Draft personalized outreach to 20 Series B SaaS leads" },
    { icon: <IC.Folder size={16}/>, label: "Summarize this week's customer calls", q: "Summarize this week's customer calls and pull common themes." },
    { icon: <IC.Lightbulb size={16}/>, label: "Research a market", q: "Research the data observability market — top 5 players and how they compare." },
    { icon: <IC.Sparkles size={16}/>, label: "Rewrite our ICP from recent calls", q: "Rewrite our ICP based on recent customer calls." },
  ];
  return (
    <div className="flex-1 flex flex-col items-center justify-center px-6 overflow-y-auto">
      <div className="max-w-[680px] w-full pb-20">
        <div className="flex items-center gap-3 mb-6">
          <PersonaAvatar name={persona.name} color={persona.color} size={44}/>
          <div className="text-[13px]" style={{ color: "var(--text-dim)" }}>
            Talking to <span style={{ color: "var(--text)" }}>{persona.name}</span> · {persona.role}
          </div>
        </div>
        <h1 className="h-display mb-2" style={{ letterSpacing: "-0.03em" }}>What can I help with?</h1>
        <p className="text-[15px] mb-8" style={{ color: "var(--text-dim)" }}>Ask anything, or pick one to get started.</p>
        <div className="grid grid-cols-2 gap-3">
          {suggestions.map((s, i) => (
            <button key={i} className="suggestion fadeup" style={{ animationDelay: `${i * 50}ms` }}
                    onClick={() => onSubmit(s.q)}>
              <div className="w-8 h-8 rounded-md flex items-center justify-center flex-shrink-0"
                   style={{ background: "var(--surface-2)", color: "var(--text-muted)" }}>
                {s.icon}
              </div>
              <span>{s.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function ActivityDrawer({
  taskId,
  task,
  events,
  streamError,
  onClose,
}: {
  taskId: string | null;
  task: Task | null;
  events: TaskStreamEvent[];
  streamError: string;
  onClose: () => void;
}) {
  const steps = Array.isArray(task?.plan) ? task.plan : task?.plan?.steps ?? [];
  const currentStep = task?.current_step ?? 0;
  const status = taskStatus(task, events, streamError);
  const approvalEvent = [...events].reverse().find(event => event.type === "awaiting_approval");
  const failureEvent = [...events].reverse().find(event => event.type === "task_failed");
  const completionEvent = [...events].reverse().find(event => event.type === "task_complete");

  return (
    <aside className="flex-shrink-0 flex flex-col border-l hairline slidein" style={{ width: 400, background: "var(--bg)" }}>
      <div className="px-5 h-[52px] flex items-center justify-between border-b hairline">
        <div className="flex items-center gap-2.5">
          <StatusDot status={status.dot}/>
          <span className="text-[14px] font-semibold">{status.label}</span>
        </div>
        <button onClick={onClose} className="btn btn-ghost btn-icon"><IC.X size={16}/></button>
      </div>

      <div className="flex-1 overflow-y-auto p-5">
        {!taskId ? (
          <EmptyState icon={<IC.Activity size={20}/>} title="No active task" sub="A live task view appears here when Chronos starts a job from chat."/>
        ) : (
          <div className="space-y-5">
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-wider mb-2" style={{ color: "var(--text-dim)" }}>
                Live task
              </div>
              <h2 className="text-[15px] font-semibold leading-snug">{task?.goal ?? "Connecting to task stream..."}</h2>
              <div className="mt-2 flex items-center gap-2 text-[12px]" style={{ color: "var(--text-dim)" }}>
                <span className="inline-ref">{taskId.slice(0, 8)}</span>
                <span>Step {Math.min(currentStep, steps.length)} of {steps.length || "..."}</span>
              </div>
            </div>

            {streamError ? (
              <div className="rounded-lg border px-3 py-2 text-[12.5px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>
                {streamError}
              </div>
            ) : null}

            {approvalEvent ? (
              <div className="rounded-lg border px-3 py-3" style={{ borderColor: "var(--warn)", background: "var(--warn-soft)" }}>
                <div className="flex items-center gap-2 text-[13px] font-semibold" style={{ color: "var(--warn)" }}>
                  <IC.Approvals size={14}/> Waiting for approval
                </div>
                <p className="mt-1 text-[12.5px] leading-relaxed" style={{ color: "var(--text-muted)" }}>
                  {approvalEvent.approval_ids?.length ?? 0} approval request(s) are waiting in chat.
                </p>
              </div>
            ) : null}

            {failureEvent ? (
              <div className="rounded-lg border px-3 py-3" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)" }}>
                <div className="flex items-center gap-2 text-[13px] font-semibold" style={{ color: "var(--danger)" }}>
                  <IC.Info size={14}/> Task failed
                </div>
                <p className="mt-1 text-[12.5px] leading-relaxed" style={{ color: "var(--danger)" }}>{formatTaskError(failureEvent.error)}</p>
              </div>
            ) : null}

            {completionEvent ? (
              <div className="rounded-lg border px-3 py-3" style={{ borderColor: "var(--ok)", background: "var(--ok-soft)" }}>
                <div className="flex items-center gap-2 text-[13px] font-semibold" style={{ color: "var(--ok)" }}>
                  <IC.Check size={14}/> Task complete
                </div>
              </div>
            ) : null}

            <div className="space-y-2.5">
              <div className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-dim)" }}>
                Progress
              </div>
              {steps.length === 0 ? (
                <div className="surface border border-soft rounded-lg px-3 py-3 text-[13px]" style={{ color: "var(--text-dim)" }}>
                  Connecting to the task plan...
                </div>
              ) : steps.map((step, index) => {
                const state = stepState(step, index, currentStep, task?.status, events);
                return (
                  <div key={step.id} className="surface border border-soft rounded-lg px-3 py-3">
                    <div className="flex items-start gap-3">
                      <StepIcon state={state}/>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2">
                          <div className="text-[13.5px] font-medium truncate">{step.description}</div>
                          {step.tool ? <Tag>{step.tool}</Tag> : null}
                        </div>
                        <div className="mt-1 text-[12px]" style={{ color: "var(--text-dim)" }}>
                          {stepStateLabel(state)}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>

            <div className="space-y-2.5">
              <div className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-dim)" }}>
                Events
              </div>
              {events.length === 0 ? (
                <div className="flex items-center gap-2 text-[13px]" style={{ color: "var(--text-dim)" }}>
                  <div className="typing-wave"><span/><span/><span/></div>
                  Waiting for activity...
                </div>
              ) : events.slice(-8).reverse().map((event, index) => (
                <div key={`${event.type}-${event.ts ?? index}`} className="text-[12.5px] leading-relaxed border-l pl-3" style={{ borderColor: eventColor(event), color: "var(--text-muted)" }}>
                  <span className="font-medium" style={{ color: "var(--text)" }}>{eventLabel(event)}</span>
                  {event.ts || event.created_at ? <span style={{ color: "var(--text-dim)" }}> · {new Date(event.ts ?? event.created_at ?? "").toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span> : null}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

function mergeTaskEvent(task: Task | null, event: TaskStreamEvent): Task | null {
  if (!task) return task;
  if (event.type === "step_start" && typeof event.step_index === "number") {
    return { ...task, status: "running", current_step: event.step_index };
  }
  if (event.type === "step_done" && typeof event.step_index === "number") {
    return { ...task, status: "running", current_step: Math.max(task.current_step, event.step_index + 1) };
  }
  if (event.type === "awaiting_approval") return { ...task, status: "awaiting_approval" };
  if (event.type === "task_failed") return { ...task, status: "failed" };
  if (event.type === "task_complete") {
    const steps = Array.isArray(task.plan) ? task.plan : task.plan?.steps;
    return { ...task, status: "complete", current_step: steps?.length ?? task.current_step };
  }
  return task;
}

function taskStatus(task: Task | null, events: TaskStreamEvent[], streamError: string) {
  if (streamError) return { label: "Stream interrupted", dot: "error" };
  if (!task) return { label: "Connecting...", dot: "working" };
  if (task.status === "awaiting_approval" || events.some(event => event.type === "awaiting_approval")) return { label: "Waiting on approval", dot: "awaiting" };
  if (task.status === "failed") return { label: "Stopped", dot: "failed" };
  if (task.status === "complete") return { label: "Complete", dot: "done" };
  if (task.status === "queued" || task.status === "pending" || task.status === "planning") return { label: "Queued", dot: "queued" };
  return { label: "Working...", dot: "working" };
}

function stepState(step: TaskStep, index: number, currentStep: number, taskStatusValue: string | undefined, events: TaskStreamEvent[]) {
  if (events.some(event => event.type === "task_failed" && event.step?.id === step.id)) return "failed";
  if (events.some(event => event.type === "awaiting_approval" && event.step_id === step.id)) return "waiting";
  if (taskStatusValue === "awaiting_approval" && step.action === "approval_gate" && index === currentStep) return "waiting";
  if (taskStatusValue === "complete" || index < currentStep) return "done";
  if (index === currentStep && taskStatusValue === "running") return "active";
  return "queued";
}

function StepIcon({ state }: { state: string }) {
  if (state === "done") return <IC.Check size={16} stroke={2.2} style={{ color: "var(--ok)" }}/>;
  if (state === "waiting") return <IC.Approvals size={16} style={{ color: "var(--warn)" }}/>;
  if (state === "failed") return <IC.Info size={16} style={{ color: "var(--danger)" }}/>;
  if (state === "active") return <Dot color="var(--accent)" size={10} pulse ring/>;
  return <Dot color="var(--text-faint)" size={8}/>;
}

function stepStateLabel(state: string) {
  return ({ done: "Finished", waiting: "Waiting for approval", failed: "Failed", active: "In progress", queued: "Queued" } as Record<string, string>)[state] ?? state;
}

function eventLabel(event: TaskStreamEvent) {
  if (event.summary) return event.summary;
  if (event.type === "step_start") return `Started: ${event.step?.description ?? event.step?.id ?? "step"}`;
  if (event.type === "step_done") return `Finished: ${event.step?.description ?? event.step?.id ?? "step"}`;
  if (event.type === "step_retry") return `Retry ${event.attempt ?? ""}: ${event.step?.description ?? event.step?.id ?? "step"}`;
  if (event.type === "awaiting_approval") return "Approval requested";
  if (event.type === "task_failed") return `Failed: ${event.error ?? "unknown error"}`;
  if (event.type === "task_complete") return "Task completed";
  if (event.type === "approval_decided") return "Approval decision recorded";
  return event.type.replaceAll("_", " ");
}

function eventColor(event: TaskStreamEvent) {
  if (event.type === "task_failed") return "var(--danger)";
  if (event.type === "awaiting_approval") return "var(--warn)";
  if (event.type === "task_complete" || event.type === "step_done") return "var(--ok)";
  return "var(--border)";
}

// ─── Activity Screen ──────────────────────────────────────────────────────────
function ActivityScreen() {
  const [mode, setMode] = useState<"jobs" | "actions">("jobs");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [actions, setActions] = useState<ActivityAction[]>([]);
  const [events, setEvents] = useState<ActivityAction[]>([]);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionsLoading, setActionsLoading] = useState(true);
  const [actionType, setActionType] = useState("all");
  const [actionStatus, setActionStatus] = useState("all");
  const [actionQuery, setActionQuery] = useState("");

  const loadTasks = useCallback(async () => {
    setLoading(true);
    await apiFetch("/tasks/")
      .then(r => r.json())
      .then((data: Task[]) => setTasks(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const loadActions = useCallback(async () => {
    setActionsLoading(true);
    const params = new URLSearchParams();
    if (actionType !== "all") params.set("type", actionType);
    if (actionStatus !== "all") params.set("status", actionStatus);
    if (actionQuery.trim()) params.set("query", actionQuery.trim());
    params.set("limit", "200");
    await apiFetch(`/activity/actions?${params.toString()}`)
      .then(r => r.json())
      .then((data: ActivityAction[]) => setActions(data))
      .catch(() => setActions([]))
      .finally(() => setActionsLoading(false));
  }, [actionQuery, actionStatus, actionType]);

  useEffect(() => { void loadTasks(); }, [loadTasks]);
  useEffect(() => { void loadActions(); }, [loadActions]);

  useEffect(() => {
    if (!activeTaskId) { setActiveTask(null); setEvents([]); return; }
    apiFetch(`/tasks/${activeTaskId}`)
      .then(r => r.json())
      .then((data: Task) => setActiveTask(data))
      .catch(() => setActiveTask(null));
    apiFetch(`/tasks/${activeTaskId}/events`)
      .then(r => r.json())
      .then((data: ActivityAction[]) => setEvents(data))
      .catch(() => setEvents([]));
  }, [activeTaskId]);

  const jobFilters = [
    { id: "all", label: "All", count: tasks.length },
    { id: "running", label: "Working", count: tasks.filter(t => t.status === "running").length },
    { id: "awaiting_approval", label: "Waiting on you", count: tasks.filter(t => t.status === "awaiting_approval").length },
    { id: "complete", label: "Done", count: tasks.filter(t => t.status === "complete").length },
    { id: "failed", label: "Stopped", count: tasks.filter(t => t.status === "failed").length },
  ];
  const [jobFilter, setJobFilter] = useState("all");
  const filteredTasks = jobFilter === "all" ? tasks : tasks.filter(t => t.status === jobFilter);

  const statusLabel: Record<string, string> = {
    queued: "Queued", running: "Working", awaiting_approval: "Waiting on you", complete: "Done", failed: "Stopped", pending: "Queued",
  };

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <PageHeader
        title="Activity"
        subtitle="Everything Chronos has done — your jobs and the individual actions inside them."
        right={
          <div className="flex items-center gap-1 surface border border-soft rounded-lg p-1">
            {[{ id: "jobs", label: "Jobs", icon: <IC.Briefcase size={13}/> }, { id: "actions", label: "Every action", icon: <IC.Activity size={13}/> }].map(m => (
              <button key={m.id} onClick={() => setMode(m.id as "jobs" | "actions")}
                      className="px-3 py-1.5 rounded-md text-[13px] font-medium smooth flex items-center gap-1.5 whitespace-nowrap"
                      style={{ background: mode === m.id ? "var(--surface-2)" : "transparent", color: mode === m.id ? "var(--text)" : "var(--text-muted)" }}>
                {m.icon} {m.label}
              </button>
            ))}
          </div>
        }
      />

      {mode === "jobs" && (
        <>
          <div className="px-10 pb-4 flex items-center gap-1 flex-wrap">
            {jobFilters.map(f => (
              <button key={f.id} onClick={() => setJobFilter(f.id)}
                      className="px-3 py-1.5 rounded-md text-[13px] font-medium smooth whitespace-nowrap"
                      style={{ background: jobFilter === f.id ? "var(--surface-2)" : "transparent", color: jobFilter === f.id ? "var(--text)" : "var(--text-muted)" }}>
                {f.label}
                <span className="ml-1.5 text-[11.5px]" style={{ color: "var(--text-faint)" }}>{f.count}</span>
              </button>
            ))}
          </div>
          <div className="px-10 pb-10 space-y-2.5">
            {loading && <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading…</p>}
            {!loading && filteredTasks.length === 0 && (
              <EmptyState icon={<IC.Activity size={20}/>} title="No jobs yet" sub="Jobs appear here when you ask Chronos to do something."/>
            )}
            {filteredTasks.map(t => {
              const sl = statusLabel[t.status] ?? t.status;
              const statusColor = { running: "var(--accent-text)", awaiting_approval: "var(--warn)", failed: "var(--danger)", complete: "var(--ok)" }[t.status] ?? "var(--text-muted)";
              return (
                <div key={t.id} className="surface border border-soft rounded-lg overflow-hidden">
                  <button onClick={() => setActiveTaskId(activeTaskId === t.id ? null : t.id)} className="w-full p-4 smooth hover:bg-[var(--surface-2)] cursor-pointer flex items-center gap-4 text-left">
                    <div className="flex-shrink-0">
                      {t.status === "running"             && <Dot color="var(--accent)" size={10} pulse ring/>}
                      {t.status === "awaiting_approval"   && <Dot color="var(--warn)" size={10}/>}
                      {t.status === "complete"            && <IC.Check size={16} stroke={2.2} style={{ color: "var(--ok)" }}/>}
                      {t.status === "failed"              && <IC.Info size={16} style={{ color: "var(--danger)" }}/>}
                      {(t.status === "queued" || t.status === "pending") && <Dot color="var(--text-faint)" size={8}/>}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[14.5px] font-medium mb-1 truncate">{t.goal}</div>
                      <div className="flex items-center gap-2 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
                        <span style={{ color: statusColor }}>{sl}</span>
                        <span>·</span>
                        <span>{t.iteration_count ?? 0} iterations</span>
                      </div>
                    </div>
                    <IC.Chevron size={16} style={{ color: "var(--text-faint)", transform: activeTaskId === t.id ? "rotate(90deg)" : "none" }}/>
                  </button>
                  {activeTaskId === t.id && (
                    <div className="border-t hairline px-5 py-4 space-y-3">
                      {activeTask?.error && <div className="text-[12.5px]" style={{ color: "var(--danger)" }}>{activeTask.error}</div>}
                      <TaskTimeline task={activeTask || t} events={events}/>
                      <TaskResult task={activeTask || t}/>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}

      {mode === "actions" && (
        <div className="px-10 pb-10 space-y-4">
          <div className="surface border border-soft rounded-lg p-3 flex items-center gap-2 flex-wrap">
            <select value={actionType} onChange={e => setActionType(e.target.value)} className="surface border border-soft rounded-md px-2.5 py-1.5 text-[12.5px] outline-none" style={{ color: "var(--text)" }}>
              <option value="all">All event types</option>
              <option value="tool_call">Tool calls</option>
              <option value="tool_result">Tool results</option>
              <option value="tool_error">Tool errors</option>
              <option value="awaiting_approval">Awaiting approval</option>
              <option value="artifact">Artifacts</option>
              <option value="sub_agent_spawned">Sub-agents</option>
              <option value="task_complete">Completed tasks</option>
              <option value="task_failed">Failed tasks</option>
            </select>
            <select value={actionStatus} onChange={e => setActionStatus(e.target.value)} className="surface border border-soft rounded-md px-2.5 py-1.5 text-[12.5px] outline-none" style={{ color: "var(--text)" }}>
              <option value="all">All statuses</option>
              <option value="running">Running</option>
              <option value="complete">Complete</option>
              <option value="approval_pending">Approval pending</option>
              <option value="approved">Approved</option>
              <option value="rejected">Rejected</option>
              <option value="error">Error</option>
            </select>
            <div className="flex-1 min-w-[220px] relative">
              <IC.Search size={13} style={{ position: "absolute", left: 10, top: 9, color: "var(--text-dim)" }}/>
              <input value={actionQuery} onChange={e => setActionQuery(e.target.value)} placeholder="Search actions, tools, or task goals"
                     className="w-full surface border border-soft rounded-md pl-8 pr-3 py-1.5 text-[12.5px] outline-none"
                     style={{ color: "var(--text)" }}/>
            </div>
            <button onClick={() => { void loadActions(); void loadTasks(); }} className="btn btn-ghost btn-sm"><IC.Refresh size={13}/> Refresh</button>
          </div>
          {actionsLoading && <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading actions…</p>}
          {!actionsLoading && actions.length === 0 && (
            <EmptyState icon={<IC.Audit size={20}/>} title="No actions found" sub="Tool calls, approvals, artifacts, sub-agents, and task outcomes appear here."/>
          )}
          {!actionsLoading && actions.length > 0 && (
            <div className="surface border border-soft rounded-lg overflow-hidden">
              {actions.map(action => (
                <ActivityActionRow
                  key={action.id}
                  action={action}
                  onTask={() => {
                    if (!action.task_id) return;
                    setMode("jobs");
                    setActiveTaskId(action.task_id);
                  }}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TaskTimeline({ task, events }: { task: Task; events: ActivityAction[] }) {
  const history = Array.isArray(task.agent_state?.agent_history) ? task.agent_state?.agent_history as unknown[] : [];
  if (!events.length) {
    return (
      <div className="rounded-lg border border-soft p-3 text-[12.5px]" style={{ color: "var(--text-muted)" }}>
        No execution events recorded yet. Agent state contains {history.length} messages and {task.iteration_count ?? 0} loop iterations.
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-soft overflow-hidden">
      <div className="px-3 py-2 flex items-center justify-between border-b hairline">
        <div className="text-[12.5px] font-medium">Execution timeline</div>
        <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{task.iteration_count ?? 0} iterations · {history.length} history messages</div>
      </div>
      <div className="divide-y" style={{ borderColor: "var(--border-soft)" }}>
        {events.map(event => <TimelineEvent key={event.id} event={event}/>)}
      </div>
    </div>
  );
}

function TimelineEvent({ event }: { event: ActivityAction }) {
  const isError = event.status === "error" || event.type === "task_failed" || event.type === "tool_error";
  const guidance = isError ? nextStepGuidance(event) : null;

  return (
    <div className="px-3 py-2.5 flex items-start gap-3">
      <Dot color={activityStatusColor(event.status, event.type)} size={7} pulse={event.status === "running"} ring={event.status === "running"}/>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[12.5px] font-medium">{event.summary}</span>
          {event.tool && <Tag>{event.tool}</Tag>}
          {event.approval_id && <Tag variant="warn">approval</Tag>}
          {event.artifact_id && <Tag variant="info">artifact</Tag>}
          {isError && <Tag variant="danger">failed</Tag>}
        </div>
        <div className="mt-1 text-[11.5px] flex items-center gap-2 flex-wrap" style={{ color: "var(--text-dim)" }}>
          <span>{labelTime(event.created_at)}</span>
          <span>{event.type}</span>
          {event.actor_id && <span>actor {event.actor_id}</span>}
          {event.error && <div className="mt-1.5 w-full text-[12.5px] rounded-md px-3 py-2" style={{ color: "var(--danger)", background: "var(--danger-soft)" }}>{event.error}</div>}
        </div>
        {guidance && (
          <div className="mt-2 text-[12px] flex items-center gap-2 flex-wrap" style={{ color: "var(--text-muted)" }}>
            {guidance.map((g, i) => (
              <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full surface border border-soft">
                {g.icon}{g.label}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function nextStepGuidance(event: ActivityAction): Array<{ icon: ReactNode; label: string }> {
  const error = (event.error ?? "").toLowerCase();
  const hints: Array<{ icon: ReactNode; label: string }> = [];
  if (error.includes("missing_tools") || error.includes("not configured") || error.includes("not installed")) {
    hints.push({ icon: <IC.Connectors size={11}/>, label: "Check Settings → Connectors to enable required integrations" });
  }
  if (error.includes("timeout") || error.includes("timed out")) {
    hints.push({ icon: <IC.Refresh size={11}/>, label: "Tool timed out — Chronos may retry automatically" });
  }
  if (error.includes("rate_limit") || error.includes("rate limit") || error.includes("429")) {
    hints.push({ icon: <IC.Clock size={11}/>, label: "Rate limited — waiting before retry" });
  }
  if (error.includes("auth") || error.includes("unauthorized") || error.includes("401") || error.includes("403")) {
    hints.push({ icon: <IC.Lock size={11}/>, label: "Authentication issue — reconnect in Settings → Connectors" });
  }
  return hints;
}

function formatTaskError(error?: string): string {
  if (!error) return "The task stopped before it finished.";
  if (error === "task_timeout") {
    return "The task exceeded the runtime limit before it finished. Try again, or narrow the task if it still takes too long.";
  }
  return error;
}

function ActivityActionRow({ action, onTask }: { action: ActivityAction; onTask: () => void }) {
  async function openArtifact() {
    if (!action.artifact_id) return;
    const tab = window.open("about:blank", "_blank", "noopener,noreferrer");
    try {
      const res = await apiFetch(`/artifacts/${action.artifact_id}/content`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      if (tab) tab.location.href = url;
      else window.open(url, "_blank", "noopener,noreferrer");
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch {
      tab?.close();
    }
  }

  return (
    <div className="px-4 py-3 border-b hairline last:border-b-0 flex items-start gap-3">
      <Dot color={activityStatusColor(action.status, action.type)} size={8} pulse={action.status === "running"} ring={action.status === "running"}/>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-[13.5px] font-medium">{action.summary}</span>
          <Tag>{action.type}</Tag>
          {action.tool && <Tag variant="info">{action.tool}</Tag>}
          <Tag variant={action.status === "error" ? "danger" : action.status === "approval_pending" ? "warn" : action.status === "complete" ? "ok" : "default"}>{action.status}</Tag>
        </div>
        <div className="mt-1 text-[12px] truncate" style={{ color: "var(--text-dim)" }}>
          {action.task_goal || "No task goal"} {action.actor_id ? `· ${action.actor_id}` : ""} {action.created_at ? `· ${labelTime(action.created_at)}` : ""}
        </div>
        <div className="mt-2 flex items-center gap-2 flex-wrap">
          {action.task_id && <button onClick={onTask} className="btn btn-ghost btn-sm"><IC.Briefcase size={13}/> Task</button>}
          {action.approval_id && <button onClick={action.task_id ? onTask : () => { window.location.href = "/chat"; }} className="btn btn-ghost btn-sm"><IC.Approvals size={13}/> Open in chat</button>}
          {action.artifact_id && <button onClick={() => void openArtifact()} className="btn btn-ghost btn-sm"><IC.Folder size={13}/> {action.artifact_title || "Artifact"}</button>}
        </div>
      </div>
    </div>
  );
}

function activityStatusColor(status: string, type: string) {
  if (status === "error" || type === "task_failed" || type === "tool_error") return "var(--danger)";
  if (status === "approval_pending" || type === "awaiting_approval") return "var(--warn)";
  if (status === "complete" || type === "task_complete" || type === "artifact") return "var(--ok)";
  return "var(--accent)";
}

function labelTime(value?: string | null) {
  if (!value) return "time unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function TaskResult({ task }: { task: Task }) {
  const result = task.result || {};
  const findings = Array.isArray(result.findings) ? result.findings as Array<Record<string, unknown>> : [];
  if (findings.length) {
    return <div className="space-y-2">{findings.map((finding, index) => <div key={index} className="text-[12.5px]"><div className="font-medium">{String(finding.title || "Finding")}</div><div style={{ color: "var(--text-dim)" }}>{String(finding.summary || "")}</div></div>)}</div>;
  }
  if (Object.keys(result).length) {
    return <pre className="text-[11.5px] max-h-56 overflow-auto rounded-md p-3" style={{ background: "var(--surface-2)", color: "var(--text-muted)" }}>{JSON.stringify(result, null, 2)}</pre>;
  }
  return <div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>No result has been recorded yet.</div>;
}

// ─── Approvals Screen ─────────────────────────────────────────────────────────
function ApprovalsScreen({ onDecision }: { onDecision: () => void }) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Record<string, "approved" | "rejected">>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");

  async function loadApprovals(preferredId?: string | null) {
    setLoading(true);
    setError("");
    try {
      const data = (await (await apiFetch("/approvals/")).json()) as Approval[];
      setApprovals(data);
      setActiveId(preferredId && data.some(item => item.id === preferredId) ? preferredId : data[0]?.id ?? null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to load approvals");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadApprovals();
  }, []);

  const active = approvals.find(a => a.id === activeId);

  async function decide(id: string, decision: "approved" | "rejected", batch = false) {
    const busyKey = batch ? `batch-${decision}` : id;
    setBusyId(busyKey);
    setError("");
    try {
      await apiFetch(`/approvals/${id}/decide`, { method: "POST", body: JSON.stringify({ decision, batch }) });
      if (!batch) setDecisions(prev => ({ ...prev, [id]: decision }));
      const nextPending = batch ? null : approvals.find(a => a.id !== id && !decisions[a.id] && a.status === "pending");
      await loadApprovals(nextPending?.id ?? null);
      onDecision();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to update approval");
    } finally {
      setBusyId(null);
    }
  }

  const pending = approvals.filter(a => !decisions[a.id] && a.status === "pending");
  const decided = approvals.filter(a => decisions[a.id] || a.status !== "pending");

  return (
    <div className="flex-1 flex min-w-0 overflow-hidden">
      {/* Inbox list */}
      <div className="flex-shrink-0 flex flex-col border-r hairline" style={{ width: 380, background: "var(--bg)" }}>
        <div className="px-5 pt-5 pb-3 flex-shrink-0">
          <h1 className="h-section">Approvals</h1>
          <p className="text-[12.5px] mt-0.5" style={{ color: "var(--text-dim)" }}>
            {pending.length} waiting · approved drafts are created after the batch is decided
          </p>
          {pending.length > 0 && (
            <div className="flex items-center gap-2 mt-4">
              <button
                onClick={() => void decide(active?.id ?? pending[0].id, "approved", true)}
                disabled={!!busyId}
                className="btn btn-ok-soft btn-sm flex-1 justify-center disabled:opacity-50"
              >
                <IC.Check size={14} stroke={2.2}/> {busyId === "batch-approved" ? "Approving..." : "Approve all"}
              </button>
              <button
                onClick={() => void decide(active?.id ?? pending[0].id, "rejected", true)}
                disabled={!!busyId}
                className="btn btn-danger-soft btn-sm flex-1 justify-center disabled:opacity-50"
              >
                <IC.X size={14}/> {busyId === "batch-rejected" ? "Rejecting..." : "Reject all"}
              </button>
            </div>
          )}
          {error && (
            <p className="mt-3 rounded-lg border px-3 py-2 text-[12.5px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>
              {error}
            </p>
          )}
        </div>
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="px-5 py-4 space-y-3">
              {[1,2,3].map(i => (
                <div key={i} className="flex items-center gap-3 px-3 py-3 rounded-lg" style={{ background: "var(--surface)" }}>
                  <div className="w-2 h-2 rounded-full" style={{ background: "var(--border-soft)" }}/>
                  <div className="flex-1">
                    <div className="h-3 rounded w-3/4" style={{ background: "var(--border-soft)" }}/>
                    <div className="h-2.5 rounded w-1/2 mt-1.5" style={{ background: "var(--border-soft)" }}/>
                  </div>
                </div>
              ))}
            </div>
          )}
          {!loading && approvals.length === 0 && (
            <div className="px-5 py-8">
              <EmptyState icon={<IC.Approvals size={20}/>} title="All caught up" sub="When Chronos needs your go-ahead on a draft or action, it shows up here."/>
            </div>
          )}
          {approvals.map(a => {
            const d = decisions[a.id];
            const isSelected = a.id === activeId;
            const isPending = !d && a.status === "pending";
            return (
              <button key={a.id} onClick={() => setActiveId(a.id)}
                      className={`email-row ${isSelected ? "selected" : ""}`}>
                <div className="flex-shrink-0">
                  {d === "approved" && <IC.Check size={16} stroke={2} style={{ color: "var(--ok)" }}/>}
                  {d === "rejected" && <IC.X size={16} style={{ color: "var(--danger)" }}/>}
                  {isPending && <div className="w-2 h-2 rounded-full" style={{ background: "var(--accent)" }}/>}
                  {!isPending && !d && <div className="w-2 h-2 rounded-full" style={{ background: "var(--text-faint)" }}/>}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-[13.5px] font-medium truncate" style={{ color: "var(--text)" }}>
                    {String(a.action_payload?.subject ?? a.action_type)}
                  </div>
                  <div className="text-[12px] mt-0.5" style={{ color: "var(--text-dim)" }}>
                    {a.action_type} · {a.requested_at ? new Date(a.requested_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—"}
                  </div>
                </div>
                {d && <Tag variant={d === "approved" ? "ok" : "danger"}>{d === "approved" ? "Approved" : "Rejected"}</Tag>}
              </button>
            );
          })}
        </div>
      </div>

      {/* Detail pane */}
      <div className="flex-1 flex flex-col min-w-0 overflow-y-auto" style={{ background: "var(--bg)" }}>
        {!active ? (
          <div className="flex-1 flex items-center justify-center">
            <EmptyState icon={<IC.Approvals size={24}/>} title="Select an approval" sub="Click one on the left to review it."/>
          </div>
        ) : (
          <div className="px-8 py-8 max-w-[680px]">
            <div className="mb-6">
              <div className="flex items-center gap-2 mb-1">
                <Tag variant="warn">{active.action_type}</Tag>
                {active.requested_at && (
                  <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>
                    Requested {new Date(active.requested_at).toLocaleString()}
                  </span>
                )}
              </div>
              <h2 className="h-section mt-2">
                {String(active.action_payload?.subject ?? active.action_type)}
              </h2>
              <p className="text-[13.5px] mt-2 leading-relaxed" style={{ color: "var(--text-muted)" }}>
                Step ID: <span className="inline-ref">{active.step_id}</span>
              </p>
            </div>

            {/* Payload */}
            <div className="surface border border-soft rounded-xl p-5 mb-6">
              <div className="text-[12px] font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-dim)" }}>Action details</div>
              <pre className="text-[12.5px] font-mono leading-relaxed overflow-x-auto" style={{ color: "var(--text-muted)", whiteSpace: "pre-wrap" }}>
                {JSON.stringify(active.action_payload, null, 2)}
              </pre>
            </div>

            {/* Decisions */}
            {!decisions[active.id] && active.status === "pending" ? (
              <div className="flex items-center gap-3">
                <button onClick={() => void decide(active.id, "approved")} disabled={busyId === active.id} className="btn btn-ok-soft flex-1 justify-center disabled:opacity-50">
                  <IC.Check size={15} stroke={2.2}/> {busyId === active.id ? "Approving..." : "Approve draft"}
                </button>
                <button onClick={() => void decide(active.id, "rejected")} disabled={busyId === active.id} className="btn btn-danger-soft flex-1 justify-center disabled:opacity-50">
                  <IC.X size={15}/> {busyId === active.id ? "Rejecting..." : "Reject draft"}
                </button>
              </div>
            ) : (
              <Tag variant={decisions[active.id] === "approved" || active.status === "approved" ? "ok" : "danger"}>
                {decisions[active.id] ?? active.status}
              </Tag>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Memory Screen ────────────────────────────────────────────────────────────
const MEMORY_SCOPES = [
  { id: "org", label: "Organization", description: "Shared across the entire organization" },
  { id: "workspace", label: "Workspace", description: "Shared within your workspace" },
  { id: "personal", label: "Personal", description: "Private to you" },
];

function MemoryScreen() {
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [newContent, setNewContent] = useState("");
  const [newScope, setNewScope] = useState("org");
  const [toast, setToast] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);

  const loadMemories = useCallback(() => {
    setLoading(true);
    apiFetch("/memory/")
      .then(r => r.json())
      .then((data: MemoryEntry[]) => setMemories(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadMemories();
  }, [loadMemories]);

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  const scopes = Array.from(new Set(memories.map(m => m.scope))).filter(Boolean);
  const [scopeFilter, setScopeFilter] = useState("all");

  const filtered = memories.filter(m => {
    if (filter === "auto" && m.source !== "autonomous") return false;
    if (filter === "you" && m.source === "autonomous") return false;
    if (scopeFilter !== "all" && m.scope !== scopeFilter) return false;
    if (search && !m.content.toLowerCase().includes(search.toLowerCase())) return false;
    return true;
  });

  async function addMemory() {
    if (!newContent.trim()) return;
    try {
      await apiFetch("/memory/", {
        method: "POST",
        body: JSON.stringify({ content: newContent, scope: newScope, scope_id: "default", source: "manual" }),
      });
      await loadMemories();
      setAdding(false);
      setNewContent("");
      setToast({ kind: "ok", text: `Memory saved as "${newScope}" scope.` });
    } catch {
      setToast({ kind: "danger", text: "Failed to save memory." });
    }
  }

  async function updateMemory(id: string, content: string, importance_score?: number) {
    const res = await apiFetch(`/memory/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ content, importance_score }),
    });
    if (!res.ok) throw new Error("Unable to update memory");
    setMemories(prev => prev.map(memory => memory.id === id ? { ...memory, content, importance_score } : memory));
  }

  async function deleteMemory(id: string) {
    try {
      await apiFetch(`/memory/${id}`, { method: "DELETE" });
      setMemories(prev => prev.filter(m => m.id !== id));
    } catch { /* silently */ }
  }

  const [conflicts, setConflicts] = useState<MemoryConflict[]>([]);
  const [showConflicts, setShowConflicts] = useState(false);

  async function flagMemory(id: string, kind: "archive" | "pin" | "sensitive", value: boolean) {
    try {
      await apiFetch(`/memory/${id}/${kind}`, { method: "POST", body: JSON.stringify({ value }) });
      if (kind === "archive" && value) {
        setMemories(prev => prev.filter(m => m.id !== id));
      } else {
        const field = kind === "pin" ? "is_pinned" : "is_sensitive";
        setMemories(prev => prev.map(m => m.id === id ? { ...m, [field]: value } : m));
      }
    } catch {
      setToast({ kind: "danger", text: `Could not ${kind} memory.` });
    }
  }

  async function reviewConflicts() {
    try {
      const data = await apiFetch("/memory/conflicts").then(r => r.json()) as MemoryConflict[];
      setConflicts(data);
      setShowConflicts(true);
      if (data.length === 0) setToast({ kind: "ok", text: "No conflicting memories found." });
    } catch {
      setToast({ kind: "danger", text: "Could not load conflicts." });
    }
  }

  async function resolveConflict(c: MemoryConflict) {
    try {
      await apiFetch("/memory/resolve-conflict", {
        method: "POST",
        body: JSON.stringify({ stale_id: c.stale_id, survivor_id: c.survivor_id }),
      });
      setConflicts(prev => prev.filter(x => x.stale_id !== c.stale_id));
      setMemories(prev => prev.filter(m => m.id !== c.stale_id));
      setToast({ kind: "ok", text: "Older memory superseded." });
    } catch {
      setToast({ kind: "danger", text: "Could not resolve conflict." });
    }
  }

  async function exportMemories() {
    try {
      const data = await apiFetch("/memory/export").then(r => r.json());
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "chronos-memory-export.json"; a.click();
      URL.revokeObjectURL(url);
      setToast({ kind: "ok", text: `Exported ${data.length} memories.` });
    } catch {
      setToast({ kind: "danger", text: "Export failed." });
    }
  }

  async function importMemories(file: File) {
    try {
      const items = JSON.parse(await file.text());
      const res = await apiFetch("/memory/import", { method: "POST", body: JSON.stringify({ items }) }).then(r => r.json());
      await loadMemories();
      setToast({ kind: "ok", text: `Imported ${res.imported} memories.` });
    } catch {
      setToast({ kind: "danger", text: "Import failed — expected a JSON array." });
    }
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <PageHeader
        title="Memory"
        subtitle="What Chronos remembers about you and your workspace. Save anything important."
        right={
          <div className="flex items-center gap-2">
            <button onClick={() => void reviewConflicts()} className="btn btn-ghost btn-sm" title="Find stale or conflicting memories">
              <IC.Info size={14}/> Review conflicts
            </button>
            <button onClick={() => void exportMemories()} className="btn btn-ghost btn-sm" title="Export memories as JSON">Export</button>
            <label className="btn btn-ghost btn-sm cursor-pointer" title="Import memories from JSON">
              Import
              <input type="file" accept="application/json" className="hidden"
                     onChange={e => { const f = e.target.files?.[0]; if (f) void importMemories(f); e.target.value = ""; }}/>
            </label>
            <button onClick={() => setAdding(true)} className="btn btn-secondary btn-sm">
              <IC.Plus size={14}/> Add a memory
            </button>
          </div>
        }
      />

      <div className="px-10 pb-4 flex items-center gap-3 flex-wrap">
        <div className="flex items-center gap-1 surface border border-soft rounded-lg p-1">
          {[{ id: "all", label: "All" }, { id: "auto", label: "Saved by Chronos" }, { id: "you", label: "Saved by you" }].map(f => (
            <button key={f.id} onClick={() => setFilter(f.id)}
                    className="px-3 py-1 rounded-md text-[13px] font-medium smooth"
                    style={{ background: filter === f.id ? "var(--surface-2)" : "transparent", color: filter === f.id ? "var(--text)" : "var(--text-muted)" }}>
              {f.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-1.5 px-3 py-1.5 surface border border-soft rounded-lg">
          <IC.Search size={14} style={{ color: "var(--text-dim)" }}/>
          <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search memories…"
                 className="bg-transparent text-[13.5px] outline-none w-48" style={{ color: "var(--text)" }}/>
        </div>

        <span className="text-[13px] ml-auto" style={{ color: "var(--text-dim)" }}>
          {filtered.length} {filtered.length === 1 ? "memory" : "memories"}
        </span>
      </div>

      {scopes.length > 0 && (
        <div className="px-10 pb-3 flex items-center gap-1.5 flex-wrap">
          {["all", ...scopes].map(s => (
            <button key={s} onClick={() => setScopeFilter(s)}
                    className="px-2.5 py-1 rounded-full text-[12.5px] font-medium smooth"
                    style={{ background: scopeFilter === s ? "var(--text)" : "transparent", color: scopeFilter === s ? "var(--bg)" : "var(--text-muted)", border: "1px solid", borderColor: scopeFilter === s ? "var(--text)" : "var(--border-soft)" }}>
              {s === "all" ? "All scopes" : s}
            </button>
          ))}
        </div>
      )}

      <div className="px-10 pb-10 space-y-3">
        {toast && (
          <div className="flex items-center gap-2 px-4 py-2.5 rounded-xl text-[13px] fadeup"
               style={{ background: toast.kind === "ok" ? "var(--ok-soft)" : "var(--danger-soft)", color: toast.kind === "ok" ? "var(--ok)" : "var(--danger)" }}>
            {toast.kind === "ok" ? <IC.Check size={15} stroke={2.2}/> : <IC.Info size={15}/>}
            {toast.text}
          </div>
        )}

        {adding && (
          <div className="mem-card p-4 fadeup" style={{ borderColor: "var(--accent)" }}>
            <textarea value={newContent} onChange={e => setNewContent(e.target.value)} autoFocus
                      placeholder="Something Chronos should remember…" rows={2}
                      className="w-full bg-transparent outline-none text-[14.5px] resize-none"
                      style={{ color: "var(--text)" }}/>
            <div className="flex items-center justify-between mt-2">
              <div className="flex items-center gap-2">
                <label className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>Scope:</label>
                <select value={newScope} onChange={e => setNewScope(e.target.value)}
                        className="text-[12.5px] surface border border-soft rounded-md px-2 py-1 outline-none"
                        style={{ color: "var(--text)" }}>
                  {MEMORY_SCOPES.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
                </select>
                {newScope !== "org" && (
                  <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>
                    {MEMORY_SCOPES.find(s => s.id === newScope)?.description}
                  </span>
                )}
                {newScope === "org" && (
                  <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>
                    Default — visible to everyone in the organization
                  </span>
                )}
              </div>
              <div className="flex items-center gap-2">
                <button onClick={() => { setAdding(false); setNewContent(""); }} className="btn btn-ghost btn-sm">Cancel</button>
                <button onClick={() => void addMemory()} className="btn btn-accent btn-sm">Save memory</button>
              </div>
            </div>
          </div>
        )}

        {loading && (
          <div className="space-y-3">
            {[1,2,3].map(i => (
              <div key={i} className="mem-card p-4">
                <div className="h-4 rounded w-3/4" style={{ background: "var(--border-soft)" }}/>
                <div className="h-3 rounded w-1/3 mt-2" style={{ background: "var(--border-soft)" }}/>
              </div>
            ))}
          </div>
        )}
        {!loading && filtered.length === 0 && (
          <EmptyState icon={<IC.Memory size={20}/>} title="No memories yet"
                      sub={memories.length === 0 ? "Chronos saves memories automatically during conversations. You can also add them manually." : "No memories match your current filter."}/>
        )}

        {showConflicts && conflicts.length > 0 && (
          <div className="mem-card p-4 fadeup" style={{ borderColor: "var(--accent)" }}>
            <div className="flex items-center justify-between mb-2">
              <span className="text-[13.5px] font-semibold" style={{ color: "var(--text)" }}>
                {conflicts.length} potential conflict{conflicts.length === 1 ? "" : "s"}
              </span>
              <button onClick={() => setShowConflicts(false)} className="btn btn-ghost btn-sm">Dismiss</button>
            </div>
            <div className="space-y-2">
              {conflicts.map(c => (
                <div key={c.stale_id} className="flex items-start gap-3 p-2.5 rounded-lg" style={{ background: "var(--surface-2)" }}>
                  <div className="flex-1 min-w-0 text-[13px]" style={{ color: "var(--text)" }}>
                    <div style={{ color: "var(--text-dim)" }}>Keep: {c.survivor_content}</div>
                    <div className="line-through" style={{ color: "var(--text-dim)" }}>Stale: {c.stale_content}</div>
                    <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{Math.round(c.similarity * 100)}% similar · {c.scope}</span>
                  </div>
                  <button onClick={() => void resolveConflict(c)} className="btn btn-accent btn-sm flex-shrink-0">Supersede</button>
                </div>
              ))}
            </div>
          </div>
        )}

        {filtered.map(m => <MemoryCard key={m.id} m={m} onDelete={deleteMemory} onUpdate={updateMemory} onFlag={flagMemory}/>)}
      </div>
    </div>
  );
}

function MemoryCard({ m, onDelete, onUpdate, onFlag }: { m: MemoryEntry; onDelete: (id: string) => void; onUpdate: (id: string, content: string, importance_score?: number) => Promise<void>; onFlag: (id: string, kind: "archive" | "pin" | "sensitive", value: boolean) => Promise<void> }) {
  const [hover, setHover] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(m.content);
  const [saving, setSaving] = useState(false);
  const isPrivate = m.scope === "restricted";
  const isAuto = m.source === "autonomous";

  async function save() {
    const next = draft.trim();
    if (!next) return;
    setSaving(true);
    try {
      await onUpdate(m.id, next, m.importance_score);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mem-card p-4" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 mt-0.5"
             style={{ background: isPrivate ? "var(--danger-soft)" : "var(--surface-2)", color: isPrivate ? "var(--danger)" : "var(--text-dim)" }}>
          {isPrivate ? <IC.Lock size={13}/> : <IC.Memory size={13}/>}
        </div>
        <div className="flex-1 min-w-0">
          {editing ? (
            <div>
              <textarea value={draft} onChange={e => setDraft(e.target.value)} rows={3} autoFocus
                        className="w-full bg-transparent outline-none text-[14.5px] leading-relaxed resize-none border border-soft rounded-md p-2"
                        style={{ color: "var(--text)" }}/>
              <div className="mt-2 flex items-center gap-2">
                <button onClick={() => { setDraft(m.content); setEditing(false); }} className="btn btn-ghost btn-sm">Cancel</button>
                <button onClick={() => void save()} disabled={saving || !draft.trim()} className="btn btn-accent btn-sm">{saving ? "Saving..." : "Save"}</button>
              </div>
            </div>
          ) : (
            <div className="text-[14.5px] leading-relaxed" style={{ color: "var(--text)" }}>{m.content}</div>
          )}
          <div className="flex items-center gap-2.5 mt-2 text-[12px]" style={{ color: "var(--text-dim)" }}>
            <span className="inline-flex items-center gap-1">
              {isAuto ? <><IC.Sparkles size={11}/> Saved by Chronos</> : <><IC.Pencil size={11}/> Saved by you</>}
            </span>
            {m.scope && <><span>·</span><span>{m.scope}</span></>}
            {typeof m.importance_score === "number" && <><span>·</span><span>{Math.round(m.importance_score * 100)}% importance</span></>}
            {m.created_by && <><span>·</span><span>by {m.created_by}</span></>}
            {m.is_pinned && <><span>·</span><span style={{ color: "var(--accent)" }}>Pinned</span></>}
            {m.is_sensitive && <><span>·</span><span style={{ color: "var(--danger)" }}>Sensitive</span></>}
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0" style={{ opacity: hover ? 1 : 0, transition: "opacity 0.15s" }}>
          <button onClick={() => void onFlag(m.id, "pin", !m.is_pinned)} className="btn btn-ghost btn-sm btn-icon" title={m.is_pinned ? "Unpin" : "Pin"}>
            <IC.ArrowUp size={13}/>
          </button>
          <button onClick={() => void onFlag(m.id, "sensitive", !m.is_sensitive)} className="btn btn-ghost btn-sm btn-icon" title={m.is_sensitive ? "Unmark sensitive" : "Mark sensitive"}>
            <IC.Lock size={13}/>
          </button>
          <button onClick={() => setEditing(true)} className="btn btn-ghost btn-sm btn-icon" title="Edit">
            <IC.Pencil size={13}/>
          </button>
          <button onClick={() => void onFlag(m.id, "archive", true)} className="btn btn-ghost btn-sm btn-icon" title="Archive">
            <IC.Folder size={13}/>
          </button>
          <button onClick={() => onDelete(m.id)} className="btn btn-ghost btn-sm btn-icon" title="Delete">
            <IC.Trash size={13}/>
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Connectors Screen ────────────────────────────────────────────────────────
// Agent tool health reported by core/connector_health.py
// ─── Integrations Marketplace ─────────────────────────────────────────────────

type CatalogApp = {
  id: string;
  name: string;
  description: string;
  icon_svg: string;
  category?: string;
  auth_type?: string;
  scopes?: string[];
  actions?: string[];
  risk_levels?: string[];
  sync_supported?: boolean;
  policy?: string;
  health_status?: string;
  health_updated_at?: string | null;
  last_used_at?: string;
  client_id_env?: string;
  client_secret_env?: string;
  configured: boolean;
  connected: boolean;
  account_handle: string;
};

function AppIcon({ svg, name }: { svg: string; name: string }) {
  return (
    <div
      className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0 border border-soft"
      style={{ background: "var(--surface-2)" }}
      dangerouslySetInnerHTML={{ __html: svg }}
      title={name}
    />
  );
}

function ConnectorsScreen() {
  const [apps, setApps] = useState<CatalogApp[]>([]);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function load() {
    setLoading(true);
    try {
      const data = await apiFetch("/connectors/catalog").then(r => r.json()) as CatalogApp[];
      setApps(data);
    } catch {
      setError("Could not load integrations.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const callbackError = params.get("connector_error") || params.get("error");
    const callbackSuccess = params.get("connector_success");
    if (callbackError) {
      setError(callbackError);
    } else if (callbackSuccess) {
      setNotice(`${callbackSuccess.replaceAll("_", " ")} connected.`);
    }
    if (callbackError || callbackSuccess) {
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  async function connect(app: CatalogApp) {
    if (app.auth_type && app.auth_type !== "oauth2") {
      setError(`${app.name} uses ${app.auth_type} setup. Configure it from connector policy/schema settings before connecting live credentials.`);
      return;
    }
    if (!app.configured) {
      const idEnv = app.client_id_env || `${app.id.toUpperCase()}_CLIENT_ID`;
      const secretEnv = app.client_secret_env || `${app.id.toUpperCase()}_CLIENT_SECRET`;
      setError(`Set ${idEnv} and ${secretEnv} in your .env to enable ${app.name}.`);
      return;
    }
    setConnecting(app.id);
    setError("");
    setNotice("");
    try {
      const endpoint = app.id === "gmail"
        ? "/connectors/gmail/oauth-start"
        : `/connectors/${app.id}/oauth-start`;
      const res = await apiFetch(endpoint, { method: "POST" });
      if (!res.ok) {
        const body = await res.json().catch(() => ({})) as { detail?: string };
        throw new Error(body.detail ?? `Error ${res.status}`);
      }
      const { url } = await res.json() as { url: string };
      window.location.href = url;
    } catch (err) {
      setError(err instanceof Error ? err.message : `${app.name} connect failed`);
      setConnecting(null);
    }
  }

  async function disconnect(app: CatalogApp) {
    if (!confirm(`Disconnect ${app.name}? Chronos will no longer be able to access it.`)) return;
    setDisconnecting(app.id);
    setError("");
    setNotice("");
    try {
      const res = await apiFetch(`/connectors/${app.id}/disconnect`, { method: "DELETE" });
      if (!res.ok) throw new Error(`Error ${res.status}`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Disconnect failed");
    } finally {
      setDisconnecting(null);
    }
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <PageHeader
        title="Integrations"
        subtitle="Connect apps so Chronos can act on your behalf. Each connection is OAuth2 — you control access."
      />

      <div className="px-10 pb-10">
        {notice && (
          <div className="mb-5 rounded-xl border border-soft px-4 py-3 text-[13px]" style={{ color: "var(--ok)", background: "var(--surface-2)" }}>
            {notice}
            <button className="ml-3 underline" onClick={() => setNotice("")}>Dismiss</button>
          </div>
        )}

        {error && (
          <div className="mb-5 rounded-xl border border-soft px-4 py-3 text-[13px]" style={{ color: "var(--danger)", background: "var(--surface-2)" }}>
            {error}
            <button className="ml-3 underline" onClick={() => setError("")}>Dismiss</button>
          </div>
        )}

        {loading && (
          <div className="grid grid-cols-3 gap-4">
            {[1,2,3,4,5,6].map(i => (
              <div key={i} className="surface border border-soft rounded-2xl p-5 animate-pulse h-28" />
            ))}
          </div>
        )}

        {!loading && apps.length === 0 && (
          <EmptyState icon={<IC.Connectors size={20}/>}
            title="No integrations available"
            sub="Configure OAuth credentials in your .env to enable integrations like Gmail, Slack, GitHub, and Notion."/>
        )}

        {!loading && apps.length > 0 && (
          <div className="grid grid-cols-3 gap-4">
            {apps.map(app => (
              <div
                key={app.id}
                className="surface border border-soft rounded-2xl p-5 flex flex-col gap-3 transition-shadow hover:shadow-sm"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-3 min-w-0">
                    <AppIcon svg={app.icon_svg} name={app.name} />
                    <div className="min-w-0">
                      <div className="font-semibold text-[14.5px] leading-5">{app.name}</div>
                      {app.connected && app.account_handle && (
                        <div className="text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>
                          {app.account_handle}
                        </div>
                      )}
                    </div>
                  </div>
                  {app.connected && (
                    <span className="shrink-0 inline-flex items-center gap-1 text-[11.5px] font-medium px-2 py-0.5 rounded-full" style={{ background: "color-mix(in srgb, var(--accent) 12%, transparent)", color: "var(--accent)" }}>
                      <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor"><circle cx="4" cy="4" r="4"/></svg>
                      Connected
                    </span>
                  )}
                </div>

                <p className="text-[12.5px] leading-[1.55]" style={{ color: "var(--text-muted)" }}>
                  {app.description}
                </p>

                <div className="flex flex-wrap gap-1">
                  {app.auth_type && <Tag>{app.auth_type}</Tag>}
                  {app.sync_supported && <Tag variant="accent">Sync</Tag>}
                  {app.health_status && <Tag variant={app.health_status === "healthy" || app.health_status === "connected" ? "ok" : app.health_status === "not_connected" ? "info" : "warn"}>{app.health_status}</Tag>}
                  {(app.risk_levels || []).slice(0, 3).map(level => (
                    <Tag key={level} variant={level === "read" ? "info" : level === "financial" || level === "destructive" ? "danger" : "warn"}>{level}</Tag>
                  ))}
                </div>

                {(app.actions || []).length > 0 && (
                  <div className="text-[11.5px] leading-5" style={{ color: "var(--text-dim)" }}>
                    Actions: {(app.actions || []).join(", ")}
                  </div>
                )}

                {(app.scopes || []).length > 0 && (
                  <div className="text-[11.5px] leading-5 line-clamp-2" style={{ color: "var(--text-dim)" }}>
                    Scopes: {(app.scopes || []).slice(0, 4).join(", ")}{(app.scopes || []).length > 4 ? "…" : ""}
                  </div>
                )}

                <div className="text-[11.5px] leading-5" style={{ color: "var(--text-dim)" }}>
                  Last used: {app.last_used_at || "never"}
                </div>

                {app.policy && (
                  <p className="text-[11.5px] leading-[1.5]" style={{ color: "var(--text-muted)" }}>
                    {app.policy}
                  </p>
                )}

                <div className="mt-auto pt-1 flex gap-2">
                  {!app.connected ? (
                    <button
                      onClick={() => void connect(app)}
                      disabled={connecting === app.id}
                      className="btn btn-accent btn-sm w-full disabled:opacity-50"
                      title={!app.configured ? `Add ${app.client_id_env || `${app.id.toUpperCase()}_CLIENT_ID`} + ${app.client_secret_env || "CLIENT_SECRET"} to .env first` : undefined}
                    >
                      {connecting === app.id ? "Redirecting…" : app.auth_type !== "oauth2" ? "Configure" : app.configured ? "Connect" : "Configure first"}
                    </button>
                  ) : (
                    <>
                      <button
                        onClick={() => void connect(app)}
                        disabled={connecting === app.id}
                        className="btn btn-secondary btn-sm flex-1 disabled:opacity-50"
                      >
                        {connecting === app.id ? "…" : "Reconnect"}
                      </button>
                      <button
                        onClick={() => void disconnect(app)}
                        disabled={disconnecting === app.id}
                        className="btn btn-danger-soft btn-sm flex-1 disabled:opacity-50"
                      >
                        {disconnecting === app.id ? "…" : "Disconnect"}
                      </button>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {apps.length > 0 && (
          <p className="mt-8 text-[12px]" style={{ color: "var(--text-dim)" }}>
            More integrations coming soon. Each connection uses OAuth2 — Chronos never sees your password.
          </p>
        )}
      </div>
    </div>
  );
}

// ─── Assistants Screen ────────────────────────────────────────────────────────
function AssistantsScreen({ onStartConversation }: { onStartConversation: (personaId: string) => void }) {
  const [activePersonaId, setActivePersonaId] = useState<string | null>(null);
  const activePersona = PERSONAS.find(p => p.id === activePersonaId);

  if (activePersona) {
    return (
      <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
        <div className="px-10 pt-6 pb-2 flex-shrink-0">
          <button onClick={() => setActivePersonaId(null)} className="btn btn-ghost btn-sm -ml-2 mb-3">
            <IC.Chevron size={14} style={{ transform: "rotate(180deg)" }}/> Assistants
          </button>
        </div>
        <div className="px-10 pb-6 flex items-center gap-5 flex-shrink-0">
          <PersonaAvatar name={activePersona.name} color={activePersona.color} size={64}/>
          <div>
            <h1 className="h-page">{activePersona.name}</h1>
            <p className="text-[14px]" style={{ color: "var(--text-dim)" }}>{activePersona.role}</p>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button className="btn btn-secondary btn-sm">Edit</button>
            <button onClick={() => onStartConversation(activePersona.id)} className="btn btn-accent btn-sm">Start a conversation</button>
          </div>
        </div>
        <div className="px-10 pb-10 max-w-[820px] space-y-7">
          <div>
            <h2 className="text-[15px] font-semibold mb-3">Personality</h2>
            <textarea defaultValue={activePersona.prompt} rows={3}
                      className="w-full surface border border-soft rounded-lg px-4 py-3 text-[14px] leading-relaxed outline-none"
                      style={{ color: "var(--text)" }}/>
          </div>
          <div>
            <h2 className="text-[15px] font-semibold mb-3">Skills</h2>
            <div className="grid grid-cols-2 gap-2.5">
              {SKILLS.map(s => {
                const on = activePersona.skills.includes(s.id);
                return (
                  <div key={s.id} className="surface border rounded-lg p-3.5 flex items-start gap-3"
                       style={{ borderColor: on ? "var(--accent)" : "var(--border-soft)", opacity: on ? 1 : 0.55 }}>
                    <div className="w-8 h-8 rounded-md flex items-center justify-center flex-shrink-0"
                         style={{ background: on ? "var(--accent-soft)" : "var(--surface-2)", color: on ? "var(--accent-text)" : "var(--text-dim)" }}>
                      <IC.Sparkles size={15}/>
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[13.5px] font-medium">{s.name}</div>
                      <div className="text-[12.5px] mt-0.5" style={{ color: "var(--text-dim)" }}>{s.description}</div>
                    </div>
                    <input type="checkbox" readOnly checked={on} className="mt-1" style={{ accentColor: "var(--accent)" }}/>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <PageHeader
        title="Assistants"
        subtitle="Saved configurations of Chronos. Each has its own role, skills, and personality."
        right={<button className="btn btn-secondary btn-sm"><IC.Plus size={14}/> New assistant</button>}
      />
      <div className="px-10 pb-10 grid grid-cols-3 gap-4">
        {PERSONAS.map(p => (
          <button key={p.id} onClick={() => setActivePersonaId(p.id)}
                  className="surface border border-soft rounded-xl p-5 text-left smooth hover:border-[var(--border)]">
            <PersonaAvatar name={p.name} color={p.color} size={48}/>
            <h3 className="text-[16px] font-semibold mt-3 mb-1">{p.name}</h3>
            <div className="text-[13px] mb-3" style={{ color: "var(--text-muted)" }}>{p.role}</div>
            <p className="text-[13px] leading-relaxed mb-4" style={{ color: "var(--text-dim)" }}>{p.prompt.substring(0, 100)}…</p>
            <div className="flex items-center gap-1.5 flex-wrap">
              {p.skills.map(s => { const sk = SKILLS.find(sk => sk.id === s); return <Tag key={s}>{sk?.name}</Tag>; })}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

// ─── Settings Screen ──────────────────────────────────────────────────────────
const SETTING_TABS: Array<{ id: SettingsTab; label: string; icon: ReactNode; keywords: string }> = [
  { id: "general", label: "General", icon: <IC.Settings size={15}/>, keywords: "workspace name timezone language theme notifications landing" },
  { id: "profile", label: "Profile", icon: <IC.Personas size={15}/>, keywords: "name avatar email role response citations interaction" },
  { id: "organization", label: "Organization", icon: <IC.Briefcase size={15}/>, keywords: "org logo domain plan seats workspace creation" },
  { id: "members", label: "Members & roles", icon: <IC.Personas size={15}/>, keywords: "member invite role remove owner admin manager operator viewer" },
  { id: "permissions", label: "Permissions", icon: <IC.Lock size={15}/>, keywords: "rbac matrix workspace employee tools approval memory audit" },
  { id: "employees", label: "AI employees", icon: <IC.Sparkles size={15}/>, keywords: "employee runtime memory scope sub agent depth" },
  { id: "runtime", label: "Runtime", icon: <IC.Activity size={15}/>, keywords: "mode isolation heartbeat restart logs queue recovery budget" },
  { id: "memory-settings", label: "Memory", icon: <IC.Memory size={15}/>, keywords: "retention review auto save sensitive export purge" },
  { id: "tools-settings", label: "Tools & integrations", icon: <IC.Connectors size={15}/>, keywords: "connectors oauth disconnect scopes approval required risk enabled" },
  { id: "approval-settings", label: "Approvals", icon: <IC.Approvals size={15}/>, keywords: "mode thresholds rules external sub agent runtime memory" },
  { id: "notifications", label: "Notifications", icon: <IC.Bell size={15}/>, keywords: "email in app runtime alerts approval task weekly digest security" },
  { id: "security", label: "Security", icon: <IC.Lock size={15}/>, keywords: "sessions password two factor api keys login history revoke" },
  { id: "billing", label: "Billing", icon: <IC.Audit size={15}/>, keywords: "plan usage seats runtime tokens storage invoices stripe" },
  { id: "audit", label: "Audit logs", icon: <IC.Audit size={15}/>, keywords: "actor action target date risk search export" },
  { id: "developer", label: "Developer", icon: <IC.Lightbulb size={15}/>, keywords: "feature flags api mode webhooks debug experimental environment model provider" },
  { id: "danger", label: "Danger zone", icon: <IC.Trash size={15}/>, keywords: "reset delete leave transfer ownership irreversible" },
];

// ─── Projects ─────────────────────────────────────────────────────────────────

type Project = {
  id: string;
  name: string;
  instructions?: string | null;
  visibility?: string | null;
  memory_policy?: string | null;
  default_tools?: unknown[];
  created_at?: string;
  created_by?: string | null;
};

type ProjectTab = "chat" | "sources" | "artifacts" | "tasks" | "research";

function ProjectsScreen() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  // null = list view; string = detail view for that project id
  const [activeProjectId, setActiveProjectId] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createInstructions, setCreateInstructions] = useState("");
  const [creating, setCreating] = useState(false);
  const [toast, setToast] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);

  // URL-based project detail: /projects?id=<uuid>
  const pathname = usePathname();
  useEffect(() => {
    if (typeof window !== "undefined") {
      const id = new URLSearchParams(window.location.search).get("id");
      setActiveProjectId(id ?? null);
    }
  }, [pathname]);

  // Keep detail view in sync with browser back/forward navigation.
  useEffect(() => {
    function onPopState() {
      if (typeof window !== "undefined") {
        const id = new URLSearchParams(window.location.search).get("id");
        setActiveProjectId(id ?? null);
      }
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = (await (await apiFetch("/projects/")).json()) as Project[];
      setProjects(data);
    } catch (e) {
      setToast({ kind: "danger", text: e instanceof Error ? e.message : "Failed to load projects" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function createProject() {
    if (!createName.trim()) return;
    setCreating(true);
    try {
      await apiFetch("/projects/", {
        method: "POST",
        body: JSON.stringify({ name: createName.trim(), instructions: createInstructions.trim() || null }),
      });
      setCreateName("");
      setCreateInstructions("");
      setShowCreate(false);
      await load();
      setToast({ kind: "ok", text: "Project created" });
    } catch (e) {
      setToast({ kind: "danger", text: e instanceof Error ? e.message : "Failed to create project" });
    } finally {
      setCreating(false);
    }
  }

  function openProject(id: string) {
    setActiveProjectId(id);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("id", id);
      window.history.pushState({}, "", url.toString());
    }
  }

  function closeProject() {
    setActiveProjectId(null);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.delete("id");
      window.history.pushState({}, "", url.toString());
    }
  }

  const activeProject = projects.find(p => p.id === activeProjectId) ?? null;

  if (activeProjectId) {
    return (
      <ProjectDetailScreen
        projectId={activeProjectId}
        project={activeProject}
        onBack={closeProject}
      />
    );
  }

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <PageHeader
        title="Projects"
        subtitle="Organize conversations, tasks, and artifacts by project"
        right={
          <button className="btn btn-primary btn-sm" onClick={() => setShowCreate(v => !v)}>
            <IC.Plus size={14} /> New project
          </button>
        }
      />
      {toast && (
        <div className={`mx-10 mb-4 px-4 py-2.5 rounded-lg text-[13.5px] font-medium ${toast.kind === "ok" ? "bg-[var(--ok-soft)] text-[var(--ok-text)]" : "bg-[var(--danger-soft)] text-[var(--danger)]"}`}>
          {toast.text}
          <button className="ml-3 opacity-60 hover:opacity-100" onClick={() => setToast(null)}>✕</button>
        </div>
      )}
      {showCreate && (
        <div className="mx-10 mb-6 surface border border-soft rounded-xl p-5 flex flex-col gap-3">
          <div className="font-medium text-[14px]">New project</div>
          <input
            className="input-field"
            placeholder="Project name"
            value={createName}
            onChange={e => setCreateName(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter" && !creating) void createProject(); }}
          />
          <textarea
            className="input-field resize-none"
            rows={3}
            placeholder="Instructions (optional) — describe the project goal, context, or constraints"
            value={createInstructions}
            onChange={e => setCreateInstructions(e.target.value)}
          />
          <div className="flex gap-2">
            <button className="btn btn-primary btn-sm" disabled={creating || !createName.trim()} onClick={() => void createProject()}>
              {creating ? "Creating…" : "Create"}
            </button>
            <button className="btn btn-sm" onClick={() => { setShowCreate(false); setCreateName(""); setCreateInstructions(""); }}>
              Cancel
            </button>
          </div>
        </div>
      )}
      <div className="flex-1 overflow-auto px-10 pb-10">
        {loading ? (
          <div className="text-[13.5px] mt-6" style={{ color: "var(--text-dim)" }}>Loading projects…</div>
        ) : projects.length === 0 ? (
          <EmptyState icon={<IC.Folder size={22} />} title="No projects yet" sub="Create a project to organize conversations, tasks, and artifacts." />
        ) : (
          <div className="grid gap-3 mt-2" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))" }}>
            {projects.map(proj => (
              <button
                key={proj.id}
                className="surface border border-soft rounded-xl p-5 text-left hover:border-[var(--accent)] transition-colors group"
                onClick={() => openProject(proj.id)}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="font-medium text-[14px] truncate">{proj.name}</div>
                  <IC.Chevron size={14} style={{ color: "var(--text-faint)", flexShrink: 0, marginTop: 2 }} />
                </div>
                {proj.instructions && (
                  <p className="text-[12.5px] mt-1.5 line-clamp-2" style={{ color: "var(--text-dim)" }}>{proj.instructions}</p>
                )}
                <div className="flex gap-3 mt-3 text-[12px]" style={{ color: "var(--text-faint)" }}>
                  <span>{proj.visibility ?? "private"}</span>
                  {proj.created_at && <span>{new Date(proj.created_at).toLocaleDateString()}</span>}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

const PROJECT_TABS: { id: ProjectTab; label: string }[] = [
  { id: "chat",     label: "Chat" },
  { id: "sources",  label: "Sources" },
  { id: "artifacts",label: "Artifacts" },
  { id: "tasks",    label: "Tasks" },
  { id: "research", label: "Research" },
];

function ProjectDetailScreen({
  projectId,
  project,
  onBack,
}: {
  projectId: string;
  project: Project | null;
  onBack: () => void;
}) {
  const [tab, setTab] = useState<ProjectTab>("chat");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactRef[]>([]);
  const [sources, setSources] = useState<ProjectSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const refreshSources = useCallback(async () => {
    const rows = (await (await apiFetch(`/projects/${projectId}/sources`)).json()) as ProjectSource[];
    setSources(rows);
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    setLoadError(false);
    Promise.all([
      apiFetch(`/projects/${projectId}/conversations`).then(r => r.json()),
      apiFetch(`/projects/${projectId}/tasks`).then(r => r.json()),
      apiFetch(`/projects/${projectId}/artifacts`).then(r => r.json()),
      apiFetch(`/projects/${projectId}/sources`).then(r => r.json()),
    ])
      .then(([convs, tsks, arts, srcs]) => {
        setConversations(convs as Conversation[]);
        setTasks(tsks as Task[]);
        setArtifacts(arts as ArtifactRef[]);
        setSources(srcs as ProjectSource[]);
      })
      .catch(() => { setLoadError(true); })
      .finally(() => setLoading(false));
  }, [projectId]);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <PageHeader
        title={project?.name ?? "Project"}
        subtitle={project?.instructions ?? undefined}
        right={
          <button className="btn btn-sm" onClick={onBack}>
            ← Back
          </button>
        }
      />
      {/* Tab bar */}
      <div role="tablist" className="flex gap-0.5 px-10 mb-6 border-b hairline">
        {PROJECT_TABS.map(t => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
            className="px-4 py-2.5 text-[13.5px] font-medium transition-colors"
            style={{
              color: tab === t.id ? "var(--text)" : "var(--text-dim)",
              borderBottom: tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
              marginBottom: -1,
            }}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="flex-1 overflow-auto px-10 pb-10">
        {loadError ? (
          <div className="text-[13.5px] mt-4" style={{ color: "var(--danger)" }}>Couldn't load project details.</div>
        ) : loading ? (
          <div className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading…</div>
        ) : tab === "chat" ? (
          conversations.length === 0 ? (
            <EmptyState icon={<IC.Chat size={22} />} title="No conversations" sub="Send a message in this project to see conversations here." />
          ) : (
            <div className="flex flex-col gap-2">
              {conversations.map(conv => (
                <div key={conv.id} className="surface border border-soft rounded-xl px-5 py-4">
                  <div className="font-medium text-[14px]">{conv.title ?? "Untitled conversation"}</div>
                  {conv.updated_at && <div className="text-[12px] mt-1" style={{ color: "var(--text-faint)" }}>{new Date(conv.updated_at).toLocaleString()}</div>}
                </div>
              ))}
            </div>
          )
        ) : tab === "sources" ? (
          <ProjectSourcesTab projectId={projectId} sources={sources} refresh={refreshSources} />
        ) : tab === "artifacts" ? (
          artifacts.length === 0 ? (
            <EmptyState icon={<IC.Folder size={22} />} title="No artifacts" sub="Artifacts created in project conversations and tasks appear here." />
          ) : (
            <div className="flex flex-col gap-2">
              {artifacts.map(art => (
                <div key={art.id} className="surface border border-soft rounded-xl px-5 py-4 flex items-center gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="font-medium text-[14px] truncate">{art.title ?? "Untitled"}</div>
                    <div className="text-[12px] mt-0.5" style={{ color: "var(--text-faint)" }}>{art.kind}</div>
                  </div>
                </div>
              ))}
            </div>
          )
        ) : tab === "tasks" ? (
          tasks.length === 0 ? (
            <EmptyState icon={<IC.Activity size={22} />} title="No tasks" sub="Tasks linked to this project appear here." />
          ) : (
            <div className="flex flex-col gap-2">
              {tasks.map(task => (
                <div key={task.id} className="surface border border-soft rounded-xl px-5 py-4">
                  <div className="font-medium text-[14px] truncate">{task.goal}</div>
                  <div className="flex gap-3 mt-1 text-[12px]" style={{ color: "var(--text-faint)" }}>
                    <span>{task.status}</span>
                    {task.created_at && <span>{new Date(task.created_at).toLocaleString()}</span>}
                  </div>
                </div>
              ))}
            </div>
          )
        ) : tab === "research" ? (
          <ProjectResearchTab tasks={tasks} artifacts={artifacts} sources={sources} />
        ) : (
          <EmptyState title="Nothing here yet" sub="This feature is coming soon." />
        )}
      </div>
    </div>
  );
}

function ProjectResearchTab({
  tasks,
  artifacts,
  sources,
}: {
  tasks: Task[];
  artifacts: ArtifactRef[];
  sources: ProjectSource[];
}) {
  const rows = [
    ...tasks.map(task => ({
      id: `task-${task.id}`,
      kind: "Task",
      title: task.goal || "Untitled task",
      detail: task.status,
      date: task.created_at,
      icon: <IC.Activity size={15} />,
      tag: task.status,
      tagVariant: task.status === "complete" ? "ok" : task.status === "failed" ? "danger" : task.status === "awaiting_approval" ? "warn" : "info",
    })),
    ...artifacts.map(artifact => ({
      id: `artifact-${artifact.id}`,
      kind: "Artifact",
      title: artifact.title || "Untitled artifact",
      detail: artifact.kind,
      date: undefined,
      icon: <IC.Folder size={15} />,
      tag: artifact.kind,
      tagVariant: "default",
    })),
    ...sources.map(source => {
      const status = source.index_status ?? source.parse_status ?? "pending";
      return {
        id: `source-${source.id}`,
        kind: "Source",
        title: source.title || source.uri || "Untitled source",
        detail: source.source_type ?? "source",
        date: source.created_at,
        icon: <IC.Search size={15} />,
        tag: status,
        tagVariant: status === "indexed" || status === "synced" ? "ok" : status === "failed" || status === "revoked" ? "danger" : "info",
      };
    }),
  ].sort((a, b) => Date.parse(b.date ?? "") - Date.parse(a.date ?? ""));

  const activeTasks = tasks.filter(task => !["complete", "failed"].includes(task.status)).length;
  const indexedSources = sources.filter(source => ["indexed", "synced"].includes(source.index_status ?? "")).length;

  if (rows.length === 0) {
    return <EmptyState icon={<IC.Search size={22} />} title="No research activity" sub="Project tasks, artifacts, and indexed sources appear here as research work accumulates." />;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
        <ProjectResearchMetric label="Tasks" value={tasks.length} detail={activeTasks ? `${activeTasks} active` : "none active"} />
        <ProjectResearchMetric label="Artifacts" value={artifacts.length} detail="project outputs" />
        <ProjectResearchMetric label="Sources" value={sources.length} detail={`${indexedSources} indexed`} />
      </div>
      <div className="surface border border-soft rounded-xl overflow-hidden">
        <div className="px-5 py-3 border-b hairline flex items-center justify-between gap-3">
          <div className="font-medium text-[14px]">Research activity</div>
          <div className="text-[12px]" style={{ color: "var(--text-faint)" }}>{rows.length} items</div>
        </div>
        {rows.map(row => (
          <div key={row.id} className="px-5 py-4 border-b hairline last:border-b-0 flex items-start gap-3">
            <div className="w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0" style={{ background: "var(--surface-2)", color: "var(--text-dim)" }}>
              {row.icon}
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 min-w-0">
                <div className="font-medium text-[14px] truncate">{row.title}</div>
                <Tag variant={row.tagVariant as "default" | "accent" | "ok" | "warn" | "danger" | "info"}>{row.tag}</Tag>
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-1 mt-1 text-[12px]" style={{ color: "var(--text-faint)" }}>
                <span>{row.kind}</span>
                <span>{row.detail}</span>
                {row.date && <span>{new Date(row.date).toLocaleString()}</span>}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ProjectResearchMetric({ label, value, detail }: { label: string; value: number; detail: string }) {
  return (
    <div className="surface border border-soft rounded-xl px-5 py-4">
      <div className="text-[12px] font-medium uppercase tracking-[0.08em]" style={{ color: "var(--text-faint)" }}>{label}</div>
      <div className="mt-1 text-[24px] font-semibold leading-tight">{value}</div>
      <div className="text-[12px] mt-0.5" style={{ color: "var(--text-dim)" }}>{detail}</div>
    </div>
  );
}

const SOURCE_STATUS_COLOR: Record<string, string> = {
  indexed: "var(--accent)",
  synced: "var(--accent)",
  pending: "var(--text-faint)",
  failed: "var(--danger)",
  revoked: "var(--danger)",
};

function ProjectSourcesTab({
  projectId,
  sources,
  refresh,
}: {
  projectId: string;
  sources: ProjectSource[];
  refresh: () => Promise<void>;
}) {
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [tool, setTool] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detail, setDetail] = useState<SourceDetail | null>(null);

  const addConnector = async () => {
    if (!title.trim() || !tool.trim()) return;
    setBusyId("add"); setError(null);
    try {
      await apiFetch(`/projects/${projectId}/sources/connector`, {
        method: "POST",
        body: JSON.stringify({ title: title.trim(), tool: tool.trim(), args: {} }),
      });
      setTitle(""); setTool(""); setAdding(false);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't add connector source");
    } finally {
      setBusyId(null);
    }
  };

  const syncSource = async (sid: string) => {
    setBusyId(sid); setError(null);
    try {
      await apiFetch(`/projects/${projectId}/sources/${sid}/sync`, { method: "POST" });
      await refresh();
      if (detail?.id === sid) await openDetail(sid);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sync failed");
    } finally {
      setBusyId(null);
    }
  };

  const openDetail = async (sid: string) => {
    setError(null);
    try {
      const d = (await (await apiFetch(`/projects/${projectId}/sources/${sid}`)).json()) as SourceDetail;
      setDetail(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Couldn't load source");
    }
  };

  const reindexSource = async (sid: string) => {
    setBusyId(sid); setError(null);
    try {
      await apiFetch(`/projects/${projectId}/sources/${sid}/reindex`, { method: "POST" });
      await refresh();
      await openDetail(sid);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Reindex failed");
    } finally {
      setBusyId(null);
    }
  };

  const deleteSource = async (sid: string) => {
    setBusyId(sid); setError(null);
    try {
      await apiFetch(`/projects/${projectId}/sources/${sid}`, { method: "DELETE" });
      setDetail(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setBusyId(null);
    }
  };

  const downloadOriginal = async (artifactId: string, name: string) => {
    try {
      const blob = await (await apiFetch(`/artifacts/${artifactId}/content`)).blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5_000);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Download failed");
    }
  };

  if (detail) {
    const status = detail.index_status ?? "pending";
    return (
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <button className="btn btn-sm" onClick={() => setDetail(null)}>← All sources</button>
          <div className="flex gap-2">
            {detail.artifact_id && (
              <button className="btn btn-sm" onClick={() => downloadOriginal(detail.artifact_id!, detail.title ?? "source")}>
                Download original
              </button>
            )}
            <button className="btn btn-sm" onClick={() => reindexSource(detail.id)} disabled={busyId === detail.id}>
              {busyId === detail.id ? "Working…" : "Reindex"}
            </button>
            <button className="btn btn-sm btn-danger-soft" onClick={() => deleteSource(detail.id)} disabled={busyId === detail.id}>
              Delete
            </button>
          </div>
        </div>

        <div className="surface border border-soft rounded-xl px-5 py-4">
          <div className="font-medium text-[15px]">{detail.title ?? "Untitled source"}</div>
          <div className="flex flex-wrap gap-3 mt-1.5 text-[12px]" style={{ color: "var(--text-faint)" }}>
            <span>{detail.source_type ?? "upload"}</span>
            <span>parse: {detail.parse_status ?? "—"}</span>
            <span style={{ color: SOURCE_STATUS_COLOR[status] ?? "var(--text-faint)" }}>index: {status}</span>
            <span>{detail.chunk_count} chunk{detail.chunk_count === 1 ? "" : "s"}</span>
          </div>
          {detail.warning && (
            <div className="mt-3 text-[12.5px]" style={{ color: "var(--danger)" }}>{detail.warning}</div>
          )}
        </div>

        {error && <div className="text-[12.5px]" style={{ color: "var(--danger)" }}>{error}</div>}

        <div className="text-[12px] font-medium" style={{ color: "var(--text-dim)" }}>
          Extracted text {detail.chunk_count > detail.chunks.length ? `(first ${detail.chunks.length} of ${detail.chunk_count} chunks)` : ""}
        </div>
        {detail.chunks.length === 0 ? (
          <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No indexed chunks for this source.</div>
        ) : (
          <div className="flex flex-col gap-2">
            {detail.chunks.map(ch => (
              <div key={ch.chunk_index} className="surface border border-soft rounded-xl px-4 py-3">
                <div className="text-[11px] mb-1" style={{ color: "var(--text-faint)" }}>Chunk {ch.chunk_index}</div>
                <div className="text-[13px] whitespace-pre-wrap" style={{ color: "var(--text)" }}>{ch.content}</div>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <div className="text-[13px]" style={{ color: "var(--text-dim)" }}>
          Uploaded files and connector feeds indexed for this project.
        </div>
        <button className="btn btn-sm" onClick={() => setAdding(a => !a)}>
          {adding ? "Cancel" : "Add connector source"}
        </button>
      </div>

      {adding && (
        <div className="surface border border-soft rounded-xl px-5 py-4 flex flex-col gap-3">
          <input
            className="input"
            placeholder="Source title (e.g. Sales inbox)"
            value={title}
            onChange={e => setTitle(e.target.value)}
          />
          <input
            className="input"
            placeholder="Tool (e.g. gmail.search)"
            value={tool}
            onChange={e => setTool(e.target.value)}
          />
          <div className="flex justify-end">
            <button
              className="btn btn-sm btn-primary"
              onClick={addConnector}
              disabled={busyId === "add" || !title.trim() || !tool.trim()}
            >
              {busyId === "add" ? "Adding…" : "Add source"}
            </button>
          </div>
        </div>
      )}

      {error && <div className="text-[12.5px]" style={{ color: "var(--danger)" }}>{error}</div>}

      {sources.length === 0 ? (
        <EmptyState icon={<IC.Folder size={22} />} title="No sources" sub="Upload a file in a project conversation or add a connector source to build project knowledge." />
      ) : (
        <div className="flex flex-col gap-2">
          {sources.map(src => {
            const status = src.index_status ?? "pending";
            return (
              <button
                key={src.id}
                onClick={() => openDetail(src.id)}
                className="surface border border-soft rounded-xl px-5 py-4 flex items-center gap-4 text-left w-full transition-colors"
              >
                <div className="flex-1 min-w-0">
                  <div className="font-medium text-[14px] truncate">{src.title ?? "Untitled source"}</div>
                  <div className="flex gap-3 mt-0.5 text-[12px]" style={{ color: "var(--text-faint)" }}>
                    <span>{src.source_type ?? "upload"}</span>
                    <span style={{ color: SOURCE_STATUS_COLOR[status] ?? "var(--text-faint)" }}>{status}</span>
                  </div>
                </div>
                {src.source_type === "connector" && status !== "revoked" && (
                  <span
                    role="button"
                    tabIndex={0}
                    className="btn btn-sm"
                    onClick={e => { e.stopPropagation(); syncSource(src.id); }}
                    onKeyDown={e => { if (e.key === "Enter") { e.stopPropagation(); syncSource(src.id); } }}
                  >
                    {busyId === src.id ? "Syncing…" : "Sync"}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function EmptyPanel({ label }: { label: string }) {
  return (
    <div className="flex-1 flex flex-col">
      <PageHeader title={label} />
      <div className="flex-1 flex items-center justify-center px-10 pb-10">
        <div className="surface border border-soft rounded-2xl px-10 py-12 flex flex-col items-center gap-3 text-center max-w-sm">
          <p className="text-[15px] font-medium">{label}</p>
          <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Nothing here yet.</p>
        </div>
      </div>
    </div>
  );
}

function SettingsScreen({ tab, setTab, theme, setTheme, accent, setAccent, signOut }: {
  tab: SettingsTab; setTab: (t: SettingsTab) => void;
  theme: "light" | "dark"; setTheme: (t: "light" | "dark") => void;
  accent: string; setAccent: (a: string) => void;
  signOut: () => void;
}) {
  const [overview, setOverview] = useState<SettingsOverview | null>(null);
  const [drafts, setDrafts] = useState<Record<string, Record<string, unknown>>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [search, setSearch] = useState("");
  const [toast, setToast] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);
  const [confirm, setConfirm] = useState<{ title: string; text: string; required?: string; action: (typed: string) => Promise<void> } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = (await (await apiFetch("/settings/")).json()) as SettingsOverview;
      setOverview(data);
      setDrafts(data.sections);
      const savedTheme = String(data.sections.general?.theme ?? "system");
      if (savedTheme === "light" || savedTheme === "dark") setTheme(savedTheme);
      setToast(null);
    } catch (exc) {
      setToast({ kind: "danger", text: exc instanceof Error ? exc.message : "Unable to load settings" });
    } finally {
      setLoading(false);
    }
  }, [setTheme]);

  useEffect(() => { void load(); }, [load]);

  const visibleTabs = SETTING_TABS.filter(item => {
    const query = search.trim().toLowerCase();
    return !query || item.label.toLowerCase().includes(query) || item.keywords.includes(query);
  });
  const activeMeta = SETTING_TABS.find(item => item.id === tab) ?? SETTING_TABS[0];
  const activeSection = apiSectionForTab(tab);
  const sectionDraft = drafts[activeSection] || {};
  const sectionSaved = overview?.sections?.[activeSection] || {};
  const dirty = JSON.stringify(sectionDraft) !== JSON.stringify(sectionSaved);
  const canAdmin = Boolean(overview?.member.can_admin);
  const readOnly = !canEditTab(tab, canAdmin);

  function patch(section: string, values: Record<string, unknown>) {
    setDrafts(prev => ({ ...prev, [section]: { ...(prev[section] || {}), ...values } }));
  }

  async function save(section = activeSection) {
    if (!overview) return;
    setSaving(true);
    try {
      const data = (await (await apiFetch(`/settings/${section}`, {
        method: "PATCH",
        body: JSON.stringify({ values: drafts[section] || {} }),
      })).json()) as { values: Record<string, unknown> };
      setOverview(prev => prev ? { ...prev, sections: { ...prev.sections, [section]: data.values } } : prev);
      if (section === "general") {
        const savedTheme = String(data.values.theme ?? "system");
        if (savedTheme === "light" || savedTheme === "dark") setTheme(savedTheme);
      }
      setToast({ kind: "ok", text: "Settings saved" });
    } catch (exc) {
      setToast({ kind: "danger", text: exc instanceof Error ? exc.message : "Unable to save settings" });
    } finally {
      setSaving(false);
    }
  }

  function cancel(section = activeSection) {
    if (!overview) return;
    setDrafts(prev => ({ ...prev, [section]: overview.sections[section] || {} }));
  }

  function selectTab(next: SettingsTab) {
    if (dirty && !window.confirm("Discard unsaved changes?")) return;
    setTab(next);
  }

  if (loading) {
    return (
      <div className="flex-1 px-10 py-9">
        <div className="h-7 w-48 rounded-md mb-6" style={{ background: "var(--surface-2)" }}/>
        {[0, 1, 2, 3].map(i => <div key={i} className="h-16 rounded-xl mb-3" style={{ background: "var(--surface-2)" }}/>)}
      </div>
    );
  }

  if (!overview) return <EmptyState icon={<IC.Settings size={20}/>} title="Settings unavailable" sub="The settings API did not return data."/>;

  return (
    <div className="flex-1 flex min-w-0 overflow-hidden">
      <div className="flex-shrink-0 border-r hairline py-5" style={{ width: 260, background: "var(--bg-deep)" }}>
        <div className="px-4 mb-3">
          <div className="text-[11px] font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-dim)" }}>Settings</div>
          <div className="surface border border-soft rounded-lg flex items-center gap-2 px-2.5 py-2">
            <IC.Search size={14} style={{ color: "var(--text-dim)" }}/>
            <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search settings" className="bg-transparent outline-none text-[13px] w-full" aria-label="Search settings"/>
          </div>
        </div>
        <div className="px-3 space-y-0.5 overflow-y-auto" style={{ maxHeight: "calc(100vh - 116px)" }}>
          {visibleTabs.map(item => (
            <button key={item.id} onClick={() => selectTab(item.id)} className={`nav-item w-full ${tab === item.id ? "active" : ""}`}>
              <span className="nav-icon">{item.icon}</span>
              <span className="flex-1 text-left">{item.label}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 min-w-0 overflow-y-auto" style={{ background: "var(--bg)" }}>
        <div className="sticky top-0 z-20 border-b hairline px-10 py-4 flex items-center justify-between" style={{ background: "var(--bg)" }}>
          <div>
            <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>Settings / {activeMeta.label}</div>
            <h1 className="h-section mt-1">{activeMeta.label}</h1>
          </div>
          <div className="flex items-center gap-2">
            {dirty && <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>Unsaved changes</span>}
            {dirty && <button onClick={() => cancel()} className="btn btn-sm">Cancel</button>}
            {!["members", "audit", "security", "billing", "danger"].includes(tab) && (
              <button onClick={() => void save()} disabled={!dirty || readOnly || saving} className="btn btn-accent btn-sm disabled:opacity-50">
                {saving ? "Saving..." : readOnly ? "Read only" : "Save"}
              </button>
            )}
          </div>
        </div>

        <div className="px-10 py-8 max-w-[1040px]">
          {toast && (
            <div className="mb-5 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: toast.kind === "ok" ? "var(--ok)" : "var(--danger)", color: toast.kind === "ok" ? "var(--ok)" : "var(--danger)" }}>
              {toast.text}
            </div>
          )}
          {readOnly && <Unavailable reason="Your role can view this section but cannot edit it."/>}
          {tab === "general" && <GeneralSettings data={sectionDraft} patch={v => patch("general", v)} theme={theme} setTheme={setTheme} accent={accent} setAccent={setAccent}/>}
          {tab === "profile" && <ProfileSettings data={sectionDraft} patch={v => patch("profile", v)} overview={overview}/>}
          {tab === "organization" && <OrganizationSettings data={sectionDraft} patch={v => patch("organization", v)} overview={overview}/>}
          {tab === "members" && <MembersSettings overview={overview} reload={load} setToast={setToast} setConfirm={setConfirm}/>}
          {tab === "permissions" && <PermissionsSettings data={sectionDraft} patch={v => patch("permissions", v)}/>}
          {tab === "employees" && <EmployeeSettings data={sectionDraft} patch={v => patch("ai_employee", v)}/>}
          {tab === "runtime" && <RuntimeSettings data={sectionDraft} patch={v => patch("runtime", v)} health={overview.runtime_health}/>}
          {tab === "memory-settings" && <MemorySettings data={sectionDraft} patch={v => patch("memory", v)} stats={overview.memory_stats} setToast={setToast} setConfirm={setConfirm} reload={load}/>}
          {tab === "tools-settings" && <ToolsSettings data={sectionDraft} patch={v => patch("tool_settings", v)} connectors={overview.connectors} capabilities={overview.capabilities} health={overview.runtime_health.connectors || {}}/>}
          {tab === "approval-settings" && <ApprovalSettings data={sectionDraft} patch={v => patch("approval", v)}/>}
          {tab === "notifications" && <NotificationSettings data={sectionDraft} patch={v => patch("notifications", v)} capabilities={overview.capabilities}/>}
          {tab === "security" && <SecuritySettings capabilities={overview.capabilities} signOut={signOut}/>}
          {tab === "billing" && <BillingSettings overview={overview}/>}
          {tab === "audit" && <AuditSettings />}
          {tab === "developer" && <DeveloperSettings data={sectionDraft} patch={v => patch("developer", v)} capabilities={overview.capabilities}/>}
          {tab === "danger" && <DangerSettings capabilities={overview.capabilities} setToast={setToast}/>}
        </div>
      </div>
      {confirm && <ConfirmModal confirm={confirm} onClose={() => setConfirm(null)}/>}
    </div>
  );
}

function apiSectionForTab(tab: SettingsTab) {
  return ({ "memory-settings": "memory", "tools-settings": "tool_settings", "approval-settings": "approval", employees: "ai_employee" } as Record<string, string>)[tab] || tab;
}

function canEditTab(tab: SettingsTab, canAdmin: boolean) {
  if (["organization", "members", "permissions", "employees", "runtime", "memory-settings", "tools-settings", "approval-settings", "developer", "danger"].includes(tab)) return canAdmin;
  return !["audit", "security", "billing"].includes(tab);
}

function val(data: Record<string, unknown>, key: string, fallback = "") {
  return String(data[key] ?? fallback);
}

function SettingsSection({ title, children, note }: { title: string; children: ReactNode; note?: string }) {
  return <section className="mb-8"><h2 className="text-[16px] font-semibold mb-1">{title}</h2>{note && <p className="text-[13px] mb-3" style={{ color: "var(--text-dim)" }}>{note}</p>}<div className="surface border border-soft rounded-xl overflow-hidden">{children}</div></section>;
}

function SettingsField({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div className="flex items-start justify-between gap-6 px-5 py-4 border-b hairline last:border-b-0"><div className="min-w-0"><div className="text-[14px] font-medium">{label}</div>{hint && <div className="text-[13px] mt-0.5" style={{ color: "var(--text-dim)" }}>{hint}</div>}</div><div className="flex-shrink-0 max-w-[440px]">{children}</div></div>;
}

function TextInput({ value, onChange, disabled = false, wide = false, ariaLabel }: { value: string; onChange: (value: string) => void; disabled?: boolean; wide?: boolean; ariaLabel: string }) {
  return <input aria-label={ariaLabel} disabled={disabled} value={value} onChange={e => onChange(e.target.value)} className={`surface border border-soft rounded-lg px-3 py-2 text-[14px] outline-none disabled:opacity-60 ${wide ? "w-96" : "w-64"}`} style={{ color: "var(--text)" }}/>;
}

function SelectInput({ value, onChange, options, ariaLabel, disabled = false }: { value: string; onChange: (value: string) => void; options: string[]; ariaLabel: string; disabled?: boolean }) {
  return <select aria-label={ariaLabel} disabled={disabled} value={value} onChange={e => onChange(e.target.value)} className="surface border border-soft rounded-lg px-3 py-2 text-[14px] outline-none w-64 disabled:opacity-60">{options.map(option => <option key={option} value={option}>{option}</option>)}</select>;
}

function Toggle({ checked, onChange, disabled = false, label }: { checked: boolean; onChange: (value: boolean) => void; disabled?: boolean; label: string }) {
  return <button aria-label={label} disabled={disabled} onClick={() => onChange(!checked)} className="rounded-full smooth disabled:opacity-50" style={{ width: 42, height: 24, background: checked ? "var(--accent)" : "var(--border)", position: "relative" }}><span className="absolute top-1 rounded-full bg-white smooth" style={{ width: 16, height: 16, left: checked ? 22 : 3, boxShadow: "0 1px 2px rgba(0,0,0,0.2)" }}/></button>;
}

function Unavailable({ reason }: { reason: string }) {
  return <div className="mb-5 surface border border-soft rounded-xl px-4 py-3 flex gap-3"><IC.Info size={16} style={{ color: "var(--text-dim)" }}/><p className="text-[13px]" style={{ color: "var(--text-dim)" }}>{reason}</p></div>;
}

function GeneralSettings({ data, patch, theme, setTheme, accent, setAccent }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; theme: "light" | "dark"; setTheme: (t: "light" | "dark") => void; accent: string; setAccent: (a: string) => void }) {
  const notifications = (data.notifications || {}) as Record<string, unknown>;
  return <>
    <SettingsSection title="Workspace basics"><SettingsField label="Workspace name"><TextInput ariaLabel="Workspace name" value={val(data, "workspace_name")} onChange={workspace_name => patch({ workspace_name })}/></SettingsField><SettingsField label="Description"><TextInput ariaLabel="Workspace description" wide value={val(data, "workspace_description")} onChange={workspace_description => patch({ workspace_description })}/></SettingsField><SettingsField label="Icon/avatar"><TextInput ariaLabel="Workspace icon" value={val(data, "workspace_icon")} onChange={workspace_icon => patch({ workspace_icon })}/></SettingsField><SettingsField label="Default landing page"><SelectInput ariaLabel="Default landing page" value={val(data, "default_landing_page", "chat")} onChange={default_landing_page => patch({ default_landing_page })} options={["chat", "activity", "approvals", "memory", "connectors", "assistants"]}/></SettingsField></SettingsSection>
    <SettingsSection title="Locale and appearance"><SettingsField label="Time zone"><TextInput ariaLabel="Time zone" value={val(data, "time_zone")} onChange={time_zone => patch({ time_zone })}/></SettingsField><SettingsField label="Date/time format"><TextInput ariaLabel="Date time format" value={val(data, "date_time_format")} onChange={date_time_format => patch({ date_time_format })}/></SettingsField><SettingsField label="Language"><SelectInput ariaLabel="Language" value={val(data, "language", "en-US")} onChange={language => patch({ language })} options={["en-US"]}/></SettingsField><SettingsField label="Theme"><SelectInput ariaLabel="Theme" value={val(data, "theme", "system")} onChange={themeValue => { patch({ theme: themeValue }); if (themeValue === "light" || themeValue === "dark") setTheme(themeValue); }} options={["system", "light", "dark"]}/></SettingsField><SettingsField label="Accent color"><div className="flex gap-2">{Object.entries(ACCENT_PALETTES).map(([key, p]) => <button key={key} onClick={() => setAccent(key)} title={key} aria-label={`Accent ${key}`} className="w-7 h-7 rounded-full smooth" style={{ background: p.accent, boxShadow: accent === key ? `0 0 0 2px var(--bg), 0 0 0 4px ${p.accent}` : "none" }}/>)}</div></SettingsField></SettingsSection>
    <SettingsSection title="Notification defaults"><SettingsField label="In-app notifications"><Toggle label="In-app notifications" checked={Boolean(notifications.in_app)} onChange={in_app => patch({ notifications: { ...notifications, in_app } })}/></SettingsField><SettingsField label="Email notifications"><Toggle label="Email notifications" checked={Boolean(notifications.email)} onChange={email => patch({ notifications: { ...notifications, email } })}/></SettingsField></SettingsSection>
  </>;
}

function ProfileSettings({ data, patch, overview }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; overview: SettingsOverview }) {
  return <><SettingsSection title="Editable profile"><SettingsField label="Display name"><TextInput ariaLabel="Display name" value={val(data, "display_name")} onChange={display_name => patch({ display_name })}/></SettingsField><SettingsField label="Avatar"><TextInput ariaLabel="Profile avatar" value={val(data, "profile_avatar")} onChange={profile_avatar => patch({ profile_avatar })}/></SettingsField><SettingsField label="Personal preferences"><TextInput ariaLabel="Personal preferences" wide value={val(data, "personal_preferences")} onChange={personal_preferences => patch({ personal_preferences })}/></SettingsField></SettingsSection><SettingsSection title="Identity"><SettingsField label="Email" hint={overview.capabilities.email_edit.reason}><TextInput ariaLabel="Email" disabled value={overview.member.email} onChange={() => {}}/></SettingsField><SettingsField label="Role"><Tag>{overview.member.role}</Tag></SettingsField></SettingsSection><SettingsSection title="AI defaults"><SettingsField label="Interaction style"><SelectInput ariaLabel="AI interaction style" value={val(data, "ai_interaction_style", "balanced")} onChange={ai_interaction_style => patch({ ai_interaction_style })} options={["concise", "balanced", "detailed"]}/></SettingsField><SettingsField label="Response length"><SelectInput ariaLabel="Preferred response length" value={val(data, "preferred_response_length", "medium")} onChange={preferred_response_length => patch({ preferred_response_length })} options={["short", "medium", "long"]}/></SettingsField><SettingsField label="Citation/detail level"><SelectInput ariaLabel="Citation detail level" value={val(data, "citation_detail_level", "standard")} onChange={citation_detail_level => patch({ citation_detail_level })} options={["minimal", "standard", "detailed"]}/></SettingsField></SettingsSection></>;
}

function OrganizationSettings({ data, patch, overview }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; overview: SettingsOverview }) {
  return <SettingsSection title="Organization profile" note={overview.member.can_admin ? "Organization changes are persisted and audited." : "Only admins can edit organization settings."}><SettingsField label="Name"><TextInput ariaLabel="Organization name" value={val(data, "name", val(data, "organization_name"))} onChange={organization_name => patch({ organization_name, name: organization_name })}/></SettingsField><SettingsField label="Logo"><TextInput ariaLabel="Organization logo" value={val(data, "logo")} onChange={logo => patch({ logo })}/></SettingsField><SettingsField label="Domain"><TextInput ariaLabel="Organization domain" value={val(data, "domain")} onChange={domain => patch({ domain })}/></SettingsField><SettingsField label="Owner/admin"><Tag>{String(overview.organization.owner ?? "Owner/Admin")}</Tag></SettingsField><SettingsField label="Plan"><Tag variant="accent">{String(overview.organization.plan ?? "trial")}</Tag></SettingsField><SettingsField label="Seats/users"><Tag>{String(overview.organization.seats ?? 0)}</Tag></SettingsField><SettingsField label="Default workspace creation"><SelectInput ariaLabel="Workspace creation permissions" value={val(data, "default_workspace_creation", "admins")} onChange={default_workspace_creation => patch({ default_workspace_creation })} options={["owners", "admins", "managers"]}/></SettingsField></SettingsSection>;
}

function MembersSettings({ overview, reload, setToast, setConfirm }: { overview: SettingsOverview; reload: () => Promise<void>; setToast: (t: { kind: "ok" | "danger"; text: string }) => void; setConfirm: (c: { title: string; text: string; required?: string; action: (typed: string) => Promise<void> } | null) => void }) {
  const [query, setQuery] = useState(""); const [role, setRole] = useState("all");
  const canAdmin = overview.member.can_admin;
  const rows = overview.members.filter(m => (role === "all" || m.role === role) && `${m.name} ${m.email}`.toLowerCase().includes(query.toLowerCase()));
  async function changeRole(id: string, nextRole: string) { try { await apiFetch(`/settings/members/${id}/role`, { method: "PATCH", body: JSON.stringify({ role: nextRole }) }); setToast({ kind: "ok", text: "Role updated" }); await reload(); } catch (exc) { setToast({ kind: "danger", text: exc instanceof Error ? exc.message : "Unable to update role" }); } }
  async function remove(id: string) { try { await apiFetch(`/settings/members/${id}`, { method: "DELETE" }); setToast({ kind: "ok", text: "Member removed" }); await reload(); } catch (exc) { setToast({ kind: "danger", text: exc instanceof Error ? exc.message : "Unable to remove member" }); } }
  return <><Unavailable reason={overview.capabilities.invitations.reason}/><div className="flex gap-2 mb-4"><TextInput ariaLabel="Search members" value={query} onChange={setQuery}/><SelectInput ariaLabel="Filter by role" value={role} onChange={setRole} options={["all", "owner", "admin", "manager", "operator", "viewer"]}/><button className="btn btn-sm" disabled title={overview.capabilities.invitations.reason}>Invite member</button></div><div className="surface border border-soft rounded-xl overflow-hidden">{rows.map(member => <div key={member.id} className="grid grid-cols-[1fr_150px_120px] gap-3 items-center px-4 py-3 border-b hairline last:border-b-0"><div><div className="font-medium text-[14px]">{member.name}</div><div className="text-[12px]" style={{ color: "var(--text-dim)" }}>{member.email} · {member.status}</div></div><SelectInput ariaLabel={`Role for ${member.email}`} disabled={!canAdmin} value={member.role} onChange={next => void changeRole(member.id, next)} options={["owner", "admin", "manager", "operator", "viewer"]}/><button disabled={!canAdmin || member.is_self} className="btn btn-danger-soft btn-sm disabled:opacity-50" onClick={() => setConfirm({ title: "Remove member", text: `Remove ${member.email} from this organization?`, action: async () => remove(member.id) })}>Remove</button></div>)}</div></>;
}

function PermissionsSettings({ data, patch }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void }) {
  const roles = (data.roles || {}) as Record<string, Record<string, string>>; const areas = ["workspace", "employee", "tools", "approvals", "memory", "audit"];
  return <SettingsSection title="Permission matrix" note="Changes are persisted and read by settings policy. Dangerous tool approvals are enforced by the tool broker."><div className="overflow-x-auto"><table className="w-full text-[13px]"><thead><tr><th className="text-left p-3">Role</th>{areas.map(a => <th key={a} className="text-left p-3 capitalize">{a}</th>)}</tr></thead><tbody>{Object.keys(roles).map(role => <tr key={role} className="border-t hairline"><td className="p-3 font-medium">{role}</td>{areas.map(area => <td key={area} className="p-2"><SelectInput ariaLabel={`${role} ${area}`} value={roles[role]?.[area] || "deny"} onChange={state => patch({ roles: { ...roles, [role]: { ...(roles[role] || {}), [area]: state } } })} options={["allow", "deny", "approval_required"]}/></td>)}</tr>)}</tbody></table></div></SettingsSection>;
}

function EmployeeSettings({ data, patch }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void }) {
  return <><SettingsSection title="Creation and memory policy"><SettingsField label="Creation policy"><SelectInput ariaLabel="Employee creation policy" value={val(data, "creation_policy")} onChange={creation_policy => patch({ creation_policy })} options={["admins_only", "admins_and_managers", "operators_with_approval"]}/></SettingsField><SettingsField label="Default memory scope"><SelectInput ariaLabel="Employee memory scope" value={val(data, "memory_scope")} onChange={memory_scope => patch({ memory_scope })} options={["user", "workspace", "org"]}/></SettingsField><SettingsField label="Tool access mode"><SelectInput ariaLabel="Employee tool access mode" value={val(data, "tool_access_mode")} onChange={tool_access_mode => patch({ tool_access_mode })} options={["disabled", "approval_required", "enabled"]}/></SettingsField></SettingsSection><SettingsSection title="Runtime limits"><SettingsField label="Runtime auto-start"><Toggle label="Runtime auto-start" checked={Boolean(data.runtime_auto_start)} onChange={runtime_auto_start => patch({ runtime_auto_start })}/></SettingsField><SettingsField label="Idle timeout minutes"><TextInput ariaLabel="Runtime idle timeout" value={val(data, "runtime_idle_timeout_minutes")} onChange={runtime_idle_timeout_minutes => patch({ runtime_idle_timeout_minutes: Number(runtime_idle_timeout_minutes) || 0 })}/></SettingsField><SettingsField label="Max concurrent runtimes"><TextInput ariaLabel="Max concurrent runtimes" value={val(data, "max_concurrent_runtimes")} onChange={max_concurrent_runtimes => patch({ max_concurrent_runtimes: Number(max_concurrent_runtimes) || 0 })}/></SettingsField><SettingsField label="Max sub-agent depth"><TextInput ariaLabel="Max sub-agent depth" value={val(data, "max_sub_agent_depth")} onChange={max_sub_agent_depth => patch({ max_sub_agent_depth: Number(max_sub_agent_depth) || 0 })}/></SettingsField><SettingsField label="Sub-agent spawning"><Toggle label="Sub-agent spawning" checked={Boolean(data.sub_agent_spawning)} onChange={sub_agent_spawning => patch({ sub_agent_spawning })}/></SettingsField></SettingsSection></>;
}

function RuntimeSettings({ data, patch, health }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; health: Record<string, unknown> }) {
  return <><Unavailable reason={`Current runtime health: ${String(health.status)}. High-risk changes require confirmation before production rollout.`}/><SettingsSection title="Runtime operation"><SettingsField label="Runtime mode"><SelectInput ariaLabel="Runtime mode" value={val(data, "runtime_mode")} onChange={runtime_mode => patch({ runtime_mode })} options={["local", "demo", "managed"]}/></SettingsField><SettingsField label="Isolation"><SelectInput ariaLabel="Isolation" value={val(data, "isolation")} onChange={isolation => patch({ isolation })} options={["process", "container_unavailable"]}/></SettingsField><SettingsField label="Heartbeat seconds"><TextInput ariaLabel="Heartbeat interval" value={val(data, "heartbeat_interval_seconds")} onChange={heartbeat_interval_seconds => patch({ heartbeat_interval_seconds: Number(heartbeat_interval_seconds) || 0 })}/></SettingsField><SettingsField label="Restart policy"><SelectInput ariaLabel="Restart policy" value={val(data, "restart_policy")} onChange={restart_policy => patch({ restart_policy })} options={["never", "on_failure", "always"]}/></SettingsField><SettingsField label="Log retention days"><TextInput ariaLabel="Log retention" value={val(data, "log_retention_days")} onChange={log_retention_days => patch({ log_retention_days: Number(log_retention_days) || 0 })}/></SettingsField><SettingsField label="Max task queue size"><TextInput ariaLabel="Max task queue size" value={val(data, "max_task_queue_size")} onChange={max_task_queue_size => patch({ max_task_queue_size: Number(max_task_queue_size) || 0 })}/></SettingsField><SettingsField label="Failure recovery"><SelectInput ariaLabel="Failure recovery" value={val(data, "failure_recovery")} onChange={failure_recovery => patch({ failure_recovery })} options={["resume", "stop", "restart"]}/></SettingsField><SettingsField label="Token budget daily"><TextInput ariaLabel="Token budget" value={val(data, "token_budget_daily")} onChange={token_budget_daily => patch({ token_budget_daily: Number(token_budget_daily) || 0 })}/></SettingsField></SettingsSection></>;
}

function MemorySettings({ data, patch, stats, setToast, setConfirm, reload }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; stats: { active: number; deleted: number }; setToast: (t: { kind: "ok" | "danger"; text: string }) => void; setConfirm: (c: { title: string; text: string; required?: string; action: (typed: string) => Promise<void> } | null) => void; reload: () => Promise<void> }) {
  async function purge() { try { await apiFetch("/settings/memory/purge", { method: "POST", body: JSON.stringify({ confirmation: "PURGE MEMORY" }) }); setToast({ kind: "ok", text: "Memory purged" }); await reload(); } catch (exc) { setToast({ kind: "danger", text: exc instanceof Error ? exc.message : "Unable to purge memory" }); } }
  async function exportMemoryJson() {
    try {
      const blob = await (await apiFetch("/settings/memory/export.json")).blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "chronos-memory-org-export.json";
      anchor.click();
      URL.revokeObjectURL(url);
      setToast({ kind: "ok", text: "Organization memory JSON exported." });
    } catch (exc) {
      setToast({ kind: "danger", text: exc instanceof Error ? exc.message : "Unable to export memory" });
    }
  }
  return <><SettingsSection title="Memory policy" note={`${stats.active} active memories, ${stats.deleted} deleted.`}><SettingsField label="Workspace memory"><Toggle label="Workspace memory" checked={Boolean(data.workspace_memory)} onChange={workspace_memory => patch({ workspace_memory })}/></SettingsField><SettingsField label="Employee memory"><Toggle label="Employee memory" checked={Boolean(data.employee_memory)} onChange={employee_memory => patch({ employee_memory })}/></SettingsField><SettingsField label="User memory"><Toggle label="User memory" checked={Boolean(data.user_memory)} onChange={user_memory => patch({ user_memory })}/></SettingsField><SettingsField label="Retention days"><TextInput ariaLabel="Memory retention" value={val(data, "retention_days")} onChange={retention_days => patch({ retention_days: Number(retention_days) || 0 })}/></SettingsField><SettingsField label="Review required"><Toggle label="Memory review required" checked={Boolean(data.review_required)} onChange={review_required => patch({ review_required })}/></SettingsField><SettingsField label="Auto-save memory"><Toggle label="Auto-save memory" checked={Boolean(data.auto_save)} onChange={auto_save => patch({ auto_save })}/></SettingsField><SettingsField label="Sensitive detection"><Toggle label="Sensitive memory detection" checked={Boolean(data.sensitive_detection)} onChange={sensitive_detection => patch({ sensitive_detection })}/></SettingsField></SettingsSection><SettingsSection title="Memory danger zone"><SettingsField label="Export organization memory" hint="Downloads JSON for non-deleted organization memory, including archived entries and excluding superseded entries."><button className="btn btn-sm" onClick={() => void exportMemoryJson()}>Export JSON</button></SettingsField><SettingsField label="Purge all memory" hint="Requires typed confirmation and writes an audit entry."><button className="btn btn-danger-soft btn-sm" onClick={() => setConfirm({ title: "Purge memory", text: "This soft-deletes all active memory entries in this workspace.", required: "PURGE MEMORY", action: async () => purge() })}>Purge memory</button></SettingsField></SettingsSection></>;
}

function ToolsSettings({ data, patch, connectors, capabilities, health }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; connectors: SettingsOverview["connectors"]; capabilities: SettingsOverview["capabilities"]; health: NonNullable<SettingsOverview["runtime_health"]["connectors"]> }) {
  function update(provider: string, values: Record<string, unknown>) { patch({ [provider]: { ...((data[provider] || {}) as Record<string, unknown>), ...values } }); }
  const providers = Object.entries(health);
  return <><SettingsSection title="Connector readiness" note="Chronos can run tools in fixture, demo, or live mode per connector.">{providers.length === 0 ? <div className="p-5"><EmptyState title="No connector checks available" sub="Startup health has not reported tool readiness yet."/></div> : providers.map(([provider, item]) => <div key={provider} className="px-5 py-4 border-b hairline last:border-b-0"><div className="flex items-start justify-between gap-4"><div><div className="font-medium capitalize">{provider}</div><div className="text-[12px] mt-1" style={{ color: "var(--text-dim)" }}>{item.reason || "Ready"}</div>{item.setup && <div className="text-[12px] mt-1 font-mono" style={{ color: "var(--text-muted)" }}>{item.setup}</div>}</div><Tag variant={item.tier === "live" ? "accent" : item.tier === "demo" ? "info" : "warn"}>{item.tier || item.status || "unknown"}</Tag></div></div>)}</SettingsSection><SettingsSection title="Connected tools" note="Enable/disable and approval-required states are enforced by the tool broker.">{connectors.length === 0 ? <div className="p-5"><EmptyState title="No tools connected" sub="Connectors appear here after OAuth or local enablement."/></div> : connectors.map(c => { const policy = ((data[c.provider] || c.policy || {}) as Record<string, unknown>); return <div key={c.id} className="px-5 py-4 border-b hairline last:border-b-0"><div className="flex justify-between gap-4"><div><div className="font-medium capitalize">{c.provider}</div><div className="text-[12px]" style={{ color: "var(--text-dim)" }}>{c.account_handle || "Org-level connector"} · {c.status} · last used {c.last_used_at || "never"}</div><div className="mt-2 flex flex-wrap gap-1">{(c.scopes || []).map(scope => <Tag key={scope}>{scope}</Tag>)}<Tag variant={String(policy.risk) === "high" ? "danger" : "info"}>{String(policy.risk || "unknown")} risk</Tag></div></div><div className="flex items-center gap-4"><Toggle label={`${c.provider} enabled`} checked={policy.enabled !== false} onChange={enabled => update(c.provider, { enabled })}/><Toggle label={`${c.provider} approval required`} checked={Boolean(policy.approval_required)} onChange={approval_required => update(c.provider, { approval_required })}/><button className="btn btn-danger-soft btn-sm" disabled title="Disconnect is managed from Connectors until scoped confirmation is added.">Disconnect</button></div></div></div>; })}</SettingsSection><Unavailable reason={capabilities.notification_email_dispatch.reason}/></>;
}

function ApprovalSettings({ data, patch }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void }) {
  const thresholds = (data.thresholds || {}) as Record<string, string>;
  return <SettingsSection title="Approval policy"><SettingsField label="Mode"><SelectInput ariaLabel="Approval mode" value={val(data, "mode")} onChange={mode => patch({ mode })} options={["off", "low-risk auto", "manual", "strict"]}/></SettingsField>{["low", "medium", "high"].map(level => <SettingsField key={level} label={`${level} risk threshold`}><SelectInput ariaLabel={`${level} risk threshold`} value={thresholds[level] || "manual"} onChange={value => patch({ thresholds: { ...thresholds, [level]: value } })} options={["auto", "manual", "strict", "blocked"]}/></SettingsField>)}<SettingsField label="Rule builder"><pre className="text-[12px] overflow-auto max-w-md" style={{ color: "var(--text-muted)" }}>{JSON.stringify(data.rules || [], null, 2)}</pre></SettingsField></SettingsSection>;
}

function NotificationSettings({ data, patch, capabilities }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; capabilities: SettingsOverview["capabilities"] }) {
  return <><Unavailable reason={capabilities.notification_email_dispatch.reason}/><SettingsSection title="Notification categories">{["email", "in_app", "runtime_failure_alerts", "approval_request_alerts", "task_completion_alerts", "weekly_digest", "security_alerts"].map(key => <SettingsField key={key} label={key.replaceAll("_", " ")}><Toggle label={key} checked={Boolean(data[key])} onChange={value => patch({ [key]: value })} disabled={key === "email"}/></SettingsField>)}</SettingsSection></>;
}

function SecuritySettings({ capabilities, signOut }: { capabilities: SettingsOverview["capabilities"]; signOut: () => void }) {
  return <><SettingsSection title="Authentication">{["sessions", "password", "two_factor", "api_keys"].map(key => <SettingsField key={key} label={key.replaceAll("_", " ")}><button className="btn btn-sm" disabled>{capabilities[key]?.reason || "Unavailable"}</button></SettingsField>)}<SettingsField label="Current session"><button onClick={signOut} className="btn btn-danger-soft btn-sm">Sign out</button></SettingsField></SettingsSection></>;
}

function BillingSettings({ overview }: { overview: SettingsOverview }) {
  return <><Unavailable reason={overview.capabilities.billing.reason}/><SettingsSection title="Read-only usage summary"><SettingsField label="Current plan"><Tag variant="accent">{String(overview.organization.plan || "trial")}</Tag></SettingsField><SettingsField label="Seats"><Tag>{String(overview.organization.seats || 0)}</Tag></SettingsField><SettingsField label="Runtime usage"><Tag>Not metered</Tag></SettingsField><SettingsField label="Token/model usage"><Tag>Not metered</Tag></SettingsField><SettingsField label="Storage usage"><Tag>{overview.memory_stats.active} memory entries</Tag></SettingsField></SettingsSection></>;
}

function AuditSettings() {
  const [rows, setRows] = useState<Array<Record<string, unknown>>>([]);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  useEffect(() => { apiFetch(`/settings/audit${query ? `?query=${encodeURIComponent(query)}` : ""}`).then(r => r.json()).then(setRows).catch(() => setRows([])); }, [query]);
  async function exportCsv() {
    const blob = await (await apiFetch("/settings/audit/export.csv")).blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "chronos-audit.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  }
  return <><div className="flex gap-2 mb-4"><TextInput ariaLabel="Search audit logs" value={query} onChange={setQuery}/><button className="btn btn-sm" onClick={() => void exportCsv()}>Export CSV</button></div><div className="surface border border-soft rounded-xl overflow-hidden">{rows.map(row => <button key={String(row.id)} onClick={() => setSelected(row)} className="w-full text-left px-4 py-3 border-b hairline last:border-b-0 hover:bg-[var(--surface-2)]"><div className="font-medium text-[13px]">{String(row.action)}</div><div className="text-[12px]" style={{ color: "var(--text-dim)" }}>{String(row.actor_id || "system")} · {row.created_at ? new Date(String(row.created_at)).toLocaleString() : "—"}</div></button>)}</div>{selected && <div className="mt-4 surface border border-soft rounded-xl p-4"><div className="font-medium mb-2">Audit detail</div><pre className="text-[12px] overflow-auto" style={{ color: "var(--text-muted)" }}>{JSON.stringify(selected, null, 2)}</pre></div>}</>;
}

function DeveloperSettings({ data, patch, capabilities }: { data: Record<string, unknown>; patch: (v: Record<string, unknown>) => void; capabilities: SettingsOverview["capabilities"] }) {
  return <><Unavailable reason="Secrets and provider API keys are never exposed in the frontend."/><SettingsSection title="Advanced"><SettingsField label="Environment"><Tag>dev</Tag></SettingsField><SettingsField label="API mode"><SelectInput ariaLabel="API mode" value={val(data, "api_mode")} onChange={api_mode => patch({ api_mode })} options={["local", "staging", "production"]}/></SettingsField><SettingsField label="Debug logging"><Toggle label="Debug logging" checked={Boolean(data.debug_logging)} onChange={debug_logging => patch({ debug_logging })}/></SettingsField><SettingsField label="Experimental features"><Toggle label="Experimental features" checked={Boolean(data.experimental_features)} onChange={experimental_features => patch({ experimental_features })}/></SettingsField><SettingsField label="Webhooks"><button className="btn btn-sm" disabled>{capabilities.webhooks.reason}</button></SettingsField></SettingsSection></>;
}

function DangerSettings({ capabilities, setToast }: { capabilities: SettingsOverview["capabilities"]; setToast: (t: { kind: "ok" | "danger"; text: string }) => void }) {
  return <SettingsSection title="Danger zone" note="Unsupported destructive actions are disabled until backend archival workflows exist."><SettingsField label="Reset workspace settings"><button className="btn btn-danger-soft btn-sm" onClick={() => setToast({ kind: "danger", text: "Reset is not implemented because there is no versioned rollback workflow." })}>Reset unavailable</button></SettingsField><SettingsField label="Delete workspace"><button className="btn btn-danger-soft btn-sm" disabled>{capabilities.delete_workspace.reason}</button></SettingsField><SettingsField label="Leave organization"><button className="btn btn-danger-soft btn-sm" disabled>Leaving the only local workspace is not supported.</button></SettingsField><SettingsField label="Transfer ownership"><button className="btn btn-danger-soft btn-sm" disabled>{capabilities.transfer_ownership.reason}</button></SettingsField></SettingsSection>;
}

function ConfirmModal({ confirm, onClose }: { confirm: { title: string; text: string; required?: string; action: (typed: string) => Promise<void> }; onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const canRun = !confirm.required || typed === confirm.required;
  return <div className="fixed inset-0 z-[100] flex items-center justify-center" style={{ background: "rgba(0,0,0,.22)" }} role="dialog" aria-modal="true"><div className="surface border border-soft rounded-xl p-5 w-[420px]"><h2 className="text-[16px] font-semibold">{confirm.title}</h2><p className="text-[13px] mt-2" style={{ color: "var(--text-dim)" }}>{confirm.text}</p>{confirm.required && <TextInput ariaLabel="Confirmation text" wide value={typed} onChange={setTyped}/>}<div className="flex justify-end gap-2 mt-5"><button className="btn btn-sm" onClick={onClose}>Cancel</button><button className="btn btn-danger-soft btn-sm" disabled={!canRun} onClick={() => { void confirm.action(typed).then(onClose); }}>Confirm</button></div></div></div>;
}

export default function ChronosApp() {
  return (
    <ErrorBoundary>
      <ChronosAppInner />
    </ErrorBoundary>
  );
}
