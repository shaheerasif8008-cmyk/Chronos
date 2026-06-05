import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("agents screen: static route and API guard", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<AgentsScreen");
  expect(pageSrc).toContain('route === "agents"');
  expect(pageSrc).toContain('pathname === "/agents"');
  expect(pageSrc).toContain('label: "Agents"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/agents/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');

  const componentSrc = fs.readFileSync(
    path.join(process.cwd(), "components/agents/AgentsScreen.tsx"),
    "utf8",
  );
  expect(componentSrc).toContain("/agents/templates");
  expect(componentSrc).toContain("/agents/${selected.id}/run");
  expect(componentSrc).toContain("/agents/${selected.id}/publications");
  expect(componentSrc).toContain("data-testid=\"agents-screen\"");
  expect(componentSrc).toContain("data-testid=\"agent-template\"");
  expect(componentSrc).toContain("data-testid=\"agent-publishing-panel\"");
  expect(componentSrc).toContain("Schedule permission");
});
