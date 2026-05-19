"use client";

import { FormEvent, useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type Conversation = {
  id: string;
  title: string | null;
  updated_at: string;
};

type Message = {
  id?: string;
  role: "user" | "assistant" | "system";
  content: string;
};

export default function ChatPage() {
  const [token, setToken] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem("chronos_token") ?? "";
    setToken(stored);
  }, []);

  useEffect(() => {
    if (!token) return;
    void loadConversations();
  }, [token]);

  async function authed(path: string, init: RequestInit = {}) {
    return fetch(`${API_BASE}${path}`, {
      ...init,
      headers: {
        ...(init.headers ?? {}),
        Authorization: `Bearer ${token}`,
      },
    });
  }

  async function loadConversations() {
    const res = await authed("/chat/conversations");
    if (res.ok) setConversations(await res.json());
  }

  async function loadMessages(id: string) {
    setConversationId(id);
    const res = await authed(`/chat/conversations/${id}/messages`);
    if (res.ok) setMessages(await res.json());
  }

  async function sendMessage(event: FormEvent) {
    event.preventDefault();
    if (!draft.trim() || streaming) return;
    const userText = draft.trim();
    setDraft("");
    setMessages((prev) => [...prev, { role: "user", content: userText }, { role: "assistant", content: "" }]);
    setStreaming(true);

    const res = await authed("/chat/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: userText, conversation_id: conversationId }),
    });
    if (!res.body) {
      setStreaming(false);
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";
      for (const raw of events) {
        if (!raw.startsWith("data: ")) continue;
        const eventData = JSON.parse(raw.slice(6));
        if (eventData.type === "conversation") setConversationId(eventData.conversation_id);
        if (eventData.type === "token") {
          setMessages((prev) => {
            const copy = [...prev];
            const last = copy[copy.length - 1];
            copy[copy.length - 1] = { ...last, content: `${last.content}${eventData.content}` };
            return copy;
          });
        }
      }
    }
    setStreaming(false);
    await loadConversations();
  }

  return (
    <main className="grid min-h-screen grid-cols-[280px_1fr] bg-[#f6f7f9] text-[#15171a]">
      <aside className="border-r border-[#d9dee7] bg-white p-4">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold">Chronos</h1>
          <button
            className="rounded-md border border-[#c9ced6] px-3 py-1.5 text-sm"
            onClick={() => {
              setConversationId(null);
              setMessages([]);
            }}
          >
            New
          </button>
        </div>
        <nav className="mt-6 space-y-2">
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              className={`block w-full rounded-md px-3 py-2 text-left text-sm ${
                conversation.id === conversationId ? "bg-[#e8edf5]" : "hover:bg-[#f0f2f5]"
              }`}
              onClick={() => loadMessages(conversation.id)}
            >
              {conversation.title ?? "Untitled conversation"}
            </button>
          ))}
        </nav>
      </aside>
      <section className="flex min-h-screen flex-col">
        <div className="border-b border-[#d9dee7] bg-white px-6 py-4">
          <h2 className="text-base font-semibold">Chat</h2>
          <p className="text-sm text-[#667085]">Sprint 1 streaming, persistence, context, and audit path.</p>
        </div>
        <div className="flex-1 space-y-4 overflow-auto p-6">
          {messages.map((message, index) => (
            <article
              key={`${message.role}-${index}`}
              className={`max-w-3xl rounded-md border px-4 py-3 text-sm leading-6 ${
                message.role === "user"
                  ? "ml-auto border-[#b9c4d4] bg-white"
                  : "border-[#d0d7e2] bg-[#eef3f8]"
              }`}
            >
              <div className="mb-1 text-xs font-medium uppercase tracking-normal text-[#667085]">{message.role}</div>
              {message.content}
            </article>
          ))}
        </div>
        <form onSubmit={sendMessage} className="border-t border-[#d9dee7] bg-white p-4">
          <div className="mx-auto flex max-w-4xl gap-3">
            <textarea
              className="min-h-12 flex-1 resize-none rounded-md border border-[#c9ced6] px-3 py-2 text-sm outline-none focus:border-[#1f6feb]"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Ask Chronos..."
            />
            <button className="rounded-md bg-[#15171a] px-4 py-2 text-sm font-medium text-white" disabled={streaming}>
              Send
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}
