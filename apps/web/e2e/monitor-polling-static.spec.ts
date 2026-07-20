import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

const root = path.resolve(__dirname, "..");
const source = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("monitor setup exposes real website, news, inbox, and connector sources", () => {
  const chat = source("app/chat/page.tsx");

  expect(chat).toContain('<option value="website">Website</option>');
  expect(chat).toContain('<option value="news">News and search</option>');
  expect(chat).toContain('<option value="inbox">Inbox</option>');
  expect(chat).toContain('<option value="connector">Connector</option>');
  expect(chat).toContain('aria-label="Monitor connector tool"');
  expect(chat).toContain("interval_seconds: Number(monitorInterval)");
  expect(chat).toContain('status: "active"');
});

test("monitor operations expose durable health, controls, and evidence history", () => {
  const chat = source("app/chat/page.tsx");

  expect(chat).toContain('apiFetch(`/monitors/${id}/run`');
  expect(chat).toContain('apiFetch(`/monitors/${id}/runs`)');
  expect(chat).toContain("monitor.last_run_status");
  expect(chat).toContain("monitor.next_run_at");
  expect(chat).toContain("monitor.consecutive_failures");
  expect(chat).toContain("monitor.last_error_code");
  expect(chat).toContain('aria-expanded={expandedMonitor === monitor.id}');
  expect(chat).toContain("Monitor alerts");
  expect(chat).toContain("Pause");
  expect(chat).toContain("Resume");
});

test("monitor UI does not synthesize observations or alerts", () => {
  const chat = source("app/chat/page.tsx");

  expect(chat).not.toContain("hash: String(Date.now())");
  expect(chat).not.toContain("`${monitor.name} changed`");
  expect(chat).not.toContain('/evaluate", { method: "POST", body: JSON.stringify({ observed:');
  expect(chat).toContain("first real observation");
});
