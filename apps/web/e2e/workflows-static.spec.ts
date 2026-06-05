import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("workflows screen: static route and phase 12 API guard", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<WorkflowsScreen");
  expect(pageSrc).toContain('route === "workflows"');
  expect(pageSrc).toContain('pathname === "/workflows"');
  expect(pageSrc).toContain('label: "Workflows"');
  expect(pageSrc).not.toContain('route === "workflows"  && <EmptyPanel label="Workflows" />');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/workflows/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');

  expect(pageSrc).toContain("/schedules/");
  expect(pageSrc).toContain("/workflows/");
  expect(pageSrc).toContain("/workflows/runs");
  expect(pageSrc).toContain("/workflows/triggers");
  expect(pageSrc).toContain("/monitors/");
  expect(pageSrc).toContain("/monitors/alerts");
  expect(pageSrc).toContain('data-testid="phase12-schedules"');
  expect(pageSrc).toContain('data-testid="phase12-workflow-runs"');
  expect(pageSrc).toContain('data-testid="phase12-monitors"');
  expect(pageSrc).toContain("Run history");
  expect(pageSrc).toContain("Monitor alerts");
});
