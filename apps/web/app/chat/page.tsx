"use client";

import { ReactNode, useEffect, useRef, useState, useMemo, useCallback } from "react";
import { usePathname, useRouter } from "next/navigation";

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

// ─── Types ───────────────────────────────────────────────────────────────────
type Route = "chat" | "activity" | "approvals" | "memory" | "connectors" | "assistants" | "settings";
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
  artifacts?: ArtifactRef[];
};
type ToolTrace = { id: string; tool: string; summary: string; status: MessageStatus };
type ArtifactRef = { id: string; title: string; kind: string; mime_type?: string; size_bytes?: number };
type MemoryEntry = { id: string; scope: string; scope_id: string; content: string; source: string; importance_score?: number; created_by?: string | null; created_at?: string };
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
type Task = { id: string; status: string; goal: string; current_step: number; plan?: TaskStep[]; result?: Record<string, unknown>; error?: string | null; created_at?: string; parent_task_id?: string | null; depth?: number };
type TaskStep = { id: string; action: string; description: string; tool?: string | null };
type ChatModel = { id: string; label: string; model: string; description?: string };
type TaskStreamEvent = {
  type: string;
  task_id?: string;
  ts?: string;
  task?: Task;
  step?: TaskStep;
  step_index?: number;
  approval_ids?: string[];
  step_id?: string;
  error?: string;
  result?: unknown;
  attempt?: number;
};
type Approval = { id: string; task_id: string; step_id: string; action_type: string; action_payload: Record<string, unknown>; requested_at?: string; status: string };
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
  if (pathname === "/settings") return "settings";
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

// ─── Root App ─────────────────────────────────────────────────────────────────
export default function ChronosApp() {
  const router = useRouter();
  const pathname = usePathname();
  const [route, setRoute] = useState<Route>(() => routeFromPath(pathname));
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("general");
  const [activeConvoId, setActiveConvoId] = useState<string | null>(null);
  const [activePersonaId, setActivePersonaId] = useState(() => initialPersonaId());
  const [newConversationOpen, setNewConversationOpen] = useState(() => initialNewConversationOpen());
  const newConversationOpenRef = useRef(newConversationOpen);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [accent, setAccent] = useState("coral");

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
    void loadConversations();
    void loadPendingApprovals();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setRoute(routeFromPath(pathname));
  }, [pathname]);

  useEffect(() => {
    newConversationOpenRef.current = newConversationOpen;
  }, [newConversationOpen]);

  function navigateRoute(next: Route) {
    setRoute(next);
    router.push(pathForRoute(next));
  }

  async function loadConversations(selectId?: string) {
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
    } catch { /* silently fail */ }
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
        activeConvoId={activeConvoId}
        onSelectConvo={(id) => { setActiveConvoId(id); setNewConversationOpen(false); newConversationOpenRef.current = false; navigateRoute("chat"); }}
        onNewConvo={() => { setActiveConvoId(null); setNewConversationOpen(true); newConversationOpenRef.current = true; navigateRoute("chat"); }}
        onDeleteConvo={deleteConversation}
        pendingApprovals={pendingApprovals}
        onOpenSettings={openSettings}
        onSignOut={signOut}
      />

      <main className="flex-1 min-w-0 flex flex-col" style={{ background: "var(--bg)" }}>
        {route === "chat"       && <ChatScreen activeConvoId={activeConvoId} activePersonaId={activePersonaId} onConvoCreated={(id) => loadConversations(id)} />}
        {route === "activity"   && <ActivityScreen />}
        {route === "approvals"  && <ApprovalsScreen onDecision={loadPendingApprovals} />}
        {route === "memory"     && <MemoryScreen />}
        {route === "connectors" && <ConnectorsScreen />}
        {route === "assistants" && <AssistantsScreen onStartConversation={(personaId) => {
          const target = `/chat?persona=${encodeURIComponent(personaId)}&new=1`;
          setActivePersonaId(personaId);
          setActiveConvoId(null);
          setNewConversationOpen(true);
          newConversationOpenRef.current = true;
          setRoute("chat");
          if (typeof window !== "undefined") window.location.href = target;
          else router.push(target);
        }} />}
        {route === "settings"   && <SettingsScreen tab={settingsTab} setTab={setSettingsTab} theme={theme} setTheme={setTheme} accent={accent} setAccent={setAccent} signOut={signOut} />}
      </main>
    </div>
  );
}

// ─── Sidebar ──────────────────────────────────────────────────────────────────
function Sidebar({
  collapsed, onCollapse, onExpand, route, onNavigate, conversations, activeConvoId,
  onSelectConvo, onNewConvo, onDeleteConvo, pendingApprovals, onOpenSettings, onSignOut
}: {
  collapsed: boolean; onCollapse: () => void; onExpand: () => void;
  route: Route; onNavigate: (r: Route) => void;
  conversations: Conversation[]; activeConvoId: string | null;
  onSelectConvo: (id: string) => void; onNewConvo: () => void;
  onDeleteConvo: (id: string) => void; pendingApprovals: number;
  onOpenSettings: (tab: SettingsTab) => void; onSignOut: () => void;
}) {
  const [accountOpen, setAccountOpen] = useState(false);
  const [convoMenu, setConvoMenu] = useState<string | null>(null);

  const nav = [
    { id: "activity"   as Route, icon: <IC.Activity size={15}/>,   label: "Activity" },
    { id: "approvals"  as Route, icon: <IC.Approvals size={15}/>,  label: "Approvals",  badge: pendingApprovals || null, badgeKind: "warn" },
    { id: "memory"     as Route, icon: <IC.Memory size={15}/>,     label: "Memory" },
    { id: "connectors" as Route, icon: <IC.Connectors size={15}/>, label: "Connectors" },
    { id: "assistants" as Route, icon: <IC.Personas size={15}/>,   label: "Assistants" },
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
        {nav.map(it => (
          <button key={it.id} onClick={() => onNavigate(it.id)} title={it.label}
                  className="btn btn-ghost btn-icon relative"
                  style={{ background: route === it.id ? "var(--surface-2)" : "transparent",
                           color: route === it.id ? "var(--text)" : "var(--text-muted)" }}>
            {it.icon}
            {it.badge ? (
              <span className="absolute -top-0.5 -right-0.5 min-w-[14px] h-[14px] px-1 rounded-full text-[9px] font-semibold flex items-center justify-center"
                    style={{ background: "var(--warn)", color: "white" }}>{it.badge}</span>
            ) : null}
          </button>
        ))}
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
      <div className="px-3 pt-1 pb-2">
        <button onClick={onNewConvo}
                className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg smooth surface border border-soft hover:border-[var(--border)] whitespace-nowrap">
          <IC.Plus size={15} stroke={2}/>
          <span className="text-[13.5px] font-medium">New conversation</span>
        </button>
      </div>

      {/* Top nav */}
      <div className="px-3 pt-2 pb-1 space-y-0.5">
        {nav.map(it => (
          <button key={it.id} onClick={() => onNavigate(it.id)}
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

      {/* Conversation list */}
      <div className="flex-1 overflow-y-auto px-2 pt-1 no-scrollbar">
        {groups.length === 0 && (
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
  onConvoCreated,
}: {
  activeConvoId: string | null;
  activePersonaId: string;
  onConvoCreated: (id: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatModels, setChatModels] = useState<ChatModel[]>([]);
  const [selectedModel, setSelectedModel] = useState("auto");
  const [streaming, setStreaming] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isEmpty = !activeConvoId;

  const activePersona = PERSONAS.find(p => p.id === activePersonaId) ?? PERSONAS[0];

  useEffect(() => {
    apiFetch("/chat/models")
      .then(r => r.json())
      .then((data: ChatModel[]) => {
        setChatModels(data);
        const preferred = data.find(model => model.id === "agent") ?? data.find(model => model.id === "openrouter") ?? data.find(model => model.id === "backup") ?? data.find(model => model.id === "fast") ?? data[0];
        if (preferred && (!data.some(model => model.id === selectedModel) || selectedModel === "auto")) setSelectedModel(preferred.id);
      })
      .catch(() => {
        setChatModels([{ id: "auto", label: "Auto", model: "auto", description: "Default model routing" }]);
        setSelectedModel("auto");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!activeConvoId) { setMessages([]); return; }
    // Load messages and artifacts together so artifacts reappear on refresh,
    // grouped under the assistant message that produced them.
    Promise.all([
      apiFetch(`/chat/conversations/${activeConvoId}/messages`).then(r => r.json()),
      apiFetch(`/artifacts?conversation_id=${activeConvoId}`).then(r => r.json()).catch(() => []),
    ])
      .then(([msgs, arts]: [Message[], Array<{ id: string; message_id?: string | null; title?: string; kind: string; mime_type?: string; size_bytes?: number }>]) => {
        const byMessage = new Map<string, ArtifactRef[]>();
        for (const a of arts || []) {
          if (!a.message_id) continue;
          const ref: ArtifactRef = { id: a.id, title: a.title ?? "artifact", kind: a.kind, mime_type: a.mime_type, size_bytes: a.size_bytes };
          byMessage.set(a.message_id, [...(byMessage.get(a.message_id) ?? []), ref]);
        }
        setMessages(msgs.map(m => (m.id && byMessage.has(m.id) ? { ...m, artifacts: byMessage.get(m.id) } : m)));
      })
      .catch(() => {});
  }, [activeConvoId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function sendMessage() {
    if (!draft.trim() || streaming) return;
    const text = draft.trim();
    setDraft("");
    setMessages(prev => [...prev, { role: "user", content: text, status: "complete" }]);
    setStreaming(true);

    const ab = new AbortController();
    abortRef.current = ab;

    try {
      let convoId = activeConvoId;
      const resp = await apiFetch("/chat/message", {
        method: "POST",
        body: JSON.stringify({ message: text, conversation_id: convoId, model: selectedModel, persona_id: activePersonaId }),
        signal: ab.signal,
      });

      const reader = resp.body?.getReader();
      if (!reader) { setStreaming(false); return; }

      const decoder = new TextDecoder();
      let partial = "";
      setMessages(prev => [...prev, { role: "assistant", content: "", status: "streaming" }]);

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        for (const line of chunk.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          try {
            const ev = JSON.parse(line.slice(6)) as {
              type: string; content?: string; conversation_id?: string; task_id?: string;
              event?: { type: string; tool?: string; summary?: string; error?: string; args_preview?: Record<string, unknown>; step?: { description?: string }; approval_ids?: string[]; goal?: string };
              artifact?: { artifact_id: string; title?: string; kind?: string; mime_type?: string; size_bytes?: number };
            };
            if (ev.type === "conversation" && ev.conversation_id) {
              convoId = ev.conversation_id;
              onConvoCreated(ev.conversation_id);
            }
            if (ev.type === "token" && ev.content) {
              partial += ev.content;
              setMessages(prev => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") updated[updated.length - 1] = { ...last, content: partial };
                return updated;
              });
            } else if (ev.type === "trace" && ev.event) {
              const te = ev.event;
              const traceType = te.type ?? "";
              const tool = te.tool ?? (traceType === "step_start" ? "think" : traceType === "awaiting_approval" ? "approval" : "");
              let summary = te.summary ?? "";
              let traceStatus: MessageStatus = "complete";
              if (traceType === "tool_call") {
                summary = `${tool.replace(/[._]/g, " ")}…`;
                traceStatus = "streaming";
              } else if (traceType === "tool_result") {
                summary = te.summary ?? `${tool} done`;
                traceStatus = "complete";
              } else if (traceType === "tool_error") {
                summary = te.error ?? `${tool} failed`;
                traceStatus = "error";
              } else if (traceType === "step_start") {
                summary = te.step?.description ?? "Thinking…";
                traceStatus = "streaming";
              } else if (traceType === "step_done") {
                summary = te.summary ?? "Step complete";
                traceStatus = "complete";
              } else if (traceType === "awaiting_approval") {
                summary = `Waiting for approval on ${te.approval_ids?.length ?? 0} item(s)`;
                traceStatus = "approval_pending";
              } else if (traceType === "thinking") {
                summary = te.summary ?? "Thinking…";
                traceStatus = "streaming";
              } else if (traceType === "sub_agent_spawned") {
                summary = `Sub-agent: ${te.goal ?? "working"}`;
                traceStatus = "streaming";
              } else if (traceType === "sub_agent_complete") {
                summary = `Sub-agent finished`;
                traceStatus = "complete";
              }
              const traceId = `${traceType}-${tool}-${Date.now()}`;
              setMessages(prev => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role !== "assistant") return updated;
                const existing = last.tool_traces ?? [];
                // tool_result / tool_error updates the matching pending tool_call trace
                if (traceType === "tool_result" || traceType === "tool_error") {
                  const idx = [...existing].reverse().findIndex(t => t.tool === tool && t.status === "streaming");
                  if (idx >= 0) {
                    const actualIdx = existing.length - 1 - idx;
                    const newTraces = [...existing];
                    newTraces[actualIdx] = { ...newTraces[actualIdx], summary, status: traceStatus };
                    return [...updated.slice(0, -1), { ...last, tool_traces: newTraces }];
                  }
                }
                // step_done closes matching step_start
                if (traceType === "step_done") {
                  const idx = [...existing].reverse().findIndex(t => t.tool === "think" && t.status === "streaming");
                  if (idx >= 0) {
                    const actualIdx = existing.length - 1 - idx;
                    const newTraces = [...existing];
                    newTraces[actualIdx] = { ...newTraces[actualIdx], summary, status: "complete" };
                    return [...updated.slice(0, -1), { ...last, tool_traces: newTraces }];
                  }
                }
                return [...updated.slice(0, -1), { ...last, tool_traces: [...existing, { id: traceId, tool, summary, status: traceStatus }] }];
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
            } else if (ev.type === "task_created" && ev.task_id) {
              setActiveTaskId(ev.task_id);
              // Don't open drawer — we now stream the result inline
            } else if (ev.type === "done") {
              setMessages(prev => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") updated[updated.length - 1] = { ...last, status: "complete" };
                return updated;
              });
            }
          } catch { /* bad JSON */ }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setMessages(prev => {
          const last = prev[prev.length - 1];
          if (last?.role === "assistant") return [...prev.slice(0, -1), { ...last, status: "error", content: last.content || "Something went wrong." }];
          return prev;
        });
      }
    } finally {
      setStreaming(false);
    }
  }

  return (
    <div className="flex-1 flex min-w-0 relative overflow-hidden">
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <div className="px-6 h-[52px] flex items-center justify-between flex-shrink-0 border-b hairline" style={{ background: "var(--bg)" }}>
          <div className="flex items-center gap-3 min-w-0">
            <button className="flex items-center gap-2.5 px-2 py-1 rounded-md smooth hover:bg-[var(--surface-2)]">
              <PersonaAvatar name={activePersona.name} color={activePersona.color} size={22}/>
              <span className="text-[14px] font-medium">{activePersona.name}</span>
              <IC.ChevronDown size={13} style={{ color: "var(--text-dim)" }}/>
            </button>
          </div>
          <div className="flex items-center gap-1">
            {!isEmpty && streaming && !activityOpen && (
              <button onClick={() => setActivityOpen(true)}
                      className="flex items-center gap-2 px-2.5 py-1.5 rounded-md smooth hover:bg-[var(--surface-2)]">
                <StatusDot status="working"/>
                <span className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>Working</span>
              </button>
            )}
            <button className="btn btn-ghost btn-icon"><IC.More size={15}/></button>
          </div>
        </div>

        {/* Messages or empty state */}
        {isEmpty ? (
          <EmptyChatState persona={activePersona} onSubmit={q => { setDraft(q); }} />
        ) : (
          <div className="flex-1 overflow-y-auto px-6 py-10">
            <div className="max-w-[780px] mx-auto space-y-10">
              {messages.map((m, i) => (
                m.role === "user"
                  ? <UserMessage key={i} content={m.content}/>
                  : <AssistantMessage key={i} content={m.content} status={m.status ?? "complete"} persona={activePersona} toolTraces={m.tool_traces} artifacts={m.artifacts}/>
              ))}
              {streaming && messages[messages.length - 1]?.role !== "assistant" && (
                <div className="flex gap-4">
                  <PersonaAvatar name={activePersona.name} color={activePersona.color} size={28}/>
                  <div className="typing-wave mt-2"><span/><span/><span/></div>
                </div>
              )}
              <div ref={bottomRef}/>
            </div>
          </div>
        )}

        {/* Composer */}
        <div className="px-6 pb-6 pt-2" style={{ background: "var(--bg)" }}>
          <div className="max-w-[780px] mx-auto">
            <div className="composer-shell">
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
                  <button className="btn btn-ghost btn-sm btn-icon"><IC.Attach size={15}/></button>
                  <button className="btn btn-ghost btn-sm">
                    <IC.Sparkles size={14} style={{ color: "var(--accent)" }}/> Skills · 1
                  </button>
                  <label className="sr-only" htmlFor="chat-model-select">Model</label>
                  <select
                    id="chat-model-select"
                    aria-label="Model"
                    value={selectedModel}
                    onChange={event => setSelectedModel(event.target.value)}
                    disabled={streaming}
                    className="surface border border-soft rounded-md px-2 py-1.5 text-[12.5px] outline-none disabled:opacity-60"
                    style={{ color: "var(--text)" }}
                    title={chatModels.find(model => model.id === selectedModel)?.model ?? selectedModel}
                  >
                    {(chatModels.length ? chatModels : [{ id: "auto", label: "Auto", model: "auto" }]).map(model => (
                      <option key={model.id} value={model.id}>{model.label}</option>
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
              Chronos won&apos;t send anything outside your workspace without your approval.
            </p>
          </div>
        </div>
      </div>

      {activityOpen && (
        <ActivityDrawer taskId={activeTaskId} onClose={() => setActivityOpen(false)} />
      )}
    </div>
  );
}

function UserMessage({ content }: { content: string }) {
  return (
    <div className="flex gap-4 fadein">
      <div className="avatar-u">A</div>
      <div className="flex-1 min-w-0 pt-0.5">
        <div className="flex items-baseline gap-2 mb-1.5">
          <span className="text-[14px] font-semibold">You</span>
        </div>
        <div className="prose-body" style={{ color: "var(--text)" }}>{content}</div>
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
    setBusy(true);
    const blob = await fetchBlob();
    setBusy(false);
    if (!blob) return;
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener,noreferrer");
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
      <button onClick={handleDownload} disabled={busy} className="btn btn-ghost btn-sm">Download</button>
    </div>
  );
}

function AssistantMessage({ content, status, persona, toolTraces, artifacts }: { content: string; status: MessageStatus; persona: typeof PERSONAS[0]; toolTraces?: ToolTrace[]; artifacts?: ArtifactRef[] }) {
  return (
    <div className="flex gap-4 fadein">
      <PersonaAvatar name={persona.name} color={persona.color} size={28}/>
      <div className="flex-1 min-w-0 pt-0.5">
        <div className="flex items-baseline gap-2 mb-1.5">
          <span className="text-[14px] font-semibold">{persona.name}</span>
          {status === "error" && <Tag variant="danger">Error</Tag>}
        </div>

        {/* Inline tool traces — like Claude's tool use steps */}
        {toolTraces && toolTraces.length > 0 && (
          <div className="mb-3 space-y-1.5">
            {toolTraces.map(trace => <TraceRow key={trace.id} trace={trace} />)}
          </div>
        )}

        {/* Answer */}
        {(content || status === "streaming") && (
          <div className="prose-body" style={{ color: "var(--text)" }}>
            {content}
            {status === "streaming" && !content && toolTraces && toolTraces.length > 0
              ? null  /* traces visible; no extra caret until tokens arrive */
              : status === "streaming" && <span className="caret ml-0.5" style={{ borderLeft: "2px solid var(--text)" }}>&nbsp;</span>}
          </div>
        )}

        {/* Artifacts — openable / downloadable files Chronos produced */}
        {artifacts && artifacts.length > 0 && (
          <div className="mt-3 space-y-2">
            {artifacts.map(a => <ArtifactCard key={a.id} artifact={a} />)}
          </div>
        )}

        {/* Typing wave: streaming, no content yet, no traces */}
        {status === "streaming" && !content && (!toolTraces || toolTraces.length === 0) && (
          <div className="typing-wave mt-2"><span/><span/><span/></div>
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

function ActivityDrawer({ taskId, onClose }: { taskId: string | null; onClose: () => void }) {
  const [task, setTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<TaskStreamEvent[]>([]);
  const [streamError, setStreamError] = useState("");

  useEffect(() => {
    if (!taskId) return;
    const controller = new AbortController();

    async function connect() {
      setEvents([]);
      setStreamError("");
      try {
        const res = await apiFetch(`/tasks/${taskId}/stream`, { signal: controller.signal });
        const reader = res.body?.getReader();
        if (!reader) throw new Error("Task stream did not return a readable body");
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";

          for (const frame of frames) {
            const line = frame.split("\n").find(part => part.startsWith("data: "));
            if (!line) continue;
            const event = JSON.parse(line.slice(6)) as TaskStreamEvent;
            if (event.type === "catch_up" && event.task) setTask(event.task);
            else {
              setEvents(prev => [...prev, event]);
              setTask(prev => mergeTaskEvent(prev, event));
            }
          }
        }
      } catch (exc) {
        if ((exc as Error).name !== "AbortError") {
          setStreamError(exc instanceof Error ? exc.message : "Task stream disconnected");
        }
      }
    }

    void connect();
    return () => controller.abort();
  }, [taskId]);

  const steps = task?.plan ?? [];
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
                  {approvalEvent.approval_ids?.length ?? 0} drafts are waiting in Approvals.
                </p>
              </div>
            ) : null}

            {failureEvent ? (
              <div className="rounded-lg border px-3 py-3" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)" }}>
                <div className="flex items-center gap-2 text-[13px] font-semibold" style={{ color: "var(--danger)" }}>
                  <IC.Info size={14}/> Task failed
                </div>
                <p className="mt-1 text-[12.5px] leading-relaxed" style={{ color: "var(--danger)" }}>{failureEvent.error}</p>
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
                  {event.ts ? <span style={{ color: "var(--text-dim)" }}> · {new Date(event.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</span> : null}
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
  if (event.type === "task_complete") return { ...task, status: "complete", current_step: task.plan?.length ?? task.current_step };
  return task;
}

function taskStatus(task: Task | null, events: TaskStreamEvent[], streamError: string) {
  if (streamError) return { label: "Stream interrupted", dot: "error" };
  if (!task) return { label: "Connecting...", dot: "working" };
  if (task.status === "awaiting_approval" || events.some(event => event.type === "awaiting_approval")) return { label: "Waiting on approval", dot: "awaiting" };
  if (task.status === "failed") return { label: "Stopped", dot: "failed" };
  if (task.status === "complete") return { label: "Complete", dot: "done" };
  if (task.status === "pending" || task.status === "planning") return { label: "Queued", dot: "queued" };
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
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    apiFetch("/tasks/")
      .then(r => r.json())
      .then((data: Task[]) => setTasks(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!activeTaskId) { setActiveTask(null); return; }
    apiFetch(`/tasks/${activeTaskId}`)
      .then(r => r.json())
      .then((data: Task) => setActiveTask(data))
      .catch(() => setActiveTask(null));
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
    running: "Working", awaiting_approval: "Waiting on you", complete: "Done", failed: "Stopped", pending: "Queued",
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
                      {t.status === "pending"             && <Dot color="var(--text-faint)" size={8}/>}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="text-[14.5px] font-medium mb-1 truncate">{t.goal}</div>
                      <div className="flex items-center gap-2 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
                        <span style={{ color: statusColor }}>{sl}</span>
                        <span>·</span>
                        <span>Step {t.current_step}</span>
                      </div>
                    </div>
                    <IC.Chevron size={16} style={{ color: "var(--text-faint)", transform: activeTaskId === t.id ? "rotate(90deg)" : "none" }}/>
                  </button>
                  {activeTaskId === t.id && (
                    <div className="border-t hairline px-5 py-4 space-y-3">
                      {activeTask?.error && <div className="text-[12.5px]" style={{ color: "var(--danger)" }}>{activeTask.error}</div>}
                      <TaskSteps task={activeTask || t}/>
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
        <div className="px-10 pb-10">
          <EmptyState icon={<IC.Audit size={20}/>} title="Action log" sub="Every action Chronos takes is recorded here. Actions appear as jobs run."/>
        </div>
      )}
    </div>
  );
}

function TaskSteps({ task }: { task: Task }) {
  const steps = Array.isArray(task.plan) ? task.plan : [];
  if (!steps.length) return <div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>No execution plan recorded.</div>;
  return <div className="space-y-1.5">{steps.map((step, index) => <div key={step.id || index} className="flex items-center gap-2 text-[12.5px]"><Tag variant={index < task.current_step ? "ok" : index === task.current_step ? "info" : "default"}>{index + 1}</Tag><span className="font-medium">{step.action}</span><span style={{ color: "var(--text-dim)" }}>{step.description}</span>{step.tool && <Tag>{step.tool}</Tag>}</div>)}</div>;
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
          {loading && <p className="px-5 py-4 text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading…</p>}
          {!loading && approvals.length === 0 && (
            <div className="px-5 py-8">
              <EmptyState icon={<IC.Approvals size={20}/>} title="All caught up" sub="Approvals appear here when Chronos needs your go-ahead."/>
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
function MemoryScreen() {
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [adding, setAdding] = useState(false);
  const [newContent, setNewContent] = useState("");

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
        body: JSON.stringify({ content: newContent, scope: "org", scope_id: "default", source: "manual" }),
      });
      await loadMemories();
      setAdding(false);
      setNewContent("");
    } catch { /* silently */ }
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

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <PageHeader
        title="Memory"
        subtitle="What Chronos remembers about you and your workspace. Save anything important."
        right={
          <button onClick={() => setAdding(true)} className="btn btn-secondary btn-sm">
            <IC.Plus size={14}/> Add a memory
          </button>
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
        {adding && (
          <div className="mem-card p-4 fadeup" style={{ borderColor: "var(--accent)" }}>
            <textarea value={newContent} onChange={e => setNewContent(e.target.value)} autoFocus
                      placeholder="Something Chronos should remember…" rows={2}
                      className="w-full bg-transparent outline-none text-[14.5px] resize-none"
                      style={{ color: "var(--text)" }}/>
            <div className="flex items-center justify-between mt-2">
              <span className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>Saved to your workspace.</span>
              <div className="flex items-center gap-2">
                <button onClick={() => { setAdding(false); setNewContent(""); }} className="btn btn-ghost btn-sm">Cancel</button>
                <button onClick={() => void addMemory()} className="btn btn-accent btn-sm">Save memory</button>
              </div>
            </div>
          </div>
        )}

        {loading && <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading…</p>}
        {!loading && filtered.length === 0 && (
          <EmptyState icon={<IC.Memory size={20}/>} title="No memories yet"
                      sub={memories.length === 0 ? "Chronos saves memories automatically during conversations. You can also add them manually." : "No memories match your current filter."}/>
        )}

        {filtered.map(m => <MemoryCard key={m.id} m={m} onDelete={deleteMemory} onUpdate={updateMemory}/>)}
      </div>
    </div>
  );
}

function MemoryCard({ m, onDelete, onUpdate }: { m: MemoryEntry; onDelete: (id: string) => void; onUpdate: (id: string, content: string, importance_score?: number) => Promise<void> }) {
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
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0" style={{ opacity: hover ? 1 : 0, transition: "opacity 0.15s" }}>
          <button onClick={() => setEditing(true)} className="btn btn-ghost btn-sm btn-icon" title="Edit">
            <IC.Pencil size={13}/>
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
function ConnectorsScreen() {
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [actions, setActions] = useState<Record<string, ConnectorAction[]>>({});
  const [logs, setLogs] = useState<ConnectorExecutionLog[]>([]);
  const [health, setHealth] = useState<Record<string, ConnectorHealth>>({});
  const [traces, setTraces] = useState<ConnectorTrace[]>([]);
  const [approvals, setApprovals] = useState<ConnectorApproval[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");

  async function load() {
    setLoading(true);
    try {
      const data = (await (await apiFetch("/connectors/")).json()) as Connector[];
      setConnectors(data);
      const actionEntries = await Promise.all(data.map(async c => [c.id, await (await apiFetch(`/connectors/${c.id}/actions`)).json()] as const));
      setActions(Object.fromEntries(actionEntries));
      setLogs((await (await apiFetch("/connectors/execution-logs")).json()) as ConnectorExecutionLog[]);
      const healthRows = (await (await apiFetch("/connectors/health")).json()) as ConnectorHealth[];
      setHealth(Object.fromEntries(healthRows.map(row => [row.connector_id, row])));
      setTraces((await (await apiFetch("/connectors/execution-traces")).json()) as ConnectorTrace[]);
      setApprovals((await (await apiFetch("/connectors/approvals?limit=20")).json()) as ConnectorApproval[]);
    } catch {
      setMessage("Unable to load connectors.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  async function install(connector: Connector) {
    setBusy(connector.id);
    setMessage("");
    try {
      const res = await apiFetch(`/connectors/${connector.id}/install`, { method: "POST", body: JSON.stringify({ workspace_id: "default" }) });
      if (!res.ok) throw new Error(await res.text());
      setMessage(`${connector.name || connector.id} installed.`);
      await load();
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : "Install failed.");
    } finally {
      setBusy(null);
    }
  }

  async function disable(connector: Connector) {
    setBusy(connector.id);
    setMessage("");
    try {
      const res = await apiFetch(`/connectors/${connector.id}/disable`, { method: "POST" });
      if (!res.ok) throw new Error(await res.text());
      setMessage(`${connector.name || connector.id} disabled.`);
      await load();
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : "Disable failed.");
    } finally {
      setBusy(null);
    }
  }

  async function runAction(connector: Connector, action: ConnectorAction) {
    setBusy(`${connector.id}:${action.name}`);
    setMessage("");
    const args = connector.id === "internal_echo" ? { message: "Connector execution proof" } : {};
    try {
      const res = await apiFetch(`/connectors/${connector.id}/actions/${action.name}/execute`, {
        method: "POST",
        body: JSON.stringify({ workspace_id: "default", arguments: args }),
      });
      const payload = await res.json();
      if (!res.ok || !["queued", "success"].includes(payload.status)) throw new Error(payload.error || JSON.stringify(payload));
      setMessage(`${connector.name || connector.id}.${action.name} ${payload.status === "queued" ? "queued for isolated worker execution" : "executed"}.`);
      await load();
    } catch (exc) {
      setMessage(exc instanceof Error ? exc.message : "Execution failed.");
      await load();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <PageHeader title="Connectors" subtitle="Registry-backed connector actions available to Chronos."/>

      <div className="px-10 pb-10 space-y-6">
        {message && <div className="surface border border-soft rounded-lg px-4 py-3 text-[13px]" style={{ color: "var(--text-muted)" }}>{message}</div>}
        {loading && <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading…</p>}
        {!loading && connectors.length === 0 && <EmptyState icon={<IC.Connectors size={20}/>} title="No connectors registered" sub="The backend registry has not returned any executable connectors."/>}
        <div className="grid grid-cols-2 gap-3">
          {connectors.map(connector => (
            <div key={connector.id} className="surface border border-soft rounded-xl p-5">
              <div className="flex items-start justify-between gap-4 mb-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <h2 className="text-[15.5px] font-semibold">{connector.name || connector.id}</h2>
                    <Tag variant={connector.status === "installed" ? "ok" : connector.status === "disabled" ? "danger" : "info"}>{connector.status}</Tag>
                    {health[connector.id] && <Tag variant={health[connector.id].status === "healthy" ? "ok" : "warn"}>{health[connector.id].status}</Tag>}
                  </div>
                  <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>{connector.description}</p>
                  <div className="mt-2 flex flex-wrap gap-1">
                    <Tag>{connector.category || "Internal"}</Tag>
                    <Tag>{connector.type || "native"}</Tag>
                    <Tag>{connector.auth_type || "none"}</Tag>
                    {(connector.scopes || []).map(scope => <Tag key={scope}>{scope}</Tag>)}
                  </div>
                </div>
                <div className="flex gap-2">
                  {connector.status !== "installed" && <button disabled={busy === connector.id} onClick={() => void install(connector)} className="btn btn-accent btn-sm disabled:opacity-50">Install</button>}
                  {connector.status === "installed" && <button disabled={busy === connector.id} onClick={() => void disable(connector)} className="btn btn-danger-soft btn-sm disabled:opacity-50">Disable</button>}
                </div>
              </div>
              <div className="space-y-2">
                {(actions[connector.id] || []).map(action => (
                  <div key={action.name} className="border-t hairline pt-3">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-medium text-[13.5px]">{action.name}</div>
                        <div className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>{action.description}</div>
                        <div className="mt-2 flex flex-wrap gap-1">
                          <Tag variant={action.risk_level === "read" ? "info" : "warn"}>{action.risk_level}</Tag>
                          {(action.approval_required || ["write", "destructive", "financial", "external_message"].includes(action.risk_level)) && <Tag variant="warn">approval checkpoint</Tag>}
                          {action.required_permissions.map(permission => <Tag key={permission}>{permission}</Tag>)}
                        </div>
                      </div>
                      <button disabled={connector.status !== "installed" || busy === `${connector.id}:${action.name}`} onClick={() => void runAction(connector, action)} className="btn btn-secondary btn-sm disabled:opacity-50">Run</button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>

        <section>
          <h2 className="text-[13px] uppercase tracking-wider mb-3" style={{ color: "var(--text-dim)" }}>Execution logs</h2>
          <div className="surface border border-soft rounded-xl overflow-hidden">
            {logs.length === 0 && <div className="p-5"><EmptyState title="No executions yet" sub="Run an installed connector action to create an execution log."/></div>}
            {logs.map(log => <div key={log.id} className="px-4 py-3 border-b hairline last:border-b-0">
              <div className="flex items-center justify-between gap-3">
                <div className="font-medium text-[13.5px]">{log.connector_id}.{log.action_name}</div>
                <Tag variant={log.result_status === "success" ? "ok" : "danger"}>{log.result_status}</Tag>
              </div>
              <div className="text-[12px] mt-1" style={{ color: "var(--text-dim)" }}>{log.created_at ? new Date(log.created_at).toLocaleString() : "unknown time"} · {log.duration_ms}ms</div>
              {log.error_message && <div className="text-[12px] mt-1" style={{ color: "var(--danger)" }}>{log.error_message}</div>}
              <details className="mt-2 text-[12px]" style={{ color: "var(--text-dim)" }}>
                <summary className="cursor-pointer">Redacted arguments</summary>
                <pre className="mt-2 overflow-x-auto rounded-md p-2" style={{ background: "var(--surface-2)" }}>{JSON.stringify(log.arguments_redacted ?? {}, null, 2)}</pre>
              </details>
            </div>)}
          </div>
        </section>

        <section className="grid grid-cols-2 gap-3">
          <div>
            <h2 className="text-[13px] uppercase tracking-wider mb-3" style={{ color: "var(--text-dim)" }}>Approval queue</h2>
            <div className="surface border border-soft rounded-xl overflow-hidden">
              {approvals.length === 0 && <div className="p-5"><EmptyState title="No connector approvals" sub="Risky connector actions create approval requests before execution."/></div>}
              {approvals.map(approval => <div key={approval.id} className="px-4 py-3 border-b hairline last:border-b-0">
                <div className="flex items-center justify-between gap-3">
                  <div className="font-medium text-[13.5px]">{approval.connector_id}.{approval.action_name}</div>
                  <Tag variant={approval.status === "pending" ? "warn" : approval.status === "approved" ? "ok" : "danger"}>{approval.status}</Tag>
                </div>
                <div className="text-[12px] mt-1" style={{ color: "var(--text-dim)" }}>{approval.risk_level} · {approval.approval_mode}</div>
              </div>)}
            </div>
          </div>

          <div>
            <h2 className="text-[13px] uppercase tracking-wider mb-3" style={{ color: "var(--text-dim)" }}>Execution traces</h2>
            <div className="surface border border-soft rounded-xl overflow-hidden">
              {traces.length === 0 && <div className="p-5"><EmptyState title="No traces yet" sub="The connector worker records traces when it executes queued jobs."/></div>}
              {traces.slice(0, 8).map(trace => <div key={trace.id} className="px-4 py-3 border-b hairline last:border-b-0">
                <div className="flex items-center justify-between gap-3">
                  <div className="font-medium text-[13.5px]">{trace.connector_id}.{trace.action_name}</div>
                  <Tag variant={trace.status === "success" ? "ok" : trace.status === "running" ? "info" : "danger"}>{trace.status}</Tag>
                </div>
                <div className="text-[12px] mt-1" style={{ color: "var(--text-dim)" }}>{trace.started_at ? new Date(trace.started_at).toLocaleString() : "unknown time"}</div>
              </div>)}
            </div>
          </div>
        </section>
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
  return <><SettingsSection title="Memory policy" note={`${stats.active} active memories, ${stats.deleted} deleted.`}><SettingsField label="Workspace memory"><Toggle label="Workspace memory" checked={Boolean(data.workspace_memory)} onChange={workspace_memory => patch({ workspace_memory })}/></SettingsField><SettingsField label="Employee memory"><Toggle label="Employee memory" checked={Boolean(data.employee_memory)} onChange={employee_memory => patch({ employee_memory })}/></SettingsField><SettingsField label="User memory"><Toggle label="User memory" checked={Boolean(data.user_memory)} onChange={user_memory => patch({ user_memory })}/></SettingsField><SettingsField label="Retention days"><TextInput ariaLabel="Memory retention" value={val(data, "retention_days")} onChange={retention_days => patch({ retention_days: Number(retention_days) || 0 })}/></SettingsField><SettingsField label="Review required"><Toggle label="Memory review required" checked={Boolean(data.review_required)} onChange={review_required => patch({ review_required })}/></SettingsField><SettingsField label="Auto-save memory"><Toggle label="Auto-save memory" checked={Boolean(data.auto_save)} onChange={auto_save => patch({ auto_save })}/></SettingsField><SettingsField label="Sensitive detection"><Toggle label="Sensitive memory detection" checked={Boolean(data.sensitive_detection)} onChange={sensitive_detection => patch({ sensitive_detection })}/></SettingsField></SettingsSection><SettingsSection title="Memory danger zone"><SettingsField label="Export memory"><button className="btn btn-sm" disabled title="Memory export endpoint is not implemented">Export unavailable</button></SettingsField><SettingsField label="Purge all memory" hint="Requires typed confirmation and writes an audit entry."><button className="btn btn-danger-soft btn-sm" onClick={() => setConfirm({ title: "Purge memory", text: "This soft-deletes all active memory entries in this workspace.", required: "PURGE MEMORY", action: async () => purge() })}>Purge memory</button></SettingsField></SettingsSection></>;
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
