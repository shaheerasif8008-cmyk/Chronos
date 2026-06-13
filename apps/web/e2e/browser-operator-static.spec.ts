import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("browser operator: visible live feed is in chat, standalone page redirects", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("LiveOperationsDrawer");
  expect(pageSrc).toContain("/browser-sessions/");
  expect(pageSrc).toContain("Live browser feed");
  expect(pageSrc).toContain("Take over in chat");
  expect(pageSrc).not.toContain("<BrowserOperatorScreen");
  expect(pageSrc).not.toContain('route === "browser"');
  expect(pageSrc).not.toContain('{ id: "browser"    as Route');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/browser/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/chat")');
});
