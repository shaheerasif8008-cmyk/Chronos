"use client";

import { ReactNode, useEffect, useRef, useState, useMemo, useCallback } from "react";
import { useRouter } from "next/navigation";

// ─── Config ──────────────────────────────────────────────────────────────────
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// ─── Types ───────────────────────────────────────────────────────────────────
type Route = "chat" | "activity" | "approvals" | "memory" | "connectors" | "assistants" | "settings";
type SettingsTab = "account" | "preferences" | "workspace" | "notifications" | "audit";
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
};
type ToolTrace = { id: string; tool: string; summary: string; status: MessageStatus };
type MemoryEntry = { id: string; scope: string; scope_id: string; content: string; source: string; created_by?: string | null; created_at?: string };
type Connector = { id: string; provider: string; account_handle?: string | null; status: string; connected_at?: string | null; last_used_at?: string | null };
type Task = { id: string; status: string; goal: string; current_step: number; plan?: TaskStep[]; result?: Record<string, unknown>; created_at?: string; parent_task_id?: string | null; depth?: number };
type TaskStep = { id: string; action: string; description: string; tool?: string | null };
type Approval = { id: string; task_id: string; step_id: string; action_type: string; action_payload: Record<string, unknown>; requested_at?: string; status: string };

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
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
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

// ─── Root App ─────────────────────────────────────────────────────────────────
export default function ChronosApp() {
  const router = useRouter();
  const [route, setRoute] = useState<Route>("chat");
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("account");
  const [activeConvoId, setActiveConvoId] = useState<string | null>(null);
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

  async function loadConversations(selectId?: string) {
    try {
      const data = (await (await apiFetch("/chat/conversations")).json()) as Conversation[];
      setConversations(data);
      if (selectId) setActiveConvoId(selectId);
      else if (!activeConvoId && data[0]) setActiveConvoId(data[0].id);
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
    setRoute("settings");
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
        onNavigate={(r: Route) => setRoute(r)}
        conversations={conversations}
        activeConvoId={activeConvoId}
        onSelectConvo={(id) => { setActiveConvoId(id); setRoute("chat"); }}
        onNewConvo={() => { setActiveConvoId(null); setRoute("chat"); }}
        onDeleteConvo={deleteConversation}
        pendingApprovals={pendingApprovals}
        onOpenSettings={openSettings}
        onSignOut={signOut}
      />

      <main className="flex-1 min-w-0 flex flex-col" style={{ background: "var(--bg)" }}>
        {route === "chat"       && <ChatScreen activeConvoId={activeConvoId} onConvoCreated={(id) => loadConversations(id)} />}
        {route === "activity"   && <ActivityScreen />}
        {route === "approvals"  && <ApprovalsScreen onDecision={loadPendingApprovals} />}
        {route === "memory"     && <MemoryScreen />}
        {route === "connectors" && <ConnectorsScreen />}
        {route === "assistants" && <AssistantsScreen />}
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
        { label: "Account",            icon: <IC.Personas size={14}/>, tab: "account" as SettingsTab },
        { label: "Preferences",        icon: <IC.Settings size={14}/>, tab: "preferences" as SettingsTab },
        { label: "Workspace settings", icon: <IC.Briefcase size={14}/>, tab: "workspace" as SettingsTab },
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
          <span>Chronos · Sprint 1</span>
          <span className="flex items-center gap-1"><Dot color="var(--ok)" size={5}/> Healthy</span>
        </div>
      </div>
    </div>
  );
}

// ─── Chat Screen ──────────────────────────────────────────────────────────────
function ChatScreen({ activeConvoId, onConvoCreated }: { activeConvoId: string | null; onConvoCreated: (id: string) => void }) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const isEmpty = !activeConvoId;

  const activePersona = PERSONAS[0];

  useEffect(() => {
    if (!activeConvoId) { setMessages([]); return; }
    apiFetch(`/chat/conversations/${activeConvoId}/messages`)
      .then(r => r.json())
      .then((data: Message[]) => setMessages(data))
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
      if (!convoId) {
        const created = await (await apiFetch("/chat/conversations", { method: "POST", body: JSON.stringify({ title: text.slice(0, 60) }) })).json() as { id: string };
        convoId = created.id;
        onConvoCreated(convoId);
      }

      const resp = await apiFetch(`/chat/conversations/${convoId}/messages`, {
        method: "POST",
        body: JSON.stringify({ content: text }),
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
            const ev = JSON.parse(line.slice(6)) as { type: string; content?: string };
            if (ev.type === "token" && ev.content) {
              partial += ev.content;
              setMessages(prev => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") updated[updated.length - 1] = { ...last, content: partial };
                return updated;
              });
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
                  : <AssistantMessage key={i} content={m.content} status={m.status ?? "complete"} persona={activePersona}/>
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
        <ActivityDrawer onClose={() => setActivityOpen(false)} />
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

function AssistantMessage({ content, status, persona }: { content: string; status: MessageStatus; persona: typeof PERSONAS[0] }) {
  return (
    <div className="flex gap-4 fadein">
      <PersonaAvatar name={persona.name} color={persona.color} size={28}/>
      <div className="flex-1 min-w-0 pt-0.5">
        <div className="flex items-baseline gap-2 mb-1.5">
          <span className="text-[14px] font-semibold">{persona.name}</span>
          {status === "error" && <Tag variant="danger">Error</Tag>}
        </div>
        <div className="prose-body" style={{ color: "var(--text)" }}>
          {content}
          {status === "streaming" && <span className="caret ml-0.5" style={{ borderLeft: "2px solid var(--text)" }}>&nbsp;</span>}
        </div>
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

function ActivityDrawer({ onClose }: { onClose: () => void }) {
  return (
    <aside className="flex-shrink-0 flex flex-col border-l hairline slidein" style={{ width: 400, background: "var(--bg)" }}>
      <div className="px-5 h-[52px] flex items-center justify-between border-b hairline">
        <div className="flex items-center gap-2.5">
          <StatusDot status="working"/>
          <span className="text-[14px] font-semibold">Working…</span>
        </div>
        <button onClick={onClose} className="btn btn-ghost btn-icon"><IC.X size={16}/></button>
      </div>
      <div className="flex-1 flex flex-col items-center justify-center p-6" style={{ color: "var(--text-dim)" }}>
        <div className="typing-wave mb-3"><span/><span/><span/></div>
        <p className="text-[13px]">Chronos is working on your request…</p>
      </div>
    </aside>
  );
}

// ─── Activity Screen ──────────────────────────────────────────────────────────
function ActivityScreen() {
  const [mode, setMode] = useState<"jobs" | "actions">("jobs");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    apiFetch("/tasks/")
      .then(r => r.json())
      .then((data: Task[]) => setTasks(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

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
                <div key={t.id} className="surface border border-soft rounded-lg p-4 smooth hover:border-[var(--border)] cursor-pointer flex items-center gap-4">
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
                  <IC.Chevron size={16} style={{ color: "var(--text-faint)" }}/>
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

// ─── Approvals Screen ─────────────────────────────────────────────────────────
function ApprovalsScreen({ onDecision }: { onDecision: () => void }) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Record<string, "approved" | "rejected">>({});

  useEffect(() => {
    setLoading(true);
    apiFetch("/approvals/")
      .then(r => r.json())
      .then((data: Approval[]) => {
        setApprovals(data);
        if (data[0]) setActiveId(data[0].id);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const active = approvals.find(a => a.id === activeId);

  async function decide(id: string, decision: "approved" | "rejected") {
    try {
      await apiFetch(`/approvals/${id}`, { method: "POST", body: JSON.stringify({ decision }) });
      setDecisions(prev => ({ ...prev, [id]: decision }));
      onDecision();
    } catch { /* silently */ }
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
            {pending.length} waiting · auto-expire in 24h
          </p>
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
                <button onClick={() => void decide(active.id, "approved")} className="btn btn-ok-soft flex-1 justify-center">
                  <IC.Check size={15} stroke={2.2}/> Approve
                </button>
                <button onClick={() => void decide(active.id, "rejected")} className="btn btn-danger-soft flex-1 justify-center">
                  <IC.X size={15}/> Reject
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

  useEffect(() => {
    setLoading(true);
    apiFetch("/memory/")
      .then(r => r.json())
      .then((data: MemoryEntry[]) => setMemories(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

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
      const entry = await (await apiFetch("/memory/", {
        method: "POST",
        body: JSON.stringify({ content: newContent, scope: "org", scope_id: "default", source: "manual" }),
      })).json() as MemoryEntry;
      setMemories(prev => [entry, ...prev]);
      setAdding(false);
      setNewContent("");
    } catch { /* silently */ }
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

        {filtered.map(m => <MemoryCard key={m.id} m={m} onDelete={deleteMemory}/>)}
      </div>
    </div>
  );
}

function MemoryCard({ m, onDelete }: { m: MemoryEntry; onDelete: (id: string) => void }) {
  const [hover, setHover] = useState(false);
  const isPrivate = m.scope === "restricted";
  const isAuto = m.source === "autonomous";

  return (
    <div className="mem-card p-4" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 mt-0.5"
             style={{ background: isPrivate ? "var(--danger-soft)" : "var(--surface-2)", color: isPrivate ? "var(--danger)" : "var(--text-dim)" }}>
          {isPrivate ? <IC.Lock size={13}/> : <IC.Memory size={13}/>}
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-[14.5px] leading-relaxed" style={{ color: "var(--text)" }}>{m.content}</div>
          <div className="flex items-center gap-2.5 mt-2 text-[12px]" style={{ color: "var(--text-dim)" }}>
            <span className="inline-flex items-center gap-1">
              {isAuto ? <><IC.Sparkles size={11}/> Saved by Chronos</> : <><IC.Pencil size={11}/> Saved by you</>}
            </span>
            {m.scope && <><span>·</span><span>{m.scope}</span></>}
            {m.created_by && <><span>·</span><span>by {m.created_by}</span></>}
          </div>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0" style={{ opacity: hover ? 1 : 0, transition: "opacity 0.15s" }}>
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
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiFetch("/connectors/")
      .then(r => r.json())
      .then((data: Connector[]) => setConnectors(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const meta: Record<string, { icon: ReactNode; bg: string; color: string; label: string; note: string }> = {
    gmail:    { icon: <IC.Mail size={20}/>,       bg: "oklch(0.95 0.04 25)",  color: "oklch(0.50 0.18 25)",  label: "Gmail",    note: "Read, draft, and send email (sending always asks you first)." },
    browser:  { icon: <IC.Globe size={20}/>,      bg: "oklch(0.95 0.04 240)", color: "oklch(0.50 0.16 240)", label: "Web",      note: "A sandboxed browser that captures screenshots of every page it visits." },
    calendar: { icon: <IC.Clock size={20}/>,      bg: "oklch(0.95 0.04 150)", color: "oklch(0.50 0.16 150)", label: "Calendar", note: "Schedule meetings, find time, send invites." },
    hubspot:  { icon: <IC.Briefcase size={20}/>,  bg: "oklch(0.95 0.04 50)",  color: "oklch(0.50 0.18 50)",  label: "HubSpot",  note: "Read and write to your CRM." },
    slack:    { icon: <IC.Chat size={20}/>,        bg: "oklch(0.95 0.04 320)", color: "oklch(0.50 0.16 320)", label: "Slack",    note: "Read mentions, post messages (with approval)." },
    drive:    { icon: <IC.Folder size={20}/>,     bg: "oklch(0.95 0.04 100)", color: "oklch(0.50 0.16 100)", label: "Drive",    note: "Read your shared drive, create documents." },
  };

  const connected = connectors.filter(c => c.status === "active" || c.status === "connected");
  const available = ["calendar", "hubspot", "slack", "drive"].filter(p => !connectors.find(c => c.provider === p && (c.status === "active" || c.status === "connected")));

  return (
    <div className="flex-1 flex flex-col min-w-0 overflow-y-auto">
      <PageHeader title="Connectors" subtitle="The apps Chronos can use on your behalf. Connect more anytime."/>

      <div className="px-10 pb-6">
        <h2 className="text-[13px] uppercase tracking-wider mb-3" style={{ color: "var(--text-dim)" }}>Connected</h2>
        {loading && <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading…</p>}
        {!loading && connected.length === 0 && (
          <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>No connectors connected yet.</p>
        )}
        <div className="grid grid-cols-2 gap-3">
          {connected.map(c => {
            const m = meta[c.provider] ?? { icon: <IC.Connectors size={20}/>, bg: "var(--surface-2)", color: "var(--text-muted)", label: c.provider, note: "" };
            return (
              <div key={c.id} className="surface border border-soft rounded-xl p-5 smooth hover:border-[var(--border)]">
                <div className="flex items-start gap-4">
                  <div className="w-12 h-12 rounded-lg flex items-center justify-center flex-shrink-0"
                       style={{ background: m.bg, color: m.color }}>{m.icon}</div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <h3 className="text-[15.5px] font-semibold">{m.label}</h3>
                      <Tag variant="ok"><Dot color="var(--ok)" size={6}/> Connected</Tag>
                    </div>
                    {c.account_handle && (
                      <div className="text-[12.5px] mb-2 font-mono" style={{ color: "var(--text-muted)" }}>{c.account_handle}</div>
                    )}
                    <p className="text-[13px] mb-3" style={{ color: "var(--text-muted)" }}>{m.note}</p>
                    <div className="flex items-baseline justify-between">
                      <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>
                        {c.last_used_at ? `Used ${new Date(c.last_used_at).toLocaleDateString()}` : "Never used"}
                      </span>
                      <button className="btn btn-ghost btn-sm">Manage</button>
                    </div>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <div className="px-10 pb-10">
        <h2 className="text-[13px] uppercase tracking-wider mb-3" style={{ color: "var(--text-dim)" }}>Available</h2>
        <div className="grid grid-cols-3 gap-3 mb-8">
          {available.map(provider => {
            const m = meta[provider] ?? { icon: <IC.Connectors size={18}/>, bg: "var(--surface-2)", color: "var(--text-muted)", label: provider, note: "" };
            return (
              <div key={provider} className="surface border border-soft rounded-xl p-4 smooth hover:border-[var(--border)]">
                <div className="flex items-start gap-3">
                  <div className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
                       style={{ background: m.bg, color: m.color, opacity: 0.7 }}>{m.icon}</div>
                  <div className="flex-1 min-w-0">
                    <h3 className="text-[14.5px] font-semibold mb-1">{m.label}</h3>
                    <p className="text-[12.5px] mb-3" style={{ color: "var(--text-muted)" }}>{m.note}</p>
                    <button className="btn btn-secondary btn-sm w-full justify-center">Connect</button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="surface border border-soft rounded-lg p-5 max-w-[720px]">
          <div className="flex items-center gap-2 mb-2">
            <IC.Lock size={15} style={{ color: "var(--text-muted)" }}/>
            <h3 className="text-[14px] font-semibold">How permissions work</h3>
          </div>
          <p className="text-[13.5px] leading-relaxed" style={{ color: "var(--text-muted)" }}>
            Chronos always asks before sending email, posting on your behalf, or moving money. Connecting an app gives Chronos read access by default — anything more is one approval at a time.
          </p>
        </div>
      </div>
    </div>
  );
}

// ─── Assistants Screen ────────────────────────────────────────────────────────
function AssistantsScreen() {
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
            <button className="btn btn-accent btn-sm">Start a conversation</button>
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
function SettingsScreen({ tab, setTab, theme, setTheme, accent, setAccent, signOut }: {
  tab: SettingsTab; setTab: (t: SettingsTab) => void;
  theme: "light" | "dark"; setTheme: (t: "light" | "dark") => void;
  accent: string; setAccent: (a: string) => void;
  signOut: () => void;
}) {
  const tabs: Array<{ id: SettingsTab; label: string; icon: ReactNode }> = [
    { id: "account",       label: "Account",         icon: <IC.Personas size={15}/> },
    { id: "preferences",   label: "Preferences",     icon: <IC.Settings size={15}/> },
    { id: "workspace",     label: "Workspace",       icon: <IC.Briefcase size={15}/> },
    { id: "notifications", label: "Notifications",   icon: <IC.Bell size={15}/> },
    { id: "audit",         label: "Audit log",       icon: <IC.Audit size={15}/> },
  ];

  return (
    <div className="flex-1 flex min-w-0 overflow-hidden">
      {/* Settings sidebar */}
      <div className="flex-shrink-0 border-r hairline py-6" style={{ width: 220, background: "var(--bg-deep)" }}>
        <div className="px-4 mb-4">
          <h2 className="text-[13px] font-semibold uppercase tracking-wider" style={{ color: "var(--text-dim)" }}>Settings</h2>
        </div>
        <div className="px-3 space-y-0.5">
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
                    className={`nav-item w-full ${tab === t.id ? "active" : ""}`}>
              <span className="nav-icon">{t.icon}</span>
              <span className="flex-1 text-left">{t.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Settings content */}
      <div className="flex-1 overflow-y-auto px-10 py-9">
        {tab === "account" && <AccountSettings signOut={signOut}/>}
        {tab === "preferences" && <PreferencesSettings theme={theme} setTheme={setTheme} accent={accent} setAccent={setAccent}/>}
        {tab === "workspace" && <WorkspaceSettings/>}
        {tab === "notifications" && <NotificationsSettings/>}
        {tab === "audit" && <AuditSettings/>}
      </div>
    </div>
  );
}

function SettingsSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="mb-8">
      <h2 className="text-[16px] font-semibold mb-4">{title}</h2>
      {children}
    </div>
  );
}

function SettingsField({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-6 py-4 border-b hairline">
      <div className="min-w-0">
        <div className="text-[14px] font-medium">{label}</div>
        {hint && <div className="text-[13px] mt-0.5" style={{ color: "var(--text-dim)" }}>{hint}</div>}
      </div>
      <div className="flex-shrink-0">{children}</div>
    </div>
  );
}

function AccountSettings({ signOut }: { signOut: () => void }) {
  return (
    <div className="max-w-[640px]">
      <h1 className="h-page mb-6">Account</h1>
      <SettingsSection title="Profile">
        <SettingsField label="Name"><input defaultValue="Admin" className="surface border border-soft rounded-lg px-3 py-2 text-[14px] outline-none w-56" style={{ color: "var(--text)" }}/></SettingsField>
        <SettingsField label="Email" hint="Used for OTP login"><input defaultValue="admin@example.com" className="surface border border-soft rounded-lg px-3 py-2 text-[14px] outline-none w-56" style={{ color: "var(--text)" }}/></SettingsField>
        <SettingsField label="Role"><Tag>Owner</Tag></SettingsField>
      </SettingsSection>
      <SettingsSection title="Danger zone">
        <div className="surface border rounded-lg p-4" style={{ borderColor: "var(--danger)" }}>
          <h3 className="text-[14px] font-semibold mb-1" style={{ color: "var(--danger)" }}>Sign out</h3>
          <p className="text-[13px] mb-3" style={{ color: "var(--text-dim)" }}>Ends your current session.</p>
          <button onClick={signOut} className="btn btn-danger-soft btn-sm">Sign out</button>
        </div>
      </SettingsSection>
    </div>
  );
}

function PreferencesSettings({ theme, setTheme, accent, setAccent }: { theme: "light" | "dark"; setTheme: (t: "light" | "dark") => void; accent: string; setAccent: (a: string) => void }) {
  return (
    <div className="max-w-[640px]">
      <h1 className="h-page mb-6">Preferences</h1>
      <SettingsSection title="Appearance">
        <SettingsField label="Theme" hint="Light is the default.">
          <div className="flex items-center gap-1 surface border border-soft rounded-lg p-1">
            {(["light", "dark"] as const).map(t => (
              <button key={t} onClick={() => setTheme(t)}
                      className="px-3 py-1 rounded-md text-[13px] font-medium smooth capitalize"
                      style={{ background: theme === t ? "var(--surface-2)" : "transparent", color: theme === t ? "var(--text)" : "var(--text-muted)" }}>
                {t}
              </button>
            ))}
          </div>
        </SettingsField>
        <SettingsField label="Accent color">
          <div className="flex items-center gap-2">
            {Object.entries(ACCENT_PALETTES).map(([key, p]) => (
              <button key={key} onClick={() => setAccent(key)} title={key}
                      className="w-7 h-7 rounded-full smooth"
                      style={{ background: p.accent, boxShadow: accent === key ? `0 0 0 2px var(--bg), 0 0 0 4px ${p.accent}` : "none" }}/>
            ))}
          </div>
        </SettingsField>
      </SettingsSection>
    </div>
  );
}

function WorkspaceSettings() {
  return (
    <div className="max-w-[640px]">
      <h1 className="h-page mb-6">Workspace</h1>
      <SettingsSection title="Organization">
        <SettingsField label="Name"><input defaultValue="My Workspace" className="surface border border-soft rounded-lg px-3 py-2 text-[14px] outline-none w-56" style={{ color: "var(--text)" }}/></SettingsField>
        <SettingsField label="Region"><Tag>us-east-1</Tag></SettingsField>
        <SettingsField label="Plan"><Tag variant="accent">Trial</Tag></SettingsField>
      </SettingsSection>
    </div>
  );
}

function NotificationsSettings() {
  const [emailNotifs, setEmailNotifs] = useState(true);
  return (
    <div className="max-w-[640px]">
      <h1 className="h-page mb-6">Notifications</h1>
      <SettingsSection title="Email">
        <SettingsField label="Approval requests" hint="Email when Chronos needs your go-ahead.">
          <button onClick={() => setEmailNotifs(v => !v)}
                  className="rounded-full smooth"
                  style={{ width: 40, height: 24, background: emailNotifs ? "var(--accent)" : "var(--border)", position: "relative" }}>
            <div className="absolute top-1 rounded-full bg-white smooth"
                 style={{ width: 16, height: 16, left: emailNotifs ? 22 : 2, boxShadow: "0 1px 2px rgba(0,0,0,0.2)" }}/>
          </button>
        </SettingsField>
      </SettingsSection>
    </div>
  );
}

function AuditSettings() {
  const [audit, setAudit] = useState<Array<{ id: string; event_type: string; actor_id?: string; action: string; created_at?: string }>>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiFetch("/audit/")
      .then(r => r.json())
      .then((data: typeof audit) => setAudit(data))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="max-w-[800px]">
      <h1 className="h-page mb-2">Audit log</h1>
      <p className="text-[14px] mb-6" style={{ color: "var(--text-dim)" }}>Append-only record of every action in this workspace.</p>

      {loading && <p className="text-[13.5px]" style={{ color: "var(--text-dim)" }}>Loading…</p>}
      {!loading && audit.length === 0 && (
        <EmptyState icon={<IC.Audit size={20}/>} title="No audit entries yet" sub="Entries appear as Chronos takes actions."/>
      )}
      {audit.length > 0 && (
        <div className="surface border border-soft rounded-lg overflow-hidden">
          {audit.slice(0, 50).map((entry, i) => (
            <div key={entry.id} className={`px-4 py-3 flex items-start gap-3 ${i < audit.length - 1 ? "border-b hairline" : ""}`}>
              <div className="w-7 h-7 rounded-md flex items-center justify-center flex-shrink-0 mt-0.5"
                   style={{ background: "var(--surface-2)", color: "var(--text-muted)" }}>
                <IC.Audit size={13}/>
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[13.5px]" style={{ color: "var(--text)" }}>
                  <span className="font-medium">{entry.actor_id ?? "system"}</span> · {entry.action}
                </div>
                <div className="text-[12px] mt-0.5 font-mono" style={{ color: "var(--text-dim)" }}>
                  {entry.event_type} · {entry.created_at ? new Date(entry.created_at).toLocaleString() : "—"}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
