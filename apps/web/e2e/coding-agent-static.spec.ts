import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("coding agent screen: static route and shell integration", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<CodingAgentScreen");
  expect(pageSrc).toContain('route === "coding"');
  expect(pageSrc).toContain('pathname === "/coding"');
  expect(pageSrc).toContain('label: "Coding"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/coding/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');

  const componentSrc = fs.readFileSync(
    path.join(process.cwd(), "components/coding/CodingAgentScreen.tsx"),
    "utf8",
  );
  expect(componentSrc).toContain("data-testid=\"coding-agent-workspace\"");
  expect(componentSrc).toContain("Clone/import");
  expect(componentSrc).toContain("Diff viewer");
  expect(componentSrc).toContain("Approval-gated PR");
  expect(componentSrc).toContain("repo.clone");
  expect(componentSrc).toContain("repo.review");
});
