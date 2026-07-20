import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("agent menu and command: static route and API guard", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<AgentMenuModal");
  expect(pageSrc).toContain("<AgentsScreen memberRole={memberRole} />");
  expect(pageSrc).toContain('startsWith("/agent")');
  expect(pageSrc).toContain('apiFetch("/agents/command"');
  expect(pageSrc).toContain("Agents");

  // Standalone agents/assistants pages reuse the governed workspace shell and
  // preserve which tab the deep link requested.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/agents/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');

  const assistantsRouteSrc = fs.readFileSync(path.join(process.cwd(), "app/assistants/page.tsx"), "utf8");
  expect(assistantsRouteSrc).toContain('export { default } from "../chat/page"');
  expect(pageSrc).toContain('pathname === "/assistants" || pathname === "/agents"');
  expect(pageSrc).toContain('pathname === "/agents" ? "agents" : "assistants"');

  const componentSrc = fs.readFileSync(
    path.join(process.cwd(), "components/agents/AgentsScreen.tsx"),
    "utf8",
  );
  expect(componentSrc).toContain("/agents/templates");
  expect(componentSrc).toContain("/agents/${selected.id}/run");
  expect(componentSrc).toContain("<AgentPublicationsPanel");
  expect(componentSrc).toContain("data-testid=\"agents-screen\"");
  expect(componentSrc).toContain("data-testid=\"agent-template\"");
  expect(componentSrc).toContain("data-testid=\"agent-publishing-panel\"");
  // The cluttered builder form was replaced by a one-click ready-to-use catalog.
  expect(componentSrc).toContain("Use agent");
  expect(componentSrc).toContain('profile_kind: "agent"');

  const publicationSrc = fs.readFileSync(
    path.join(process.cwd(), "components/agents/AgentPublicationsPanel.tsx"),
    "utf8",
  );
  expect(publicationSrc).toContain("/agents/${agent.id}/publications");
  expect(publicationSrc).toContain("/agents/publication-bindings");
  expect(publicationSrc).toContain("/lifecycle");
  expect(publicationSrc).toContain("plaintext_secret");
  expect(publicationSrc).toContain("organization API key with write scope");
  expect(publicationSrc).toContain('data-testid="agent-publication-admin"');
});
