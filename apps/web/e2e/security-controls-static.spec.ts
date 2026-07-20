import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

const root = path.resolve(__dirname, "..");
const source = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("admin file quarantine is metadata-only and cannot restore bytes", () => {
  const admin = source("app/admin/page.tsx");
  const panel = source("components/settings/FileQuarantinePanel.tsx");

  expect(admin).toContain("<FileQuarantinePanel />");
  expect(panel).toContain("Metadata-only review");
  expect(panel).toContain("cannot be restored");
  expect(panel).toContain("MARK FALSE POSITIVE");
  expect(panel).toContain('review("acknowledged")');
  expect(panel).toContain('review("closed")');
  expect(panel).not.toContain("Download original");
  expect(panel).not.toContain("Restore file");
});

test("cloud computer consent binds network access to exact domains", () => {
  const computer = source("components/computer/ComputerScreen.tsx");

  expect(computer).toContain("Allowlisted network access");
  expect(computer).toContain("Allowed egress domains");
  expect(computer).toContain("allowed_egress_domains");
  expect(computer).toContain("Exact domains only");
  expect(computer).toContain("organization-approved domain");
});
