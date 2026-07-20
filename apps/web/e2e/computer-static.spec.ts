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

  // The standalone /computer route deep-links the matching Activity tab.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/computer/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity?tab=computer")');
});

test("computer: cloud desktop is consented, resumable, controllable, and destroyable", async () => {
  const screenSrc = fs.readFileSync(path.join(process.cwd(), "components/computer/ComputerScreen.tsx"), "utf8");
  const liveSrc = fs.readFileSync(path.join(process.cwd(), "components/computer/ComputerLiveView.tsx"), "utf8");
  const apiSrc = fs.readFileSync(path.join(process.cwd(), "../api/routers/computer_sessions.py"), "utf8");

  expect(screenSrc).toContain("confirmed_by_user: true");
  expect(screenSrc).toContain("capabilities: sessionCapabilities");
  expect(screenSrc).toContain("expires_at:");
  expect(screenSrc).toContain("/screenshot");
  expect(screenSrc).toContain("/input");
  expect(screenSrc).toContain('sessionAction("pause")');
  expect(screenSrc).toContain('sessionAction("resume")');
  expect(screenSrc).toContain('sessionAction("cancel")');
  expect(screenSrc).toContain("Unexported files and desktop state will be permanently deleted");
  expect(screenSrc).not.toContain("sandbox is headless");

  expect(liveSrc).toContain("/computer-sessions/${active.id}/screenshot");
  expect(liveSrc).not.toContain("/desktop-sessions/");
  expect(apiSrc).toContain('"computer.input"');
  expect(apiSrc).toContain('"computer.cancel_session"');
});
