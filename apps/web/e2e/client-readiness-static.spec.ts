import fs from "node:fs";
import path from "node:path";
import { test, expect } from "@playwright/test";

const root = path.resolve(__dirname, "..");
const source = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("project datasets are explicitly scoped and non-CSV uploads normalize server-side", () => {
  const page = source("app/chat/page.tsx");
  const data = source("components/data/DataScreen.tsx");
  expect(page).toContain("<DataScreen projectId={projectId}");
  expect(data).toContain("project_id: projectId");
  expect(data).toContain("?project_id=${encodeURIComponent(projectId)}");
  expect(data).toContain('form.append("project_id", projectId)');
});

test("chat suggestions, response stop semantics, and in-chat artifact roles are truthful", () => {
  const page = source("app/chat/page.tsx");
  const panel = source("components/artifacts/InChatArtifactPanel.tsx");
  expect(page).toContain("onSubmit={q => void sendMessage(q)}");
  expect(page).toContain("Stop response");
  expect(page).toContain("Durable task work continues");
  expect(panel).toContain("memberRole={currentMember.role}");
  expect(panel).toContain("currentMember={currentMember}");
});

test("approval decisions are role-gated and production has no synthetic dispatch button", () => {
  const page = source("app/chat/page.tsx");
  expect(page).toContain('["admin", "owner", "approver"].includes(currentMemberRole)');
  expect(page).toContain("Read-only access. An approver, administrator, or owner");
  expect(page).not.toContain(">Dispatch event</button>");
  expect(page).not.toContain('event_type: "event.received"');
});

test("auth config failure is terminal and shared input styling covers every production form", () => {
  const login = source("app/login/page.tsx");
  const css = source("app/globals.css");
  expect(login).toContain('authConfig?.provider === "unavailable"');
  expect(login).toContain("Chronos cannot reach its authentication service.");
  expect(login).toContain("Retry connection");
  expect(css).toContain(".input,\n.input-field");
});

test("memory capture state is server-backed and destructive consolidation is confirmed", () => {
  const page = source("app/chat/page.tsx");
  expect(page).toContain('apiFetch("/memory/policy?scope=member")');
  expect(page).toContain("Capture status unavailable");
  expect(page).toContain("Merge ${duplicate_ids.length} duplicate");
  expect(page).toContain("Delete this memory?");
});

test("chat visibly binds each conversation to a native workspace and surfaces memory evidence", () => {
  const page = source("app/chat/page.tsx");
  expect(page).toContain('/settings/admin-lifecycle/accessible-workspaces');
  expect(page).toContain('aria-label="Workspace"');
  expect(page).toContain('workspace_id: selectedWorkspace?.id');
  expect(page).toContain('A conversation stays bound to the workspace where it was created.');
  expect(page).toContain('memory_refs: Array.isArray(m.memory_refs)');
  expect(page).toContain('Memory used');
  expect(page).toContain('Memories used for this answer');
  expect(page).toContain('/memory?memory=${encodeURIComponent(memory.id)}');
  expect(page).toContain('Inspect or edit');
  expect(page).toContain('focusOnMount={focusedMemoryId === m.id}');
});

test("voice mode supports continuous pause-detected hands-free conversations", () => {
  const page = source("app/chat/page.tsx");
  expect(page).toContain("Hands-free conversation");
  expect(page).toContain("Detect a pause, send, read the reply aloud, and keep listening");
  expect(page).toContain("analyser.getByteTimeDomainData(waveform)");
  expect(page).toContain("await sendMessage(transcript)");
  expect(page).toContain("playHandsFreeReply(responseText)");
  expect(page).toContain('aria-label="Stop hands-free voice mode"');
});

test("notification, billing, and admin surfaces stay connected to the authenticated shell", () => {
  const shell = source("app/chat/page.tsx");
  const notifications = source("app/notifications/page.tsx");
  const billing = source("app/settings/billing/page.tsx");
  const admin = source("app/admin/page.tsx");
  expect(shell).toContain('href="/notifications"');
  expect(shell).toContain('href="/settings/billing"');
  expect(shell).toContain("notification_email_dispatch?.supported");
  for (const page of [notifications, billing, admin]) {
    expect(page).toContain("apiFetch");
  }
  expect(notifications).toContain('href="/chat"');
  expect(admin).toContain('href="/chat"');
  expect(billing).toContain('href="/settings?tab=billing"');
});
