import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("browser operator screen: static route and API guard", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<BrowserOperatorScreen");
  expect(pageSrc).toContain('route === "browser"');
  expect(pageSrc).toContain('pathname === "/browser"');
  expect(pageSrc).toContain('label: "Browser"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/browser/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');

  const componentSrc = fs.readFileSync(
    path.join(process.cwd(), "components/browser/BrowserOperatorScreen.tsx"),
    "utf8",
  );
  expect(componentSrc).toContain("/browser-sessions/");
  expect(componentSrc).toContain("data-testid=\"browser-viewport\"");
  expect(componentSrc).toContain("Takeover requested");
  expect(componentSrc).toContain("Sensitive Approvals");
});
