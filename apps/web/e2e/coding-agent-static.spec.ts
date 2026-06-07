import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("coding work is handled in chat without a separate coding surface", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).not.toContain("CodingAgentScreen");
  expect(pageSrc).not.toContain('route === "coding"');
  expect(pageSrc).not.toContain('pathname === "/coding"');
  expect(pageSrc).toContain('{ id: "coding",   label: "Coding" }');
  expect(pageSrc).not.toContain('id: "coding"     as Route');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/coding/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/chat")');
  expect(fs.existsSync(path.join(process.cwd(), "components/coding/CodingAgentScreen.tsx"))).toBe(false);
});
