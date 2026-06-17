import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("agent menu and command: static route and API guard", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<AgentMenuModal");
  expect(pageSrc).toContain("<AgentsScreen />");
  expect(pageSrc).toContain('startsWith("/agent")');
  expect(pageSrc).toContain('apiFetch("/agents/command"');
  expect(pageSrc).toContain("Agents");

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/agents/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/chat")');

  const assistantsRouteSrc = fs.readFileSync(path.join(process.cwd(), "app/assistants/page.tsx"), "utf8");
  expect(assistantsRouteSrc).toContain('redirect("/chat")');

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
  expect(componentSrc).toContain('profile_kind: "agent"');
});
