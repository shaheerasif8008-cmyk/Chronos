import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("research is chat functionality, not a standalone page", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).not.toContain("<ResearchScreen");
  expect(pageSrc).not.toContain('route === "research"');
  expect(pageSrc).toContain('{ id: "research", label: "Research" }');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/research/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/chat")');
});
