import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("research lives in Activity and Projects, not a top-level route", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  // Research renders as a tab inside the Activity surface (and as a Projects
  // tab), driven by local tab state rather than a top-level route.
  expect(pageSrc).toContain("<ResearchScreen");
  expect(pageSrc).not.toContain('route === "research"');
  // Projects keeps a Research tab.
  expect(pageSrc).toContain('{ id: "research", label: "Research" }');

  // The standalone /research route redirects into Activity.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/research/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity")');
});
