import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("tasks page redirects to activity", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  const controlsSrc = fs.readFileSync(path.join(process.cwd(), "lib/task-controls.ts"), "utf8");
  expect(pageSrc).not.toContain('route === "tasks"');
  expect(pageSrc).not.toContain('{ id: "tasks"      as Route');
  expect(pageSrc).toContain("ActivityScreen");
  expect(controlsSrc).toContain("/tasks/${encodeURIComponent(taskId)}/${action}");
  expect(pageSrc).toContain('"dead_letter"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/tasks/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity?tab=tasks")');
});

test("audit screen: static route reusing the audit log viewer", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain('pathname === "/audit"');
  expect(pageSrc).toContain('setSettingsTab("audit")');
  expect(pageSrc).toContain('id: "audit", label: "Audit logs"');
  expect(pageSrc).toContain('{route === "audit"      && <AuditScreen canExport={canAdmin} />}');
  expect(pageSrc).toContain('{tab === "audit" && <AuditSettings canExport={canAdmin} />}');
  expect(pageSrc).not.toContain('{ id: "audit",      icon: <IC.Audit');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/audit/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');
});

test("settings navigation keeps one bounded mobile scroll region", () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("max-h-[34vh] flex flex-col flex-shrink-0");
  expect(pageSrc).toContain('aria-label="Settings sections" className="px-3 space-y-0.5 flex-1 min-h-0 overflow-y-auto');
  expect(pageSrc).not.toContain("max-h-[34vh] flex-shrink-0 border-b hairline py-3 overflow-y-auto");
});

test("approval and audit empty controls remain unambiguous", () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain('!active && approvals.length === 0');
  expect(pageSrc.match(/title="All caught up"/g)).toHaveLength(1);
  expect(pageSrc).toContain('placeholder="Search audit logs"');
});

test("datasets live in Projects; chat documents stay in settings", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  // Dataset management (DataScreen) renders as a tab inside project detail.
  expect(pageSrc).toContain("<DataScreen");
  expect(pageSrc).not.toContain('route === "data"');
  // Chat document uploads remain a settings tab.
  expect(pageSrc).toContain("AccountDataSettings");
  expect(pageSrc).toContain('id: "data", label: "Data"');
  expect(pageSrc).toContain("Upload documents in chat");

  // The standalone /data route redirects into Projects.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/data/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/projects")');
});
