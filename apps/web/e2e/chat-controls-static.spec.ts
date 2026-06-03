import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("message convert-to-task action targets the implemented API route", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain("/convert-to-task");
  expect(source).toContain("/convert-to-workflow");
  expect(source).not.toContain("/convert-task");
  expect(source).not.toContain("/convert-workflow");
});

test("message menu exposes regenerate and retry-from-here actions", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain("/regenerate");
  expect(source).toContain("/retry-from-here");
  expect(source).toContain("Regenerate");
  expect(source).toContain("Retry from here");
});
