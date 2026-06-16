import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("computer: live status in chat, full session view in Activity", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  // The in-chat live status stays.
  expect(pageSrc).toContain("LiveOperationsDrawer");
  expect(pageSrc).toContain("/computer-sessions/");
  expect(pageSrc).toContain("Virtual computer");
  // The full computer view now renders as a tab inside the Activity surface,
  // driven by Activity's local tab state rather than a top-level route.
  expect(pageSrc).toContain("<ComputerScreen");
  expect(pageSrc).not.toContain('route === "computer"');
  expect(pageSrc).not.toContain('{ id: "computer"   as Route');

  // The standalone /computer route redirects into Activity.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/computer/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity")');
});
