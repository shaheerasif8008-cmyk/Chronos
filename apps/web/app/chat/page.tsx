"use client";

import { FormEvent, ReactNode, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type Route = "chat" | "activity" | "approvals" | "memory" | "connectors" | "assistants" | "settings";
type ActivityMode = "jobs" | "actions";
type SettingsTab = "account" | "preferences" | "workspace" | "notifications" | "audit";
type Conversation = { id: string; title: string | null; updated_at?: string; created_at?: string };
type Message = { id?: string; role: "user" | "assistant" | "system" | "tool"; content: string };
type MemoryEntry = { id: string; scope: string; scope_id: string; content: string; source: string; created_by?: string | null };

const ORG = {
  name: "Chronos workspace",
  member: { name: "Admin", email: "admin@example.com", role: "Owner", initials: "AD" },
};

function getToken() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("chronos_token") ?? "";
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
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

function Icon({ name, className = "h-4 w-4" }: { name: string; className?: string }) {
  const paths: Record<string, ReactNode> = {
    logo: <><circle cx="12" cy="12" r="8" /><path d="M12 7v5l3 2" /></>,
    plus: <><path d="M12 5v14M5 12h14" /></>,
    activity: <path d="M4 12h4l2-6 4 12 2-6h4" />,
    approvals: <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M4 9h16" /></>,
    memory: <><path d="M7 10c0-4 3-7 7-7s7 3 7 7v7a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3" /><path d="M10 10h6M10 14h5" /></>,
    connectors: <><path d="M8 3v5M16 3v5M6 8h12v3a6 6 0 0 1-12 0z" /><path d="M12 17v4" /></>,
    assistants: <><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4 3.5-7 8-7s8 3 8 7" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1" /></>,
    send: <path d="M4 12 20 5l-6 16-3-7z" />,
    edit: <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />,
    trash: <><path d="M3 6h18" /><path d="M8 6V4h8v2M6 6l1 15h10l1-15" /></>,
  };
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function Avatar({ label, color = "#d37a36" }: { label: string; color?: string }) {
  return <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold text-white" style={{ background: color }}>{label.slice(0, 1)}</span>;
}

function Pill({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "ok" | "warn" }) {
  const toneClass = {
    neutral: "bg-stone-100 text-stone-600 dark:bg-stone-700 dark:text-stone-200",
    ok: "bg-emerald-50 text-emerald-700",
    warn: "bg-amber-50 text-amber-700",
  }[tone];
  return <span className={`inline-flex items-center rounded-md px-2 py-1 text-xs font-medium ${toneClass}`}>{children}</span>;
}

export default function ChronosApp() {
  const router = useRouter();
  const [route, setRoute] = useState<Route>("chat");
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("account");
  const [activeConversation, setActiveConversation] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [accountOpen, setAccountOpen] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [error, setError] = useState("");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    void refreshConversations();
  }, [router]);

  async function refreshConversations(selectedId?: string) {
    setError("");
    try {
      const data = (await (await apiFetch("/chat/conversations")).json()) as Conversation[];
      setConversations(data);
      if (selectedId) setActiveConversation(selectedId);
      else if (!activeConversation && data[0]) setActiveConversation(data[0].id);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to load conversations");
    }
  }

  function openSettings(tab: SettingsTab) {
    setSettingsTab(tab);
    setRoute("settings");
    setAccountOpen(false);
  }

  function signOut() {
    localStorage.removeItem("chronos_token");
    router.replace("/login");
  }

  const nav: Array<[Route, string, string, number]> = [
    ["activity", "Activity", "activity", 0],
    ["approvals", "Approvals", "approvals", 0],
    ["memory", "Memory", "memory", 0],
    ["connectors", "Connectors", "connectors", 0],
    ["assistants", "Assistants", "assistants", 0],
  ];

  return (
    <main className="flex h-screen overflow-hidden" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <aside className="hidden w-[256px] shrink-0 flex-col border-r hairline md:flex" style={{ background: "var(--bg-deep)" }}>
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-stone-900 text-[#fbfaf7] dark:bg-stone-100 dark:text-stone-900"><Icon name="logo" /></span>
            <span className="font-semibold tracking-normal">Chronos</span>
          </div>
        </div>
        <div className="px-3 pb-3">
          <button onClick={() => { setRoute("chat"); setActiveConversation(null); }} className="surface smooth flex w-full items-center gap-2.5 rounded-lg border border-soft px-3 py-2 text-[13.5px] font-medium hover:border-[var(--border)]">
            <Icon name="plus" /> New conversation
          </button>
        </div>
        <nav className="space-y-1 px-3">
          {nav.map(([id, label, icon, badge]) => (
            <button key={id} onClick={() => setRoute(id)} className={`nav-item w-full ${route === id ? "active" : ""}`}>
              <Icon name={icon} className="nav-icon h-[15px] w-[15px]" />
              <span className="flex-1 text-left">{label}</span>
              {badge > 0 ? <span className="rounded-full bg-[#d97835] px-1.5 py-0.5 text-[10px] font-semibold text-white">{badge}</span> : null}
            </button>
          ))}
        </nav>
        <div className="mt-4 flex-1 overflow-auto px-3">
          <h3 className="px-2 py-1 text-[11.5px] font-medium uppercase tracking-wider" style={{ color: "var(--text-dim)" }}>Conversations</h3>
          {conversations.length === 0 ? <p className="px-2 py-2 text-[13.5px]" style={{ color: "var(--text-dim)" }}>No conversations yet.</p> : null}
          {conversations.map((conversation) => (
            <button key={conversation.id} onClick={() => { setRoute("chat"); setActiveConversation(conversation.id); }} className={`convo-row w-full ${route === "chat" && activeConversation === conversation.id ? "active" : ""}`}>
              {conversation.title || "Untitled conversation"}
            </button>
          ))}
        </div>
        <div className="relative border-t hairline p-2">
          <button onClick={() => setAccountOpen((open) => !open)} className="smooth flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left hover:bg-[var(--surface-2)]">
            <span className="avatar-u">{ORG.member.initials}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">{ORG.member.name}</span>
              <span className="block truncate text-[11.5px]" style={{ color: "var(--text-dim)" }}>{ORG.name}</span>
            </span>
          </button>
          {accountOpen ? (
            <div className="surface absolute bottom-16 left-2 right-2 z-50 overflow-hidden rounded-xl border shadow-xl" style={{ borderColor: "var(--border)" }}>
              {(["account", "preferences", "workspace", "notifications", "audit"] as SettingsTab[]).map((tab) => (
                <button key={tab} onClick={() => openSettings(tab)} className="block w-full px-3 py-2 text-left text-sm capitalize hover:bg-stone-50 dark:hover:bg-stone-700">{tab === "audit" ? "Audit log" : tab}</button>
              ))}
              <button onClick={signOut} className="block w-full border-t border-stone-100 px-3 py-2 text-left text-sm text-red-600 dark:border-stone-700">Sign out</button>
            </div>
          ) : null}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 overflow-x-auto border-b hairline px-3 py-2 md:hidden" style={{ background: "var(--bg-deep)" }}>
          <button onClick={() => { setRoute("chat"); setActiveConversation(null); }} className="flex shrink-0 items-center gap-2 rounded-lg bg-white px-3 py-2 text-sm font-medium shadow-sm dark:bg-stone-800">
            <Icon name="logo" /> Chronos
          </button>
          {nav.map(([id]) => (
            <button key={id} onClick={() => setRoute(id)} className={`shrink-0 rounded-lg px-3 py-2 text-sm capitalize ${route === id ? "bg-white font-medium shadow-sm dark:bg-stone-800" : "text-stone-600 dark:text-stone-300"}`}>
              {id}
            </button>
          ))}
          <button onClick={() => openSettings("account")} className="shrink-0 rounded-lg px-3 py-2 text-sm text-stone-600 dark:text-stone-300">Account</button>
        </div>
        {error ? <div className="border-b border-red-200 bg-red-50 px-4 py-2 text-sm text-red-700">{error}</div> : null}
        <div className="min-h-0 flex-1">
          {route === "chat" && <ChatScreen activeConversation={activeConversation} onConversationCreated={(id) => refreshConversations(id)} />}
          {route === "activity" && <ActivityScreen onAudit={() => openSettings("audit")} />}
          {route === "approvals" && <ApprovalsScreen />}
          {route === "memory" && <MemoryScreen />}
          {route === "connectors" && <ConnectorsScreen />}
          {route === "assistants" && <AssistantsScreen />}
          {route === "settings" && <SettingsScreen tab={settingsTab} setTab={setSettingsTab} theme={theme} setTheme={setTheme} signOut={signOut} />}
        </div>
      </section>
    </main>
  );
}

function ChatScreen({ activeConversation, onConversationCreated }: { activeConversation: string | null; onConversationCreated: (id: string) => void }) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function loadMessages() {
      setError("");
      if (!activeConversation) {
        setMessages([]);
        return;
      }
      try {
        const data = (await (await apiFetch(`/chat/conversations/${activeConversation}/messages`)).json()) as Message[];
        if (!cancelled) setMessages(data);
      } catch (exc) {
        if (!cancelled) setError(exc instanceof Error ? exc.message : "Unable to load messages");
      }
    }
    void loadMessages();
    return () => {
      cancelled = true;
    };
  }, [activeConversation]);

  async function send(event: FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || isStreaming) return;
    setDraft("");
    setError("");
    setIsStreaming(true);
    setMessages((prev) => [...prev, { role: "user", content: message }, { role: "assistant", content: "" }]);
    try {
      const res = await fetch(`${API_BASE}/chat/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ message, conversation_id: activeConversation }),
      });
      if (!res.ok || !res.body) throw new Error(await res.text());
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        for (const eventText of events) {
          const line = eventText.split("\n").find((row) => row.startsWith("data: "));
          if (!line) continue;
          const eventData = JSON.parse(line.slice(6));
          if (eventData.type === "conversation" && !activeConversation) onConversationCreated(eventData.conversation_id);
          if (eventData.type === "token") {
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") last.content += eventData.content;
              return next;
            });
          }
          if (eventData.type === "memory_saved") {
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant" && !last.content) last.content = `Memory saved: ${eventData.content}`;
              return next;
            });
          }
        }
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Message failed");
      setMessages((prev) => prev.slice(0, -1));
    } finally {
      setIsStreaming(false);
    }
  }

  return (
    <div className="flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-[52px] items-center justify-between border-b hairline px-6" style={{ background: "var(--bg)" }}>
          <div className="flex items-center gap-3">
            <Avatar label="Chronos" />
            <div>
              <p className="text-sm font-semibold">Chronos</p>
              <p className="text-xs" style={{ color: "var(--text-dim)" }}>Connected to the local API</p>
            </div>
          </div>
        </header>
        {error ? <div className="border-b border-red-200 bg-red-50 px-6 py-2 text-sm text-red-700">{error}</div> : null}
        <div className="flex-1 overflow-auto px-6 py-10">
          {messages.length === 0 ? <EmptyChat /> : <Thread messages={messages} />}
        </div>
        <Composer value={draft} setValue={setDraft} onSubmit={send} disabled={isStreaming} />
      </div>
    </div>
  );
}

function EmptyChat() {
  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col justify-center pb-20">
      <div className="mb-6 flex items-center gap-3">
        <Avatar label="Chronos" />
        <span className="text-[13px]" style={{ color: "var(--text-dim)" }}>New conversation</span>
      </div>
      <h1 className="h-display">What can I help with?</h1>
      <p className="mt-3 text-[15px]" style={{ color: "var(--text-dim)" }}>Start a fresh conversation. No sample chats or generated logs are shown here.</p>
    </div>
  );
}

function Thread({ messages }: { messages: Message[] }) {
  return (
    <div className="mx-auto max-w-3xl space-y-10">
      {messages.map((message, index) => (
        <article key={message.id ?? index} className="flex gap-4">
          <Avatar label={message.role === "user" ? "You" : "Chronos"} />
          <div>
            <p className="mb-1 text-sm font-semibold">{message.role === "user" ? "You" : "Chronos"}</p>
            <p className="prose-body whitespace-pre-wrap">{message.content || "..."}</p>
          </div>
        </article>
      ))}
    </div>
  );
}

function Composer({ value, setValue, onSubmit, disabled }: { value: string; setValue: (value: string) => void; onSubmit: (event: FormEvent) => void; disabled: boolean }) {
  return (
    <form onSubmit={onSubmit} className="border-t hairline p-4" style={{ background: "var(--bg)" }}>
      <div className="composer-shell relative mx-auto max-w-4xl p-3">
        <textarea value={value} onChange={(event) => setValue(event.target.value)} placeholder="Message Chronos..." className="min-h-16 w-full resize-none bg-transparent px-2 text-[15px] outline-none" />
        <div className="flex items-center justify-end">
          <button className="flex h-9 w-9 items-center justify-center rounded-full text-white disabled:opacity-40" style={{ background: "var(--text)", color: "var(--bg)" }} disabled={!value.trim() || disabled}><Icon name="send" /></button>
        </div>
      </div>
    </form>
  );
}

function ActivityScreen({ onAudit }: { onAudit: () => void }) {
  const [mode, setMode] = useState<ActivityMode>("jobs");
  return (
    <Page title="Activity" subtitle="Live work will appear here when the task engine writes activity events." action={
      <div className="surface rounded-lg border border-soft p-1">
        {(["jobs", "actions"] as ActivityMode[]).map((item) => <button key={item} onClick={() => setMode(item)} className="smooth rounded-md px-3 py-1.5 text-[13px] font-medium capitalize" style={{ background: mode === item ? "var(--surface-2)" : "transparent", color: mode === item ? "var(--text)" : "var(--text-muted)" }}>{item === "actions" ? "Every action" : "Jobs"}</button>)}
      </div>
    }>
      <EmptyState>{mode === "jobs" ? "No jobs have been started in this workspace." : "No activity events are available yet."}</EmptyState>
      {mode === "actions" ? <button onClick={onAudit} className="mt-4 w-full rounded-xl py-3 text-sm text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800">Open audit settings</button> : null}
    </Page>
  );
}

function ApprovalsScreen() {
  return (
    <Page title="Approvals" subtitle="Requests that need operator approval before Chronos acts.">
      <EmptyState>No approvals are waiting.</EmptyState>
    </Page>
  );
}

function MemoryScreen() {
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    void refreshMemory();
  }, []);

  async function refreshMemory() {
    setError("");
    try {
      const data = (await (await apiFetch("/memory/")).json()) as MemoryEntry[];
      setMemories(data);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to load memory");
    }
  }

  async function saveMemory(event: FormEvent) {
    event.preventDefault();
    const nextContent = content.trim();
    if (!nextContent) return;
    try {
      if (editing) await apiFetch(`/memory/${editing}`, { method: "PATCH", body: JSON.stringify({ content: nextContent }) });
      else await apiFetch("/memory/", { method: "POST", body: JSON.stringify({ content: nextContent, scope: "org" }) });
      setContent("");
      setEditing(null);
      await refreshMemory();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to save memory");
    }
  }

  async function deleteMemory(id: string) {
    try {
      await apiFetch(`/memory/${id}`, { method: "DELETE" });
      await refreshMemory();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to delete memory");
    }
  }

  return (
    <Page title="Memory" subtitle="What Chronos has actually saved through the memory API.">
      <form onSubmit={saveMemory} className="mem-card mb-5 p-5">
        <label className="block text-sm font-medium">Memory content</label>
        <textarea value={content} onChange={(event) => setContent(event.target.value)} className="mt-2 min-h-20 w-full resize-none rounded-lg border border-stone-200 bg-transparent px-3 py-2 text-sm outline-none focus:border-stone-400 dark:border-stone-700" />
        <div className="mt-3 flex gap-2">
          <button className="rounded-lg px-3 py-2 text-sm font-medium disabled:opacity-40" style={{ background: "var(--text)", color: "var(--bg)" }} disabled={!content.trim()}>{editing ? "Update memory" : "Add memory"}</button>
          {editing ? <button type="button" onClick={() => { setEditing(null); setContent(""); }} className="rounded-lg border border-stone-200 px-3 py-2 text-sm dark:border-stone-700">Cancel</button> : null}
        </div>
      </form>
      {error ? <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}
      {memories.length === 0 ? <EmptyState>No memory entries found.</EmptyState> : null}
      <div className="grid gap-3">
        {memories.map((memory) => (
          <Surface key={memory.id}>
            <div className="flex items-start justify-between gap-4">
              <div><p>{memory.content}</p><p className="mt-2 text-sm text-stone-500">{memory.scope} · {memory.source}</p></div>
              <div className="flex gap-1">
                <button onClick={() => { setEditing(memory.id); setContent(memory.content); }} className="rounded-lg p-2 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-700" aria-label="Edit memory"><Icon name="edit" /></button>
                <button onClick={() => deleteMemory(memory.id)} className="rounded-lg p-2 text-red-600 hover:bg-red-50 dark:hover:bg-stone-700" aria-label="Delete memory"><Icon name="trash" /></button>
              </div>
            </div>
          </Surface>
        ))}
      </div>
    </Page>
  );
}

function ConnectorsScreen() {
  return (
    <Page title="Connectors" subtitle="Connector status will appear here when connector APIs are implemented.">
      <EmptyState>No connectors are configured.</EmptyState>
    </Page>
  );
}

function AssistantsScreen() {
  return (
    <Page title="Assistants" subtitle="Assistant configuration will appear here when personas are backed by the API.">
      <div className="grid gap-4 lg:grid-cols-3">
        <Surface>
          <Avatar label="Chronos" />
          <h3 className="mt-4 font-semibold">Chronos</h3>
          <p className="text-sm text-stone-500">Default assistant identity for this local build.</p>
          <div className="mt-4"><Pill>Default</Pill></div>
        </Surface>
      </div>
    </Page>
  );
}

function SettingsScreen({ tab, setTab, theme, setTheme, signOut }: { tab: SettingsTab; setTab: (tab: SettingsTab) => void; theme: "light" | "dark"; setTheme: (theme: "light" | "dark") => void; signOut: () => void }) {
  return (
    <div className="flex h-full flex-col md:flex-row">
      <aside className="shrink-0 border-b border-stone-200 p-4 dark:border-stone-700 md:w-56 md:border-b-0 md:border-r">
        <h1 className="mb-3 font-semibold md:mb-4">Settings</h1>
        <div className="flex gap-1 overflow-x-auto md:block">{(["account", "preferences", "workspace", "notifications", "audit"] as SettingsTab[]).map((item) => <button key={item} onClick={() => setTab(item)} className={`shrink-0 rounded-lg px-3 py-2 text-left text-sm capitalize md:block md:w-full ${tab === item ? "bg-stone-100 font-medium dark:bg-stone-800" : "text-stone-500 hover:bg-stone-50 dark:hover:bg-stone-800"}`}>{item === "audit" ? "Audit log" : item}</button>)}</div>
      </aside>
      <div className="flex-1 overflow-auto">
        <Page title={tab === "audit" ? "Audit log" : tab[0].toUpperCase() + tab.slice(1)} subtitle={tab === "audit" ? "Audit data is recorded by the backend; a read endpoint is not implemented yet." : "Manage your local Chronos workspace."}>
          {tab === "account" ? <SettingsCard rows={[["Name", ORG.member.name], ["Email", ORG.member.email], ["Role", ORG.member.role]]} action={<button onClick={signOut} className="rounded-lg border border-stone-200 px-3 py-2 text-sm text-red-600 hover:bg-red-50 dark:border-stone-700 dark:hover:bg-stone-700">Sign out</button>} /> : null}
          {tab === "preferences" ? <Surface><div className="flex items-center justify-between"><div><h3 className="font-medium">Theme</h3><p className="text-sm text-stone-500">Switches the look across the app.</p></div><div className="rounded-lg bg-stone-100 p-1 dark:bg-stone-800">{(["light", "dark"] as const).map((item) => <button key={item} onClick={() => setTheme(item)} className={`rounded-md px-3 py-1.5 text-sm capitalize ${theme === item ? "bg-white shadow-sm dark:bg-stone-700" : "text-stone-500"}`}>{item}</button>)}</div></div></Surface> : null}
          {tab === "workspace" ? <SettingsCard rows={[["Workspace name", ORG.name], ["API", API_BASE]]} /> : null}
          {tab === "notifications" ? <EmptyState>No notification channels are configured.</EmptyState> : null}
          {tab === "audit" ? <EmptyState>No audit read endpoint is available in the UI yet.</EmptyState> : null}
        </Page>
      </div>
    </div>
  );
}

function SettingsCard({ rows, action }: { rows: string[][]; action?: ReactNode }) {
  return <Surface>{rows.map(([label, value], index) => <div key={label} className={`flex items-center justify-between py-3 ${index < rows.length - 1 ? "border-b border-stone-100 dark:border-stone-700" : ""}`}><span className="text-sm font-medium">{label}</span><span className="text-sm text-stone-500">{value}</span></div>)}{action ? <div className="mt-4">{action}</div> : null}</Surface>;
}

function Page({ title, subtitle, action, children }: { title: string; subtitle?: string; action?: ReactNode; children: ReactNode }) {
  return <div className="h-full overflow-auto"><header className="flex flex-col items-start justify-between gap-4 px-4 pb-6 pt-7 sm:px-8 md:flex-row md:px-10 md:pt-9"><div><h1 className="h-page">{title}</h1>{subtitle ? <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>{subtitle}</p> : null}</div>{action}</header><div className="px-4 pb-10 sm:px-8 md:px-10">{children}</div></div>;
}

function Surface({ children }: { children: ReactNode }) {
  return <div className="surface rounded-xl border border-soft p-5">{children}</div>;
}

function EmptyState({ children }: { children: ReactNode }) {
  return <div className="surface rounded-xl border border-dashed p-6 text-sm" style={{ borderColor: "var(--border-soft)", color: "var(--text-dim)" }}>{children}</div>;
}
