import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("coding work is handled in chat without a separate coding surface", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).not.toContain("CodingAgentScreen");
  expect(pageSrc).not.toContain('route === "coding"');
  expect(pageSrc).not.toContain('pathname === "/coding"');
  // No coding nav item, tab, or route exists.
  expect(pageSrc).not.toContain('label: "Coding"');
  expect(pageSrc).not.toContain('id: "coding"');

  // The /coding route and its component were removed entirely.
  expect(fs.existsSync(path.join(process.cwd(), "app/coding/page.tsx"))).toBe(false);
  expect(fs.existsSync(path.join(process.cwd(), "components/coding/CodingAgentScreen.tsx"))).toBe(false);
});
