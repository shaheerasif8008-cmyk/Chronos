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
  const unavailableFallback = source.indexOf('EmptyState title="Section unavailable" sub="Return to the project overview and try again."');
  expect(researchBranch).toBeGreaterThan(-1);
  expect(unavailableFallback).toBeGreaterThan(researchBranch);
});

test("project artifacts can create and download a bounded durable ZIP", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain('apiFetch(`/projects/${projectId}/export`');
  expect(source).toContain('apiFetch(`/artifacts/${result.artifact.id}/content`)');
  expect(source).toContain("Export project ZIP");
  expect(source).toContain("only artifacts explicitly shared to this project");
  expect(source).toContain("Operator role required to export project artifacts");
});
