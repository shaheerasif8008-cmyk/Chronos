import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("top-level tasks page is not exposed as a standalone surface", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(pageSrc).not.toContain('pathname === "/tasks"');
  expect(pageSrc).not.toContain('route === "tasks"');
  expect(pageSrc).not.toContain('{ id: "tasks"      as Route');

  expect(pageSrc).toContain('apiFetch("/tasks/")');
  expect(pageSrc).toContain('{ id: "tasks",    label: "Tasks" }');
});
