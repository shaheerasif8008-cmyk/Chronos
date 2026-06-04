import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("project detail research tab renders project-scoped activity", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain('tab === "research"');
  expect(source).toContain("ProjectResearchTab");
  expect(source).toContain("Research activity");
  expect(source).toContain("tasks={tasks}");
  expect(source).toContain("artifacts={artifacts}");
  expect(source).toContain("sources={sources}");

  const researchBranch = source.indexOf('tab === "research"');
  const comingSoonFallback = source.indexOf('EmptyState title="Nothing here yet" sub="This feature is coming soon."');
  expect(researchBranch).toBeGreaterThan(-1);
  expect(comingSoonFallback).toBeGreaterThan(researchBranch);
});
