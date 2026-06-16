import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("tasks page redirects to activity", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).not.toContain('route === "tasks"');
  expect(pageSrc).not.toContain('{ id: "tasks"      as Route');
  expect(pageSrc).toContain("ActivityScreen");
  expect(pageSrc).toContain("/tasks/${taskId}/${action}");
  expect(pageSrc).toContain('"dead_letter"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/tasks/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity")');
});

test("audit screen: static route reusing the audit log viewer", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<AuditScreen");
  expect(pageSrc).toContain('route === "audit"');
  expect(pageSrc).toContain('pathname === "/audit"');
  expect(pageSrc).toContain('label: "Audit"');
  expect(pageSrc).toContain("function AuditScreen");
  expect(pageSrc).toContain("<AuditSettings/>");

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/audit/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');
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
