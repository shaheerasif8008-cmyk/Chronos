"use client";

import { FormEvent, KeyboardEvent, ReactNode, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type Route = "chat" | "activity" | "approvals" | "memory" | "connectors" | "assistants" | "settings";
type ActivityMode = "jobs" | "actions";
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
  runtime_source?: string;
  persona_source?: string;
  tool_traces?: ToolTrace[];
  citations?: ReferenceItem[];
  memory_refs?: ReferenceItem[];
  artifacts?: ReferenceItem[];
  approval_state?: string;
};
type ToolTrace = { id: string; tool: string; summary: string; status: MessageStatus };
type ReferenceItem = { id: string; label: string; href?: string };
type MemoryEntry = { id: string; scope: string; scope_id: string; content: string; source: string; created_by?: string | null };
type Connector = { id: string; provider: string; account_handle?: string | null; status: string; connected_at?: string | null; last_used_at?: string | null };
type ConnectorProof = { connectorId: string; status: string; detail: string; tool?: string | null };
type Task = { id: string; status: string; goal: string; current_step: number; plan?: TaskStep[]; result?: Record<string, unknown>; created_at?: string; parent_task_id?: string | null; depth?: number };
type TaskStep = { id: string; action: string; description: string; tool?: string | null; args?: Record<string, unknown>; approval_required?: boolean; depends_on?: string[] };
type ActivityEvent = { type: string; task_id?: string; ts?: string; step?: TaskStep; step_index?: number; result?: unknown; error?: string; approval_ids?: string[]; sub_task_id?: string; event?: ActivityEvent };
type Approval = { id: string; task_id: string; step_id: string; action_type: string; action_payload: Record<string, unknown>; requested_at?: string; status: string };

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
    artifact: <><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" /><path d="M14 2v6h6" /></>,
    approvals: <><rect x="4" y="5" width="16" height="14" rx="2" /><path d="M4 9h16" /></>,
    branch: <><path d="M6 3v6a3 3 0 0 0 3 3h6" /><path d="M15 7l4 5-4 5" /><path d="M6 21v-6" /></>,
    memory: <><path d="M7 10c0-4 3-7 7-7s7 3 7 7v7a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3" /><path d="M10 10h6M10 14h5" /></>,
    connectors: <><path d="M8 3v5M16 3v5M6 8h12v3a6 6 0 0 1-12 0z" /><path d="M12 17v4" /></>,
    assistants: <><circle cx="12" cy="8" r="4" /><path d="M4 21c0-4 3.5-7 8-7s8 3 8 7" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1" /></>,
    send: <path d="M4 12 20 5l-6 16-3-7z" />,
    stop: <rect x="7" y="7" width="10" height="10" rx="1.5" />,
    edit: <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />,
    trash: <><path d="M3 6h18" /><path d="M8 6V4h8v2M6 6l1 15h10l1-15" /></>,
    copy: <><rect x="9" y="9" width="10" height="10" rx="2" /><path d="M5 15V5h10" /></>,
    retry: <><path d="M20 12a8 8 0 1 1-2.3-5.7" /><path d="M20 4v6h-6" /></>,
    pin: <><path d="M12 17v5" /><path d="m5 17 7-14 7 14Z" /></>,
    workflow: <><rect x="3" y="4" width="6" height="6" rx="1.5" /><rect x="15" y="4" width="6" height="6" rx="1.5" /><rect x="9" y="15" width="6" height="6" rx="1.5" /><path d="M9 7h6M12 10v5" /></>,
    more: <><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></>,
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
  const [conversationMenu, setConversationMenu] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [pendingApprovals, setPendingApprovals] = useState(0);
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
    void refreshPendingApprovals();
  }, [router]);

  async function refreshPendingApprovals() {
    try {
      const data = (await (await apiFetch("/approvals/?status=pending")).json()) as Approval[];
      setPendingApprovals(data.length);
    } catch {
      setPendingApprovals(0);
    }
  }

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

  async function deleteConversation(conversationId: string) {
    setError("");
    try {
      await apiFetch(`/chat/conversations/${conversationId}`, { method: "DELETE" });
      setConversationMenu(null);
      setConversations((current) => current.filter((conversation) => conversation.id !== conversationId));
      if (activeConversation === conversationId) setActiveConversation(null);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Unable to delete conversation");
    }
  }

  const nav: Array<[Route, string, string, number]> = [
    ["activity", "Activity", "activity", 0],
    ["approvals", "Approvals", "approvals", pendingApprovals],
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
            <div key={conversation.id} className="group relative">
              <button onClick={() => { setRoute("chat"); setActiveConversation(conversation.id); setConversationMenu(null); }} className={`convo-row w-full pr-9 ${route === "chat" && activeConversation === conversation.id ? "active" : ""}`}>
                {conversation.title || "Untitled conversation"}
              </button>
              <button
                onClick={(event) => { event.stopPropagation(); setConversationMenu((current) => current === conversation.id ? null : conversation.id); }}
                className="absolute right-1 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-stone-500 opacity-0 hover:bg-stone-100 group-hover:opacity-100 dark:hover:bg-stone-800"
                aria-label="Conversation actions"
              >
                <Icon name="more" />
              </button>
              {conversationMenu === conversation.id ? (
                <div className="surface absolute right-1 top-8 z-30 w-36 overflow-hidden rounded-lg border shadow-lg" style={{ borderColor: "var(--border)" }}>
                  <button onClick={() => deleteConversation(conversation.id)} className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-red-600 hover:bg-red-50 dark:hover:bg-stone-800">
                    <Icon name="trash" /> Delete chat
                  </button>
                </div>
              ) : null}
            </div>
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
          {route === "chat" && <ChatScreen activeConversation={activeConversation} onConversationCreated={(id) => refreshConversations(id)} onTaskCreated={(id) => { setActiveTaskId(id); setRoute("activity"); }} />}
          {route === "activity" && <ActivityScreen activeTaskId={activeTaskId} setActiveTaskId={setActiveTaskId} onAudit={() => openSettings("audit")} />}
          {route === "approvals" && <ApprovalsScreen onDecision={refreshPendingApprovals} onOpenTask={(id) => { setActiveTaskId(id); setRoute("activity"); }} />}
          {route === "memory" && <MemoryScreen />}
          {route === "connectors" && <ConnectorsScreen />}
          {route === "assistants" && <AssistantsScreen />}
          {route === "settings" && <SettingsScreen tab={settingsTab} setTab={setSettingsTab} theme={theme} setTheme={setTheme} signOut={signOut} />}
        </div>
      </section>
    </main>
  );
}

function ChatScreen({ activeConversation, onConversationCreated, onTaskCreated }: { activeConversation: string | null; onConversationCreated: (id: string) => void; onTaskCreated: (id: string) => void }) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState("");
  const streamAbortRef = useRef<AbortController | null>(null);

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
    await sendMessage(draft.trim(), { appendUser: true });
  }

  async function sendMessage(message: string, { appendUser }: { appendUser: boolean }) {
    if (!message || isStreaming) return;
    const controller = new AbortController();
    streamAbortRef.current = controller;
    if (appendUser) setDraft("");
    setError("");
    setIsStreaming(true);
    setMessages((prev) => [...prev, ...(appendUser ? [{ role: "user" as const, content: message }] : []), { role: "assistant", content: "" }]);
    try {
      const res = await fetch(`${API_BASE}/chat/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ message, conversation_id: activeConversation }),
        signal: controller.signal,
      });
      if (res.status === 401) {
        localStorage.removeItem("chronos_token");
        window.location.href = "/login";
        return;
      }
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
          if (eventData.type === "task_created") {
            onTaskCreated(eventData.task_id);
            setMessages((prev) => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last?.role === "assistant") {
                last.content = `${last.content.trim() || "I started this as an autonomous task."}\n\nTask created: ${eventData.task_id}`;
              }
              return next;
            });
          }
        }
      }
    } catch (exc) {
      if (exc instanceof DOMException && exc.name === "AbortError") {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last?.role === "assistant") {
            last.status = "paused";
          }
          return next;
        });
        return;
      }
      setError(humanizeError(exc));
      setMessages((prev) => prev.slice(0, -1));
    } finally {
      if (streamAbortRef.current === controller) streamAbortRef.current = null;
      setIsStreaming(false);
    }
  }

  function retryResponse(index: number) {
    const previousUser = [...messages.slice(0, index)].reverse().find((message) => message.role === "user");
    if (!previousUser) return;
    setMessages((prev) => prev.slice(0, index));
    void sendMessage(previousUser.content, { appendUser: false });
  }

  function stopStreaming() {
    streamAbortRef.current?.abort();
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
          {messages.length === 0 ? <EmptyChat /> : <Thread messages={messages} onRetry={retryResponse} />}
        </div>
        <Composer value={draft} setValue={setDraft} onSubmit={send} disabled={isStreaming} isStreaming={isStreaming} onStop={stopStreaming} />
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

function Thread({ messages, onRetry }: { messages: Message[]; onRetry: (index: number) => void }) {
  async function copyResponse(content: string) {
    if (!content) return;
    await navigator.clipboard.writeText(content);
  }

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      {messages.map((message, index) => (
        <article key={message.id ?? index} className="message-block">
          <Avatar label={message.role === "user" ? "You" : "Chronos"} color={message.role === "user" ? "#5f6d7a" : "#d37a36"} />
          <div className="min-w-0 flex-1">
            <MessageHeader message={message} />
            <div className="mt-3">
              {message.content ? <p className="prose-body whitespace-pre-wrap">{message.content}</p> : <TypingDots />}
              {message.status === "paused" ? <p className="mt-2 text-xs" style={{ color: "var(--text-dim)" }}>Chat response paused</p> : null}
            </div>
            <MessageOperationalDetails message={message} />
            {message.role === "assistant" && message.content ? (
              <MessageControls
                onCopy={() => copyResponse(message.content)}
                onRetry={() => onRetry(index)}
              />
            ) : null}
          </div>
        </article>
      ))}
    </div>
  );
}

function MessageHeader({ message }: { message: Message }) {
  const source = message.role === "user" ? "Operator" : message.persona_source || "Chronos";
  const runtime = message.runtime_source || (message.role === "assistant" ? "API runtime" : "User input");
  return (
    <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm font-semibold">{source}</p>
          <Pill tone={messageStatusTone(message.status)}>{message.status || "complete"}</Pill>
          {message.approval_state ? <Pill tone="warn">{message.approval_state}</Pill> : null}
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs" style={{ color: "var(--text-dim)" }}>
          <span>{formatMessageTime(message.created_at)}</span>
          <span>{runtime}</span>
          <span>{messageTypeLabel(message)}</span>
        </div>
      </div>
    </div>
  );
}

function MessageOperationalDetails({ message }: { message: Message }) {
  const traces = message.tool_traces || inferredToolTraces(message);
  const memories = message.memory_refs || inferredMemoryRefs(message);
  const artifacts = message.artifacts || inferredArtifacts(message);
  const citations = message.citations || [];
  if (!traces.length && !memories.length && !artifacts.length && !citations.length) return null;

  return (
    <div className="mt-4 grid gap-2">
      {traces.length ? (
        <details className="message-detail">
          <summary>Tool execution traces</summary>
          <div className="mt-3 space-y-2">
            {traces.map((trace) => (
              <div key={trace.id} className="flex items-start justify-between gap-3 rounded-lg border border-soft px-3 py-2">
                <div>
                  <p className="text-sm font-medium">{trace.tool}</p>
                  <p className="text-xs" style={{ color: "var(--text-dim)" }}>{trace.summary}</p>
                </div>
                <Pill tone={messageStatusTone(trace.status)}>{trace.status}</Pill>
              </div>
            ))}
          </div>
        </details>
      ) : null}
      {[["Memory references", memories], ["Citations", citations], ["Artifacts", artifacts]].map(([label, items]) => {
        const refs = items as ReferenceItem[];
        return refs.length ? (
          <div key={label as string} className="message-ref-row">
            <span>{label as string}</span>
            <div className="flex flex-wrap gap-1.5">
              {refs.map((item) => <span key={item.id} className="tag">{item.label}</span>)}
            </div>
          </div>
        ) : null;
      })}
    </div>
  );
}

function ActionSuggestions({ onRetry }: { onRetry: () => void }) {
  const suggestions: Array<[string, string]> = [
    ["plus", "Create task"],
    ["memory", "Save to memory"],
    ["workflow", "Turn into workflow"],
    ["assistants", "Assign employee"],
    ["artifact", "Generate report"],
    ["approvals", "Request approval"],
  ];
  return (
    <div className="message-actions-menu" role="menu" aria-label="Message actions">
      <button onClick={onRetry} className="message-menu-item" type="button" role="menuitem">
        <Icon name="retry" className="h-3.5 w-3.5" />
        Retry response
      </button>
      <button className="message-menu-item" type="button" role="menuitem">
        <Icon name="branch" className="h-3.5 w-3.5" />
        Branch conversation
      </button>
      <button className="message-menu-item" type="button" role="menuitem">
        <Icon name="pin" className="h-3.5 w-3.5" />
        Pin output
      </button>
      {suggestions.map(([icon, label]) => (
        <button key={label} className="message-menu-item" type="button" role="menuitem">
          <Icon name={icon} className="h-3.5 w-3.5" />
          {label}
        </button>
      ))}
    </div>
  );
}

function MessageControls({ onCopy, onRetry }: { onCopy: () => void; onRetry: () => void }) {
  return (
    <div className="mt-3 flex items-start gap-1">
      <button onClick={onCopy} className="rounded-md p-1.5 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800" aria-label="Copy response" title="Copy response">
        <Icon name="copy" />
      </button>
      <details className="message-actions-dropdown">
        <summary aria-label="Open message actions" title="Message actions">
          <Icon name="more" />
          <span>Actions</span>
        </summary>
        <ActionSuggestions onRetry={onRetry} />
      </details>
    </div>
  );
}

function messageStatusTone(status?: MessageStatus): "neutral" | "ok" | "warn" {
  if (status === "paused" || status === "approval_pending") return "warn";
  if (status === "complete" || !status) return "ok";
  return "neutral";
}

function messageTypeLabel(message: Message) {
  if (message.role === "tool") return "Tool execution";
  if (message.role === "system") return "Runtime event";
  if (message.approval_state) return "Approval request";
  if (message.content.toLowerCase().includes("task created")) return "Task handoff";
  return message.role === "assistant" ? "AI response" : "User message";
}

function formatMessageTime(value?: string) {
  if (!value) return "Just now";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Just now";
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function inferredToolTraces(message: Message): ToolTrace[] {
  if (!message.content.toLowerCase().includes("task created")) return [];
  return [{ id: "task-created", tool: "tasks.create", summary: "Created an autonomous task from this response.", status: "complete" }];
}

function inferredMemoryRefs(message: Message): ReferenceItem[] {
  if (!message.content.toLowerCase().startsWith("memory saved:")) return [];
  return [{ id: "saved-memory", label: "Saved memory" }];
}

function inferredArtifacts(message: Message): ReferenceItem[] {
  if (!message.content.toLowerCase().includes("task created")) return [];
  return [{ id: "activity-stream", label: "Live activity stream" }];
}

function TypingDots() {
  return (
    <span className="typing-wave" aria-label="Chronos is responding">
      <span />
      <span />
      <span />
    </span>
  );
}

function humanizeError(error: unknown) {
  const message = error instanceof Error ? error.message : "Message failed";
  try {
    const parsed = JSON.parse(message) as { detail?: string };
    if (parsed.detail === "Invalid bearer token" || parsed.detail === "Missing bearer token") return "Your session expired. Sign in again.";
    return parsed.detail ?? message;
  } catch {
    return message;
  }
}

function Composer({ value, setValue, onSubmit, disabled, isStreaming, onStop }: { value: string; setValue: (value: string) => void; onSubmit: (event: FormEvent) => void; disabled: boolean; isStreaming: boolean; onStop: () => void }) {
  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  return (
    <form onSubmit={onSubmit} className="border-t hairline p-4" style={{ background: "var(--bg)" }}>
      <div className="composer-shell relative mx-auto max-w-4xl p-3">
        <textarea value={value} onChange={(event) => setValue(event.target.value)} onKeyDown={handleKeyDown} placeholder="Message Chronos..." className="min-h-16 w-full resize-none bg-transparent px-2 text-[15px] outline-none" />
        <div className="flex items-center justify-end">
          {isStreaming ? (
            <button type="button" onClick={onStop} className="flex h-9 w-9 items-center justify-center rounded-full text-white" style={{ background: "var(--danger)", color: "white" }} aria-label="Stop response" title="Stop response">
              <Icon name="stop" />
            </button>
          ) : (
            <button className="flex h-9 w-9 items-center justify-center rounded-full text-white disabled:opacity-40" style={{ background: "var(--text)", color: "var(--bg)" }} disabled={!value.trim() || disabled} aria-label="Send message"><Icon name="send" /></button>
          )}
        </div>
      </div>
    </form>
  );
}

function ActivityScreen({ activeTaskId, setActiveTaskId, onAudit }: { activeTaskId: string | null; setActiveTaskId: (id: string) => void; onAudit: () => void }) {
  const [mode, setMode] = useState<ActivityMode>("jobs");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [streamStatus, setStreamStatus] = useState("Select a task to stream live activity.");
  const [error, setError] = useState("");

  useEffect(() => {
    void refreshTasks();
  }, []);

  useEffect(() => {
    if (!activeTaskId) return;
    let cancelled = false;
    const controller = new AbortController();

    async function streamTask() {
      setEvents([]);
      setError("");
      setStreamStatus("Connecting to activity stream...");
      try {
        const res = await fetch(`${API_BASE}/tasks/${activeTaskId}/stream`, {
          headers: authHeaders(),
          signal: controller.signal,
        });
        if (!res.ok || !res.body) throw new Error(await res.text());
        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!cancelled) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() ?? "";
          for (const chunk of chunks) {
            const line = chunk.split("\n").find((row) => row.startsWith("data: "));
            if (!line) continue;
            const eventData = JSON.parse(line.slice(6)) as ActivityEvent & { task?: Task };
            if (eventData.type === "catch_up") {
              setStreamStatus(`Streaming ${eventData.task?.status ?? "task"} task`);
              continue;
            }
            setEvents((current) => [...current, eventData]);
            if (eventData.type === "task_complete" || eventData.type === "task_failed") {
              setStreamStatus(eventData.type === "task_complete" ? "Task complete" : "Task failed");
              void refreshTasks();
            }
          }
        }
      } catch (exc) {
        if (!cancelled && !(exc instanceof DOMException && exc.name === "AbortError")) {
          setError(humanizeError(exc));
          setStreamStatus("Activity stream disconnected.");
        }
      }
    }

    void streamTask();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [activeTaskId]);

  async function refreshTasks() {
    setError("");
    try {
      const data = (await (await apiFetch("/tasks/")).json()) as Task[];
      setTasks(data);
      if (!activeTaskId && data[0]) setActiveTaskId(data[0].id);
    } catch (exc) {
      setError(humanizeError(exc));
    }
  }

  const selectedTask = tasks.find((task) => task.id === activeTaskId);

  return (
    <Page title="Activity" subtitle="Live work will appear here when the task engine writes activity events." action={
      <div className="surface rounded-lg border border-soft p-1">
        {(["jobs", "actions"] as ActivityMode[]).map((item) => <button key={item} onClick={() => setMode(item)} className="smooth rounded-md px-3 py-1.5 text-[13px] font-medium capitalize" style={{ background: mode === item ? "var(--surface-2)" : "transparent", color: mode === item ? "var(--text)" : "var(--text-muted)" }}>{item === "actions" ? "Every action" : "Jobs"}</button>)}
      </div>
    }>
      {error ? <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}
      {mode === "jobs" ? (
        <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold">Tasks</h2>
              <button onClick={refreshTasks} className="rounded-lg border border-stone-200 px-2.5 py-1.5 text-xs hover:bg-stone-50 dark:border-stone-700 dark:hover:bg-stone-800">Refresh</button>
            </div>
            {tasks.length === 0 ? <EmptyState>No jobs have been started in this workspace.</EmptyState> : null}
            {tasks.map((task) => (
              <button key={task.id} onClick={() => setActiveTaskId(task.id)} className={`surface block w-full rounded-xl border p-4 text-left smooth ${activeTaskId === task.id ? "border-[var(--accent)]" : "border-soft hover:border-[var(--border)]"}`}>
                <div className="flex items-center justify-between gap-3">
                  <Pill tone={task.status === "complete" ? "ok" : task.status === "awaiting_approval" ? "warn" : "neutral"}>{task.status}</Pill>
                  <span className="text-xs" style={{ color: "var(--text-dim)" }}>Step {task.current_step ?? 0}</span>
                </div>
                <p className="mt-3 line-clamp-3 text-sm font-medium">{task.goal}</p>
                <p className="mt-2 truncate text-xs" style={{ color: "var(--text-dim)" }}>{task.id}</p>
              </button>
            ))}
          </div>
          <Surface>
            {selectedTask ? (
              <div>
                <div className="flex flex-col gap-3 border-b border-stone-100 pb-4 dark:border-stone-700 md:flex-row md:items-start md:justify-between">
                  <div>
                    <h2 className="font-semibold">{selectedTask.goal}</h2>
                    <p className="mt-1 text-sm" style={{ color: "var(--text-dim)" }}>{streamStatus}</p>
                  </div>
                  <Pill tone={selectedTask.status === "complete" ? "ok" : selectedTask.status === "awaiting_approval" ? "warn" : "neutral"}>{selectedTask.status}</Pill>
                </div>
                <div className="mt-5 space-y-3">
                  {events.length === 0 ? <EmptyState>No live events have arrived for this task yet.</EmptyState> : null}
                  {events.map((event, index) => <ActivityEventRow key={`${event.type}-${index}`} event={event} />)}
                </div>
              </div>
            ) : <EmptyState>Select a task to inspect its live activity stream.</EmptyState>}
          </Surface>
        </div>
      ) : <EmptyState>No audit activity read endpoint is available in the UI yet.</EmptyState>}
      {mode === "actions" ? <button onClick={onAudit} className="mt-4 w-full rounded-xl py-3 text-sm text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800">Open audit settings</button> : null}
    </Page>
  );
}

function ActivityEventRow({ event }: { event: ActivityEvent }) {
  const title = event.type.replaceAll("_", " ");
  const description = event.step?.description || event.error || (event.sub_task_id ? `Sub-task ${event.sub_task_id}` : "");
  return (
    <div className="rounded-xl border border-soft p-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <p className="text-sm font-semibold capitalize">{title}</p>
          {description ? <p className="mt-1 text-sm" style={{ color: "var(--text-dim)" }}>{description}</p> : null}
        </div>
        {typeof event.step_index === "number" ? <span className="text-xs" style={{ color: "var(--text-dim)" }}>#{event.step_index + 1}</span> : null}
      </div>
      {event.event ? <div className="mt-3 border-l-2 pl-3" style={{ borderColor: "var(--border)" }}><ActivityEventRow event={event.event} /></div> : null}
    </div>
  );
}

function ApprovalsScreen({ onDecision, onOpenTask }: { onDecision: () => void; onOpenTask: (taskId: string) => void }) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    void refreshApprovals();
  }, []);

  async function refreshApprovals() {
    setError("");
    try {
      const data = (await (await apiFetch("/approvals/?status=pending")).json()) as Approval[];
      setApprovals(data);
    } catch (exc) {
      setError(humanizeError(exc));
    }
  }

  async function decide(approval: Approval, decision: "approved" | "rejected", batch = false) {
    setBusy(batch ? "batch" : approval.id);
    setError("");
    try {
      await apiFetch(`/approvals/${approval.id}/decide`, {
        method: "POST",
        body: JSON.stringify({ decision, batch }),
      });
      await refreshApprovals();
      onDecision();
    } catch (exc) {
      setError(humanizeError(exc));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Page title="Approvals" subtitle="Requests that need operator approval before Chronos acts.">
      {error ? <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}
      {approvals.length === 0 ? <EmptyState>No approvals are waiting.</EmptyState> : null}
      <div className="grid gap-3">
        {approvals.map((approval, index) => (
          <Surface key={approval.id}>
            <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone="warn">Pending</Pill>
                  <p className="text-sm font-semibold">{approval.action_type}</p>
                  <span className="text-xs" style={{ color: "var(--text-dim)" }}>#{index + 1}</span>
                </div>
                <p className="mt-3 text-sm"><span className="font-medium">To:</span> {String(approval.action_payload.to ?? "Not specified")}</p>
                <p className="mt-1 text-sm"><span className="font-medium">Subject:</span> {String(approval.action_payload.subject ?? "No subject")}</p>
                <p className="mt-3 max-h-36 overflow-auto whitespace-pre-wrap rounded-lg border border-soft p-3 text-sm" style={{ color: "var(--text-muted)" }}>{String(approval.action_payload.body ?? JSON.stringify(approval.action_payload, null, 2))}</p>
                <button onClick={() => onOpenTask(approval.task_id)} className="mt-3 text-sm font-medium" style={{ color: "var(--accent-text)" }}>Open task activity</button>
              </div>
              <div className="flex shrink-0 flex-wrap gap-2">
                <button onClick={() => decide(approval, "approved")} disabled={busy !== null} className="rounded-lg px-3 py-2 text-sm font-medium disabled:opacity-50" style={{ background: "var(--text)", color: "var(--bg)" }}>Approve</button>
                <button onClick={() => decide(approval, "approved", true)} disabled={busy !== null} className="rounded-lg border border-stone-200 px-3 py-2 text-sm font-medium hover:bg-stone-50 disabled:opacity-50 dark:border-stone-700 dark:hover:bg-stone-800">Approve batch</button>
                <button onClick={() => decide(approval, "rejected")} disabled={busy !== null} className="rounded-lg border border-stone-200 px-3 py-2 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50 dark:border-stone-700 dark:hover:bg-stone-800">Reject</button>
              </div>
            </div>
          </Surface>
        ))}
      </div>
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
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [proof, setProof] = useState<ConnectorProof | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    void refreshConnectors();
  }, []);

  async function refreshConnectors() {
    setError("");
    try {
      const data = (await (await apiFetch("/connectors/")).json()) as Connector[];
      setConnectors(data);
    } catch (exc) {
      setError(humanizeError(exc));
    }
  }

  async function enableBrowser() {
    setBusy("browser");
    setError("");
    try {
      await apiFetch("/connectors/browser/enable", { method: "POST" });
      await refreshConnectors();
    } catch (exc) {
      setError(humanizeError(exc));
    } finally {
      setBusy(null);
    }
  }

  async function connectGmail() {
    setBusy("gmail");
    setError("");
    try {
      const data = (await (await apiFetch("/connectors/gmail/oauth-url")).json()) as { url: string };
      window.location.href = data.url;
    } catch (exc) {
      setError(humanizeError(exc));
      setBusy(null);
    }
  }

  async function runProof(connector: Connector) {
    setBusy(connector.id);
    setError("");
    setProof(null);
    try {
      const data = (await (await apiFetch(`/connectors/${connector.id}/test`, {
        method: "POST",
        body: JSON.stringify(connector.provider === "gmail" ? {
          to: "operator@example.com",
          subject: "Chronos connector proof",
          body: "This draft proves Gmail actions route through the Chronos tool broker.",
        } : { url: "https://example.com" }),
      })).json()) as { status: string; detail: string; tool?: string | null };
      setProof({ connectorId: connector.id, ...data });
      await refreshConnectors();
    } catch (exc) {
      setError(humanizeError(exc));
    } finally {
      setBusy(null);
    }
  }

  async function disconnect(connector: Connector) {
    setBusy(connector.id);
    setError("");
    try {
      await apiFetch(`/connectors/${connector.id}`, { method: "DELETE" });
      if (proof?.connectorId === connector.id) setProof(null);
      await refreshConnectors();
    } catch (exc) {
      setError(humanizeError(exc));
    } finally {
      setBusy(null);
    }
  }

  const hasBrowser = connectors.some((connector) => connector.provider === "browser" && connector.status === "active");

  return (
    <Page title="Connectors" subtitle="Broker-routed Gmail and browser capabilities with audit-backed proof.">
      <div className="mb-5 flex flex-wrap gap-2">
        <button onClick={enableBrowser} disabled={hasBrowser || busy === "browser"} className="rounded-lg px-3 py-2 text-sm font-medium disabled:opacity-50" style={{ background: "var(--text)", color: "var(--bg)" }}>
          {hasBrowser ? "Browser enabled" : "Enable browser"}
        </button>
        <button onClick={connectGmail} disabled={busy === "gmail"} className="rounded-lg border border-stone-200 px-3 py-2 text-sm font-medium hover:bg-stone-50 disabled:opacity-50 dark:border-stone-700 dark:hover:bg-stone-800">
          Connect Gmail
        </button>
      </div>
      {error ? <p className="mb-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p> : null}
      {proof ? (
        <Surface>
          <p className="text-sm font-semibold">Proof result</p>
          <p className="mt-2 text-sm text-stone-600 dark:text-stone-300">Tool: {proof.tool || "unknown"} · Status: {proof.status}</p>
          <p className="mt-1 text-sm text-stone-500">{proof.detail}</p>
          <p className="mt-3 text-xs text-stone-500">This proof is executed through tool_broker.execute and writes connector_proof plus tool_call/tool_result audit events.</p>
        </Surface>
      ) : null}
      <div className="mt-4 grid gap-3">
        {connectors.length === 0 ? <EmptyState>No connectors are configured.</EmptyState> : null}
        {connectors.map((connector) => (
          <Surface key={connector.id}>
            <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <h3 className="font-semibold capitalize">{connector.provider}</h3>
                  <Pill tone={connector.status === "active" ? "ok" : "neutral"}>{connector.status}</Pill>
                </div>
                <p className="mt-1 text-sm text-stone-500">{connector.account_handle || "Org-level connector"}</p>
                <p className="mt-2 text-xs text-stone-500">Last proof: {connector.last_used_at || "Not run yet"}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <button onClick={() => runProof(connector)} disabled={busy === connector.id} className="rounded-lg px-3 py-2 text-sm font-medium disabled:opacity-50" style={{ background: "var(--text)", color: "var(--bg)" }}>
                  Run proof
                </button>
                <button onClick={() => disconnect(connector)} disabled={busy === connector.id} className="rounded-lg border border-stone-200 px-3 py-2 text-sm text-red-600 hover:bg-red-50 disabled:opacity-50 dark:border-stone-700 dark:hover:bg-stone-800">
                  Disconnect
                </button>
              </div>
            </div>
          </Surface>
        ))}
      </div>
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
