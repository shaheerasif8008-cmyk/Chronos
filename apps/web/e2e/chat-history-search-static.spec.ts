import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("global search spans workspace content and opens matching conversations", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain("Search conversations, tasks, artifacts, memory, and sources…");
  expect(source).toContain('aria-label="Search all Chronos content"');
  expect(source).toContain("/search?q=${q}");
  expect(source).toContain("const conversationId = new URL(result.url, window.location.origin).searchParams.get(\"c\")");
  expect(source).toContain("onConvoSelected(conversationId)");
  expect(source).toContain("new URLSearchParams(window.location.search).get(\"c\")");
  expect(source).toContain("router.push(`/chat?c=${encodeURIComponent(id)}`)");
});
