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

  const operatorSrc = fs.readFileSync(path.join(process.cwd(), "components/browser/BrowserOperatorScreen.tsx"), "utf8");
  expect(operatorSrc).toContain("active?.screenshot_url");
  expect(operatorSrc).toContain("download.download_url");
  expect(operatorSrc).toContain("/live-view");
  expect(operatorSrc).toContain('active.takeover_state === "requested"');
  expect(operatorSrc).not.toContain("download.path");

  // The standalone /browser route deep-links the matching Activity tab.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/browser/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity?tab=browser")');
});
