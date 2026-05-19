"use client";

import { FormEvent, ReactNode, useEffect, useState } from "react";

type Route = "chat" | "activity" | "approvals" | "memory" | "connectors" | "assistants" | "settings";
type ActivityMode = "jobs" | "actions";
type SettingsTab = "account" | "preferences" | "workspace" | "notifications" | "audit";

const ORG = {
  name: "Novatech",
  member: { name: "Alex Park", email: "alex@novatech.com", role: "Owner", initials: "AP" },
};

const assistants = [
  { id: "chronos", name: "Chronos", role: "General assistant", color: "#c56d2d", skills: ["General"] },
  { id: "jordan", name: "Jordan", role: "Sales outreach", color: "#2f6fba", skills: ["General", "Sales outreach"] },
  { id: "morgan", name: "Morgan", role: "Research analyst", color: "#3a8f5c", skills: ["General", "Research brief"] },
];

const conversations = [
  { id: "lead", title: "Outreach to Series B SaaS leads", group: "Today", active: true },
  { id: "deck", title: "Q3 board deck narrative pass", group: "Today" },
  { id: "pricing", title: "Competitor pricing summary", group: "Yesterday" },
  { id: "calls", title: "Rewrite our ICP from May calls", group: "Earlier" },
];

const jobs = [
  { title: "Find 20 leads and draft personalized outreach", status: "Working", detail: "4 of 20 drafts written", by: "Jordan", when: "Started 10:42 AM" },
  { title: "Refresh our ICP from May customer calls", status: "Waiting on you", detail: "Needs approval to read a members-only group", by: "Morgan", when: "Started 9:14 AM" },
  { title: "Publish v3 of the SOC 2 readiness doc", status: "Waiting on you", detail: "Review replacement document", by: "Chronos", when: "Yesterday" },
  { title: "Daily summary of the last 7 days of work", status: "Done", detail: "Delivered at 3:00 AM", by: "Chronos", when: "Today" },
  { title: "Scrape competitor pricing from gated PDFs", status: "Stopped", detail: "Hit a paywall", by: "Morgan", when: "May 13" },
];

const actions = [
  ["10:43 AM today", "Jordan", "Read Vanta's mid-market positioning"],
  ["10:42 AM today", "Jordan", "Saved a draft email to Mercury"],
  ["10:42 AM today", "Chronos", "Saved a memory about your outbound style"],
  ["10:42 AM today", "Jordan", "Read Mercury's careers page"],
  ["10:42 AM today", "Jordan", "Searched the web for Series B SaaS companies hiring SDRs"],
  ["10:38 AM today", "Alex Park", "Approved a batch of 8 emails"],
  ["9:14 AM today", "Morgan", "Asked you to approve reading a LinkedIn group"],
  ["Yesterday 4:52 PM", "Chronos", "Updated the Novatech ICP memory"],
];

const memories = [
  { scope: "Novatech", text: "Our ICP: B2B SaaS, post-PMF, 50-200 employees, technical buyer, US-based.", by: "Chronos", source: "Saved by Chronos" },
  { scope: "Novatech", text: "Don't mention pricing in cold outreach. Wait for the second reply.", by: "Alex Park", source: "Saved by you" },
  { scope: "Outreach", text: "Alex prefers a \"shipping by Friday\" framing in outbound when it's true.", by: "Chronos", source: "Saved by Chronos" },
  { scope: "Jordan", text: "Jordan opens with a one-line observation, not a pleasantry.", by: "Alex Park", source: "Saved by you" },
  { scope: "Private", text: "Compensation framework for new SDR hires: 70/30 base/variable, $90k OTE.", by: "Alex Park", source: "Saved by you" },
];

const approvals = [
  {
    from: "Jordan",
    title: "20 cold emails to Series B SaaS leads",
    summary: "Each email references the lead's hiring page or a recent product update.",
    requested: "11:02 AM",
    why: "These are outbound emails to people outside Novatech, so I want you to look before they go.",
    items: [
      { to: "Sarah Chen <sarah@mercury.com>", subject: "Idea on Mercury's SDR ramp", body: "Hi Sarah,\n\nSaw the three SDR openings on your careers page and wanted to share a pattern we keep seeing at Series B SaaS teams scaling outbound while still shipping by Friday.\n\nHappy to share the playbook in 12 minutes if useful.\n\nAlex" },
      { to: "Daniel Wu <daniel@ramp.com>", subject: "Quick thought on commercial pipeline", body: "Daniel,\n\nYour June post on consolidating commercial and enterprise pipeline echoed something we hear from other Ramp-stage teams.\n\nIf useful, I can send a one-pager first so you can decide if a call is worth it.\n\nAlex" },
      { to: "Jamie Park <jamie@linear.app>", subject: "Linear's GTM motion vs self-serve", body: "Hi Jamie,\n\nLoved the May product update. The framing on deliberate self-serve is exactly how we talk about it internally.\n\nCurious whether the SDR motion changes as Linear moves up-market.\n\nAlex" },
    ],
  },
  {
    from: "Morgan",
    title: "Read a members-only LinkedIn group",
    summary: "Read-only access to 14 days of posts to refresh the ICP.",
    requested: "9:14 AM",
    why: "Logging into a third-party account always asks you first, even when Chronos has access.",
    items: [{ to: "linkedin.com/groups/saas-rev-leaders", subject: "Scope: 14-day discussion history", body: "Read-only. I won't post or comment. I'll extract anonymized themes for the ICP refresh." }],
  },
];

const connectors = [
  ["Gmail", "Connected", "Draft and send email with your approval", "jordan@chronos.novatech.com"],
  ["Web", "Connected", "Search and read public pages", "Chronos browser"],
  ["Calendar", "Available", "Find time and send invites", "Not connected"],
  ["HubSpot", "Available", "Read and update CRM records", "Not connected"],
  ["Drive", "Available", "Read shared docs and create files", "Not connected"],
];

const allSkills = ["General", "Sales outreach", "Research brief", "Meeting prep", "Doc review", "Weekly update"];
const conversationGroups = conversations.reduce<Record<string, typeof conversations>>((groups, conversation) => {
  groups[conversation.group] = [...(groups[conversation.group] ?? []), conversation];
  return groups;
}, {});

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
    check: <path d="m5 13 4 4L19 7" />,
    x: <path d="m7 7 10 10M17 7 7 17" />,
    mail: <><rect x="4" y="6" width="16" height="12" rx="2" /><path d="m4 8 8 6 8-6" /></>,
    dots: <><circle cx="6" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="18" cy="12" r="1" /></>,
  };
  return <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name]}</svg>;
}

function Avatar({ label, color = "#d37a36" }: { label: string; color?: string }) {
  return <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-semibold text-white" style={{ background: color }}>{label.slice(0, 1)}</span>;
}

function Pill({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "ok" | "warn" | "accent" }) {
  const toneClass = {
    neutral: "bg-stone-100 text-stone-600",
    ok: "bg-emerald-50 text-emerald-700",
    warn: "bg-amber-50 text-amber-700",
    accent: "bg-orange-50 text-orange-800",
  }[tone];
  return <span className={`inline-flex items-center rounded-md px-2 py-1 text-xs font-medium ${toneClass}`}>{children}</span>;
}

export default function ChronosApp() {
  const [route, setRoute] = useState<Route>("chat");
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("account");
  const [activeConversation, setActiveConversation] = useState("lead");
  const [accountOpen, setAccountOpen] = useState(false);
  const [theme, setTheme] = useState<"light" | "dark">("light");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  function openSettings(tab: SettingsTab) {
    setSettingsTab(tab);
    setRoute("settings");
    setAccountOpen(false);
  }

  return (
    <main className="flex h-screen overflow-hidden bg-[#fbfaf7] text-stone-900 dark:bg-[#25231f] dark:text-stone-100">
      <aside className="hidden w-64 shrink-0 flex-col border-r border-stone-200 bg-[#f5f1ea] dark:border-stone-700 dark:bg-[#1f1d1a] md:flex">
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-stone-900 text-[#fbfaf7] dark:bg-stone-100 dark:text-stone-900"><Icon name="logo" /></span>
            <span className="font-semibold tracking-tight">Chronos</span>
          </div>
        </div>
        <div className="px-3 pb-3">
          <button onClick={() => { setRoute("chat"); setActiveConversation("new"); }} className="flex w-full items-center gap-2 rounded-xl border border-stone-200 bg-white px-3 py-2.5 text-sm font-medium shadow-sm transition hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:hover:bg-stone-700">
            <Icon name="plus" /> New conversation
          </button>
        </div>
        <nav className="space-y-1 px-3">
          {[
            ["activity", "Activity", "activity", 2],
            ["approvals", "Approvals", "approvals", approvals.length],
            ["memory", "Memory", "memory", 0],
            ["connectors", "Connectors", "connectors", 0],
            ["assistants", "Assistants", "assistants", 0],
          ].map(([id, label, icon, badge]) => (
            <button key={String(id)} onClick={() => setRoute(id as Route)} className={`flex w-full items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition ${route === id ? "bg-white text-stone-950 shadow-sm dark:bg-stone-800 dark:text-white" : "text-stone-600 hover:bg-white/70 dark:text-stone-300 dark:hover:bg-stone-800"}`}>
              <Icon name={String(icon)} />
              <span className="flex-1 text-left">{label}</span>
              {Number(badge) > 0 && <span className="rounded-full bg-[#d97835] px-1.5 py-0.5 text-[10px] font-semibold text-white">{badge}</span>}
            </button>
          ))}
        </nav>
        <div className="mt-4 flex-1 overflow-auto px-3">
          {Object.entries(conversationGroups).map(([group, rows]) => (
            <section key={group} className="mb-4">
              <h3 className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-stone-400">{group}</h3>
              {rows?.map((conversation) => (
                <button key={conversation.id} onClick={() => { setRoute("chat"); setActiveConversation(conversation.id); }} className={`w-full truncate rounded-lg px-3 py-2 text-left text-sm transition ${route === "chat" && activeConversation === conversation.id ? "bg-white font-medium shadow-sm dark:bg-stone-800" : "text-stone-600 hover:bg-white/70 dark:text-stone-300 dark:hover:bg-stone-800"}`}>
                  {conversation.title}
                </button>
              ))}
            </section>
          ))}
        </div>
        <div className="relative border-t border-stone-200 p-2 dark:border-stone-700">
          <button onClick={() => setAccountOpen((open) => !open)} className="flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition hover:bg-white dark:hover:bg-stone-800">
            <span className="flex h-8 w-8 items-center justify-center rounded-full bg-stone-200 text-sm font-semibold dark:bg-stone-700">{ORG.member.initials}</span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium">{ORG.member.name}</span>
              <span className="block truncate text-xs text-stone-500">{ORG.name} · {ORG.member.role}</span>
            </span>
          </button>
          {accountOpen && (
            <div className="absolute bottom-16 left-2 right-2 overflow-hidden rounded-xl border border-stone-200 bg-white shadow-xl dark:border-stone-700 dark:bg-stone-800">
              <div className="border-b border-stone-100 px-3 py-3 dark:border-stone-700">
                <p className="text-sm font-semibold">{ORG.member.name}</p>
                <p className="text-xs text-stone-500">{ORG.member.email}</p>
              </div>
              {(["account", "preferences", "workspace", "notifications", "audit"] as SettingsTab[]).map((tab) => (
                <button key={tab} onClick={() => openSettings(tab)} className="block w-full px-3 py-2 text-left text-sm capitalize hover:bg-stone-50 dark:hover:bg-stone-700">{tab === "audit" ? "Audit log" : tab}</button>
              ))}
              <button className="block w-full border-t border-stone-100 px-3 py-2 text-left text-sm text-red-600 dark:border-stone-700">Sign out</button>
            </div>
          )}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 overflow-x-auto border-b border-stone-200 bg-[#f5f1ea] px-3 py-2 dark:border-stone-700 dark:bg-[#1f1d1a] md:hidden">
          <button onClick={() => { setRoute("chat"); setActiveConversation("new"); }} className="flex shrink-0 items-center gap-2 rounded-lg bg-white px-3 py-2 text-sm font-medium shadow-sm dark:bg-stone-800">
            <Icon name="logo" /> Chronos
          </button>
          {(["activity", "approvals", "memory", "connectors", "assistants"] as Route[]).map((item) => (
            <button key={item} onClick={() => setRoute(item)} className={`shrink-0 rounded-lg px-3 py-2 text-sm capitalize ${route === item ? "bg-white font-medium shadow-sm dark:bg-stone-800" : "text-stone-600 dark:text-stone-300"}`}>
              {item}
            </button>
          ))}
          <button onClick={() => openSettings("account")} className="shrink-0 rounded-lg px-3 py-2 text-sm text-stone-600 dark:text-stone-300">Account</button>
        </div>
        <div className="min-h-0 flex-1">
          {route === "chat" && <ChatScreen activeConversation={activeConversation} />}
          {route === "activity" && <ActivityScreen onAudit={() => openSettings("audit")} />}
          {route === "approvals" && <ApprovalsScreen />}
          {route === "memory" && <MemoryScreen />}
          {route === "connectors" && <ConnectorsScreen />}
          {route === "assistants" && <AssistantsScreen />}
          {route === "settings" && <SettingsScreen tab={settingsTab} setTab={setSettingsTab} theme={theme} setTheme={setTheme} />}
        </div>
      </section>
    </main>
  );
}

function ChatScreen({ activeConversation }: { activeConversation: string }) {
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<Array<{ role: "user" | "assistant"; text: string }>>([]);
  const [activityOpen, setActivityOpen] = useState(false);
  const isEmpty = activeConversation === "new" && messages.length === 0;

  function send(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim()) return;
    setMessages((prev) => [...prev, { role: "user", text: draft.trim() }, { role: "assistant", text: "I’ll handle that. I’ll work in the background and ask before anything leaves Novatech." }]);
    setDraft("");
  }

  return (
    <div className="flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 items-center justify-between border-b border-stone-200 px-6 dark:border-stone-700">
          <div className="flex items-center gap-3">
            <Avatar label="Jordan" color="#2f6fba" />
            <div>
              <p className="text-sm font-semibold">Jordan</p>
              <p className="text-xs text-stone-500">Sales outreach</p>
            </div>
          </div>
          {!isEmpty && <button onClick={() => setActivityOpen((open) => !open)} className="rounded-lg px-3 py-2 text-sm text-stone-600 hover:bg-stone-100 dark:text-stone-300 dark:hover:bg-stone-800"><span className="mr-2 inline-block h-2 w-2 rounded-full bg-[#d97835]" />Working</button>}
        </header>
        <div className="flex-1 overflow-auto px-4 py-8 sm:px-6 sm:py-10">
          {isEmpty ? <EmptyChat setDraft={setDraft} /> : <Thread messages={messages} onOpenActivity={() => setActivityOpen(true)} />}
        </div>
        <Composer value={draft} setValue={setDraft} onSubmit={send} />
      </div>
      {activityOpen && <ActivityDrawer onClose={() => setActivityOpen(false)} />}
    </div>
  );
}

function EmptyChat({ setDraft }: { setDraft: (value: string) => void }) {
  const suggestions = ["Draft outreach to a list of leads", "Summarize this week's customer calls", "Research a market and competitors", "Rewrite our ICP from recent calls"];
  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col justify-center pb-20">
      <div className="mb-6 flex items-center gap-3">
        <Avatar label="Jordan" color="#2f6fba" />
        <span className="text-sm text-stone-500">Talking to Jordan · Sales outreach</span>
      </div>
      <h1 className="text-3xl font-semibold tracking-[-0.03em] sm:text-4xl">What can I help with?</h1>
      <p className="mt-3 text-stone-500">Ask anything, or pick one of these to get started.</p>
      <div className="mt-8 grid gap-3 sm:grid-cols-2">
        {suggestions.map((suggestion) => (
          <button key={suggestion} onClick={() => setDraft(suggestion)} className="flex items-center gap-3 rounded-xl border border-stone-200 bg-white p-4 text-left text-sm shadow-sm transition hover:bg-stone-50 dark:border-stone-700 dark:bg-stone-800 dark:hover:bg-stone-700">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-stone-100 text-stone-500 dark:bg-stone-700"><Icon name="mail" /></span>
            {suggestion}
          </button>
        ))}
      </div>
    </div>
  );
}

function Thread({ messages, onOpenActivity }: { messages: Array<{ role: "user" | "assistant"; text: string }>; onOpenActivity: () => void }) {
  const rows = messages.length ? messages : [
    { role: "user" as const, text: "Find 20 B2B SaaS companies, qualify them against our ICP, and draft personalized cold outreach for each." },
    { role: "assistant" as const, text: "On it. I’ll pull your ICP, research candidate companies, draft personalized emails, and put everything in Approvals before anything is sent." },
  ];
  return (
      <div className="mx-auto max-w-3xl space-y-10">
      {rows.map((message, index) => (
        <article key={index} className="flex gap-4">
          {message.role === "user" ? <span className="flex h-8 w-8 items-center justify-center rounded-full bg-stone-200 text-sm font-semibold">AP</span> : <Avatar label="Jordan" color="#2f6fba" />}
          <div>
            <p className="mb-1 text-sm font-semibold">{message.role === "user" ? ORG.member.name : "Jordan"}</p>
            <p className="text-[15px] leading-7">{message.text}</p>
          </div>
        </article>
      ))}
      <div className="overflow-hidden rounded-2xl border border-stone-200 bg-white shadow-sm dark:border-stone-700 dark:bg-stone-800 sm:ml-12">
        <button onClick={onOpenActivity} className="flex w-full items-center gap-3 p-4 text-left hover:bg-stone-50 dark:hover:bg-stone-700">
          <span className="h-2.5 w-2.5 animate-pulse rounded-full bg-[#d97835]" />
          <span className="flex-1">
            <span className="block text-sm font-medium">Working on your 20 leads — about 6 minutes left.</span>
            <span className="block text-xs text-stone-500">Writing draft 4 of 20 — Vanta</span>
          </span>
          <span className="text-xs text-stone-500">Show details</span>
        </button>
        <div className="border-t border-stone-100 p-4 dark:border-stone-700">
          <div className="mb-4 h-1.5 overflow-hidden rounded-full bg-stone-100 dark:bg-stone-700"><div className="h-full w-[62%] rounded-full bg-[#d97835]" /></div>
          {["Pulled your ICP and Novatech context", "Searched the web for matching companies", "Scored each against your ICP", "Writing personalized emails", "Saving everything as drafts to review"].map((step, i) => (
            <div key={step} className="flex items-center gap-3 py-1.5 text-sm">
              {i < 3 ? <Icon name="check" className="h-4 w-4 text-emerald-600" /> : <span className={`h-2 w-2 rounded-full ${i === 3 ? "animate-pulse bg-[#d97835]" : "bg-stone-300"}`} />}
              <span className={i > 3 ? "text-stone-400" : ""}>{step}</span>
              {i === 3 && <span className="ml-auto text-xs text-stone-500">4 of 20</span>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Composer({ value, setValue, onSubmit }: { value: string; setValue: (value: string) => void; onSubmit: (event: FormEvent) => void }) {
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [enabled, setEnabled] = useState(["General", "Sales outreach"]);
  return (
    <form onSubmit={onSubmit} className="border-t border-stone-200 bg-[#fbfaf7] p-4 dark:border-stone-700 dark:bg-[#25231f]">
      <div className="relative mx-auto max-w-4xl rounded-2xl border border-stone-200 bg-white p-3 shadow-sm focus-within:border-stone-400 dark:border-stone-700 dark:bg-stone-800">
        <textarea value={value} onChange={(event) => setValue(event.target.value)} placeholder="Message Chronos..." className="min-h-16 w-full resize-none bg-transparent px-2 text-[15px] outline-none" />
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => setSkillsOpen((open) => !open)} className="rounded-lg border border-stone-200 px-3 py-1.5 text-sm hover:bg-stone-50 dark:border-stone-700 dark:hover:bg-stone-700">Skills · {enabled.length}</button>
            <button type="button" className="rounded-lg px-3 py-1.5 text-sm text-stone-500 hover:bg-stone-50 dark:hover:bg-stone-700">Attach</button>
          </div>
          <button className="flex h-9 w-9 items-center justify-center rounded-full bg-stone-900 text-white disabled:opacity-40 dark:bg-stone-100 dark:text-stone-900" disabled={!value.trim()}><Icon name="send" /></button>
        </div>
        {skillsOpen && (
          <div className="absolute bottom-16 left-3 z-10 w-80 rounded-2xl border border-stone-200 bg-white p-3 shadow-xl dark:border-stone-700 dark:bg-stone-800">
            <p className="px-2 pb-2 text-xs font-semibold uppercase tracking-wide text-stone-400">On for this conversation</p>
            {allSkills.map((skill) => {
              const on = enabled.includes(skill);
              return (
                <button key={skill} type="button" onClick={() => setEnabled((prev) => on ? prev.filter((item) => item !== skill) : [...prev, skill])} className="flex w-full items-center justify-between rounded-lg px-2 py-2 text-left text-sm hover:bg-stone-50 dark:hover:bg-stone-700">
                  <span>{skill}</span>
                  <span className={`h-5 w-9 rounded-full p-0.5 transition ${on ? "bg-[#d97835]" : "bg-stone-200 dark:bg-stone-700"}`}><span className={`block h-4 w-4 rounded-full bg-white transition ${on ? "translate-x-4" : ""}`} /></span>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </form>
  );
}

function ActivityDrawer({ onClose }: { onClose: () => void }) {
  return (
    <aside className="absolute inset-x-3 bottom-3 top-20 z-20 overflow-auto rounded-2xl border border-stone-200 bg-white p-5 shadow-xl dark:border-stone-700 dark:bg-stone-800 md:static md:inset-auto md:w-[380px] md:rounded-none md:border-y-0 md:border-r-0">
      <div className="mb-5 flex items-center justify-between">
        <h2 className="font-semibold">What Chronos is doing</h2>
        <button onClick={onClose} className="rounded-lg p-2 hover:bg-stone-100 dark:hover:bg-stone-700"><Icon name="x" /></button>
      </div>
      {["Started working on this", "Pulled 7 things remembered about Novatech and your ICP", "Searching the web for matching companies", "Read Mercury's careers page", "Saved draft for Mercury", "Reading Vanta's mid-market positioning"].map((item, index) => (
        <div key={item} className="flex gap-3 border-l border-stone-200 pb-4 pl-4 dark:border-stone-700">
          <span className={`-ml-[21px] mt-1 h-3 w-3 rounded-full border-2 border-white ${index === 5 ? "animate-pulse bg-[#d97835]" : "bg-stone-300"} dark:border-stone-800`} />
          <div>
            <p className="text-sm">{item}</p>
            <p className="text-xs text-stone-500">10:42 AM</p>
          </div>
        </div>
      ))}
    </aside>
  );
}

function ActivityScreen({ onAudit }: { onAudit: () => void }) {
  const [mode, setMode] = useState<ActivityMode>("jobs");
  return (
    <Page title="Activity" subtitle="Everything Chronos has done: your jobs and the individual steps inside them." action={
      <div className="rounded-xl border border-stone-200 bg-white p-1 dark:border-stone-700 dark:bg-stone-800">
        {(["jobs", "actions"] as ActivityMode[]).map((item) => <button key={item} onClick={() => setMode(item)} className={`rounded-lg px-3 py-2 text-sm font-medium capitalize ${mode === item ? "bg-stone-100 dark:bg-stone-700" : "text-stone-500"}`}>{item === "actions" ? "Every action" : "Jobs"}</button>)}
      </div>
    }>
      {mode === "jobs" ? <JobsList /> : <ActionsList onAudit={onAudit} />}
    </Page>
  );
}

function JobsList() {
  return <div className="space-y-3">{jobs.map((job) => <Surface key={job.title}><div className="flex items-center gap-4"><Status status={job.status} /><div className="min-w-0 flex-1"><h3 className="font-medium">{job.title}</h3><p className="mt-1 text-sm text-stone-500">{job.status} · {job.detail} · by {job.by} · {job.when}</p></div></div></Surface>)}</div>;
}

function ActionsList({ onAudit }: { onAudit: () => void }) {
  return <div className="max-w-3xl space-y-3">{actions.map(([time, who, what]) => <Surface key={`${time}-${what}`}><p className="text-sm"><span className="font-medium">{who}</span> · {what}</p><p className="mt-1 text-xs text-stone-500">{time}</p></Surface>)}<button onClick={onAudit} className="w-full rounded-xl py-3 text-sm text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800">Older entries are in Settings → Audit log</button></div>;
}

function ApprovalsScreen() {
  const [activeApproval, setActiveApproval] = useState(0);
  const [activeItem, setActiveItem] = useState(0);
  const current = approvals[activeApproval];
  const item = current.items[activeItem];
  return (
    <div className="flex h-full flex-col overflow-auto lg:flex-row lg:overflow-hidden">
      <aside className="shrink-0 border-b border-stone-200 dark:border-stone-700 lg:w-[380px] lg:border-b-0 lg:border-r">
        <div className="p-5"><h1 className="text-lg font-semibold">Approvals</h1><p className="text-sm text-stone-500">{approvals.length} waiting · review one by one</p></div>
        <div className="max-h-64 overflow-auto lg:max-h-none">{approvals.map((approval, index) => <button key={approval.title} onClick={() => { setActiveApproval(index); setActiveItem(0); }} className={`block w-full border-b border-stone-100 p-4 text-left dark:border-stone-700 ${index === activeApproval ? "bg-orange-50 dark:bg-stone-800" : "hover:bg-stone-50 dark:hover:bg-stone-800"}`}><p className="text-sm font-semibold">{approval.from}</p><p className="mt-1 text-sm">{approval.title}</p><p className="mt-1 truncate text-xs text-stone-500">{approval.summary}</p></button>)}</div>
      </aside>
      <section className="min-w-0 flex-1 overflow-auto">
        <div className="border-b border-stone-200 p-8 dark:border-stone-700"><div className="mb-3 flex gap-2"><Pill tone="warn">Waiting on you</Pill><Pill>{current.items.length} items</Pill><span className="text-sm text-stone-500">requested {current.requested}</span></div><h1 className="text-2xl font-semibold tracking-tight">{current.title}</h1><p className="mt-2 max-w-2xl text-stone-500">{current.summary}</p></div>
        <div className="p-8"><Surface><p className="text-sm text-stone-600 dark:text-stone-300">{current.why}</p></Surface></div>
        <div className="grid gap-8 px-4 pb-8 sm:px-8 lg:grid-cols-[220px_1fr]">
          <div className="space-y-1">{current.items.map((draft, index) => <button key={draft.subject} onClick={() => setActiveItem(index)} className={`w-full rounded-lg px-3 py-2 text-left text-sm ${index === activeItem ? "bg-stone-100 font-medium dark:bg-stone-800" : "text-stone-500 hover:bg-stone-50 dark:hover:bg-stone-800"}`}>{String(index + 1).padStart(2, "0")} · {draft.subject}</button>)}</div>
          <Surface><div className="grid grid-cols-[70px_1fr] gap-2 text-sm"><span className="text-stone-500">To</span><span>{item.to}</span><span className="text-stone-500">Subject</span><span className="font-medium">{item.subject}</span></div><hr className="my-5 border-stone-100 dark:border-stone-700" /><p className="whitespace-pre-line text-[15px] leading-7">{item.body}</p><div className="mt-6 flex gap-2"><button className="rounded-lg border border-stone-200 px-3 py-2 text-sm hover:bg-stone-50 dark:border-stone-700 dark:hover:bg-stone-700">Edit</button><button className="rounded-lg border border-stone-200 px-3 py-2 text-sm hover:bg-stone-50 dark:border-stone-700 dark:hover:bg-stone-700">Skip this one</button><button className="rounded-lg bg-emerald-600 px-3 py-2 text-sm font-medium text-white">Approve this one</button></div></Surface>
        </div>
      </section>
    </div>
  );
}

function MemoryScreen() {
  const [filter, setFilter] = useState("All");
  const filtered = filter === "All" ? memories : memories.filter((m) => m.source === filter);
  return <Page title="Memory" subtitle="What Chronos automatically and manually saves so future work gets better." action={<button className="rounded-lg bg-stone-900 px-3 py-2 text-sm font-medium text-white dark:bg-stone-100 dark:text-stone-900">Add memory</button>}><div className="mb-5 flex gap-2">{["All", "Saved by Chronos", "Saved by you"].map((item) => <button key={item} onClick={() => setFilter(item)} className={`rounded-lg px-3 py-2 text-sm ${filter === item ? "bg-stone-100 dark:bg-stone-800" : "text-stone-500"}`}>{item}</button>)}</div><div className="grid gap-3">{filtered.map((memory) => <Surface key={memory.text}><div className="flex items-start gap-3"><Pill>{memory.scope}</Pill><div><p>{memory.text}</p><p className="mt-2 text-sm text-stone-500">{memory.source} · {memory.by}</p></div></div></Surface>)}</div></Page>;
}

function ConnectorsScreen() {
  return <Page title="Connectors" subtitle="The apps Chronos can use for you. Sending and publishing ask first."><div className="grid gap-3 lg:grid-cols-2">{connectors.map(([name, status, desc, identity]) => <Surface key={name}><div className="flex items-start justify-between gap-4"><div><h3 className="font-semibold">{name}</h3><p className="mt-1 text-sm text-stone-500">{desc}</p><p className="mt-3 text-xs text-stone-400">{identity}</p></div><Pill tone={status === "Connected" ? "ok" : "neutral"}>{status}</Pill></div></Surface>)}</div></Page>;
}

function AssistantsScreen() {
  return <Page title="Assistants" subtitle="Choose who Chronos acts as for each kind of work."><div className="grid gap-4 lg:grid-cols-3">{assistants.map((assistant) => <Surface key={assistant.id}><Avatar label={assistant.name} color={assistant.color} /><h3 className="mt-4 font-semibold">{assistant.name}</h3><p className="text-sm text-stone-500">{assistant.role}</p><div className="mt-4 flex flex-wrap gap-2">{assistant.skills.map((skill) => <Pill key={skill}>{skill}</Pill>)}</div><button className="mt-5 rounded-lg border border-stone-200 px-3 py-2 text-sm hover:bg-stone-50 dark:border-stone-700 dark:hover:bg-stone-700">Edit assistant</button></Surface>)}</div></Page>;
}

function SettingsScreen({ tab, setTab, theme, setTheme }: { tab: SettingsTab; setTab: (tab: SettingsTab) => void; theme: "light" | "dark"; setTheme: (theme: "light" | "dark") => void }) {
  return (
    <div className="flex h-full flex-col md:flex-row">
      <aside className="shrink-0 border-b border-stone-200 p-4 dark:border-stone-700 md:w-56 md:border-b-0 md:border-r"><h1 className="mb-3 font-semibold md:mb-4">Settings</h1><div className="flex gap-1 overflow-x-auto md:block md:space-y-0">{(["account", "preferences", "workspace", "notifications", "audit"] as SettingsTab[]).map((item) => <button key={item} onClick={() => setTab(item)} className={`shrink-0 rounded-lg px-3 py-2 text-left text-sm capitalize md:block md:w-full ${tab === item ? "bg-stone-100 font-medium dark:bg-stone-800" : "text-stone-500 hover:bg-stone-50 dark:hover:bg-stone-800"}`}>{item === "audit" ? "Audit log" : item}</button>)}</div></aside>
      <div className="flex-1 overflow-auto"><Page title={tab === "audit" ? "Audit log" : tab[0].toUpperCase() + tab.slice(1)} subtitle={tab === "audit" ? "The full append-only record for admins and compliance." : "Manage your Chronos workspace."}>{tab === "account" && <SettingsCard rows={[["Name", ORG.member.name], ["Email", ORG.member.email], ["Role", ORG.member.role], ["Two-step verification", "On"]]} />}{tab === "preferences" && <div className="space-y-4"><Surface><div className="flex items-center justify-between"><div><h3 className="font-medium">Theme</h3><p className="text-sm text-stone-500">Switches the look across the app.</p></div><div className="rounded-lg bg-stone-100 p-1 dark:bg-stone-800">{(["light", "dark"] as const).map((item) => <button key={item} onClick={() => setTheme(item)} className={`rounded-md px-3 py-1.5 text-sm capitalize ${theme === item ? "bg-white shadow-sm dark:bg-stone-700" : "text-stone-500"}`}>{item}</button>)}</div></div></Surface><SettingsCard rows={[["Language", "English"], ["Auto-save memories", "On"], ["Show technical details by default", "Off"], ["Keyboard shortcut", "Command + K"]]} /></div>}{tab === "workspace" && <SettingsCard rows={[["Workspace name", ORG.name], ["URL", "novatech.cognisiatech.com"], ["Region", "United States"], ["Plan", "Trial"]]} />}{tab === "notifications" && <SettingsCard rows={[["Email approvals", "On"], ["Daily summary", "On"], ["Quiet hours", "9:00 AM - 11:00 AM"], ["Push notifications", "Off"]]} />}{tab === "audit" && <ActionsList onAudit={() => undefined} />}</Page></div>
    </div>
  );
}

function SettingsCard({ rows }: { rows: string[][] }) {
  return <Surface>{rows.map(([label, value], index) => <div key={label} className={`flex items-center justify-between py-3 ${index < rows.length - 1 ? "border-b border-stone-100 dark:border-stone-700" : ""}`}><span className="text-sm font-medium">{label}</span><span className="text-sm text-stone-500">{value}</span></div>)}</Surface>;
}

function Page({ title, subtitle, action, children }: { title: string; subtitle?: string; action?: ReactNode; children: ReactNode }) {
  return <div className="h-full overflow-auto"><header className="flex flex-col items-start justify-between gap-4 px-4 pb-6 pt-7 sm:px-8 md:flex-row md:px-10 md:pt-9"><div><h1 className="text-2xl font-semibold tracking-tight">{title}</h1>{subtitle && <p className="mt-1.5 text-sm text-stone-500">{subtitle}</p>}</div>{action}</header><div className="px-4 pb-10 sm:px-8 md:px-10">{children}</div></div>;
}

function Surface({ children }: { children: ReactNode }) {
  return <div className="rounded-2xl border border-stone-200 bg-white p-5 shadow-sm dark:border-stone-700 dark:bg-stone-800">{children}</div>;
}

function Status({ status }: { status: string }) {
  const tone = status === "Working" ? "bg-[#d97835] animate-pulse" : status === "Waiting on you" ? "bg-amber-500" : status === "Done" ? "bg-emerald-600" : "bg-red-500";
  return <span className={`h-3 w-3 rounded-full ${tone}`} />;
}
