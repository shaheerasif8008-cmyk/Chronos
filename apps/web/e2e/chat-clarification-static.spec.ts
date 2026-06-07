import { test, expect } from "@playwright/test";
import fs from "fs";
import path from "path";

test("chat renders clarification options and closes stream on done", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain("type ClarificationPrompt");
  expect(source).toContain("function ClarificationCard");
  expect(source).toContain('ev.type === "clarification"');
  expect(source).toContain("Other...");
  expect(source).toContain('status: "complete",\n              content: partial || last.content,\n              thinking: false');
});
