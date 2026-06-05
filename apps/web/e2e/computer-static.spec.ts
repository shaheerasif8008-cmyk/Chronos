import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("computer screen: static route and API guard", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<ComputerScreen");
  expect(pageSrc).toContain('route === "computer"');
  expect(pageSrc).toContain('pathname === "/computer"');
  expect(pageSrc).toContain('label: "Computer"');

  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/computer/page.tsx"), "utf8");
  expect(routeSrc).toContain('export { default } from "../chat/page"');

  const componentSrc = fs.readFileSync(
    path.join(process.cwd(), "components/computer/ComputerScreen.tsx"),
    "utf8",
  );
  expect(componentSrc).toContain("/computer-sessions/");
  expect(componentSrc).toContain("data-testid=\"computer-viewport\"");
  expect(componentSrc).toContain("local-grant-row");
  expect(componentSrc).toContain("New cloud computer");
});
