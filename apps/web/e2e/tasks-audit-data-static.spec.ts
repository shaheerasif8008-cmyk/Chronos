import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("tasks screen: static route and management actions", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<TasksScreen");
  expect(pageSrc).toContain('route === "tasks"');
  expect(pageSrc).toContain('pathname === "/tasks"');
  expect(pageSrc).toContain('label: "Tasks"');
  expect(pageSrc).toContain("function TasksScreen");
  expect(pageSrc).toContain("/tasks/${taskId}/${action}");
  expect(pageSrc).toContain('"dead_letter"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/tasks/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');
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

test("data screen: rendered inside the app shell", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<DataScreen");
  expect(pageSrc).toContain('route === "data"');
  expect(pageSrc).toContain('pathname === "/data"');
  expect(pageSrc).toContain('label: "Data"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/data/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');
});
