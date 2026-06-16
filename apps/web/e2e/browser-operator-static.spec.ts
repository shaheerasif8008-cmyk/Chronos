import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("browser operator: live feed in chat, full session view in Activity", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  // The in-chat live feed stays.
  expect(pageSrc).toContain("LiveOperationsDrawer");
  expect(pageSrc).toContain("/browser-sessions/");
  expect(pageSrc).toContain("Live browser feed");
  expect(pageSrc).toContain("Take over in chat");
  // The full operator view now renders as a tab inside the Activity surface,
  // driven by Activity's local tab state rather than a top-level route.
  expect(pageSrc).toContain("<BrowserOperatorScreen");
  expect(pageSrc).not.toContain('route === "browser"');
  expect(pageSrc).not.toContain('{ id: "browser"    as Route');

  // The standalone /browser route redirects into Activity.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/browser/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity")');
});
