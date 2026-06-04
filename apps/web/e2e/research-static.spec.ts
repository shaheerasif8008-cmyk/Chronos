import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("research screen: static source guard", async () => {
  // Verify page.tsx wires ResearchScreen and the route check
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  expect(pageSrc).toContain("<ResearchScreen");
  expect(pageSrc).toContain('route === "research"');

  // Verify ResearchScreen contains required literals
  const componentSrc = fs.readFileSync(
    path.join(process.cwd(), "components/research/ResearchScreen.tsx"),
    "utf8"
  );
  expect(componentSrc).toContain("/research/");
  expect(componentSrc).toContain("Start research");
  expect(componentSrc).toContain("Open report");
});
