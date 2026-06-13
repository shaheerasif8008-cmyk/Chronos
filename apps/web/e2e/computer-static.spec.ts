import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("computer: virtual computer status is in chat, standalone page redirects", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("LiveOperationsDrawer");
  expect(pageSrc).toContain("/computer-sessions/");
  expect(pageSrc).toContain("Virtual computer");
  expect(pageSrc).not.toContain("<ComputerScreen");
  expect(pageSrc).not.toContain('route === "computer"');
  expect(pageSrc).not.toContain('{ id: "computer"   as Route');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/computer/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/chat")');
});
