import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");

function source(relativePath: string): string {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("runtime settings expose only enforced organization guardrails as editable", () => {
  const page = source("app/chat/page.tsx");

  expect(page).toContain('title="Enforced organization guardrails"');
  expect(page).toContain('ariaLabel="Max task queue size"');
  expect(page).toContain('ariaLabel="Token budget"');
  expect(page).toContain('ariaLabel="Daily model cost budget"');
  expect(page).toContain('ariaLabel="Request rate per minute"');
  expect(page).toContain('ariaLabel="Connector rate per minute"');

  expect(page).toContain('title="Deployment-owned runtime configuration"');
  expect(page).not.toContain('ariaLabel="Runtime mode"');
  expect(page).not.toContain('ariaLabel="Heartbeat interval"');
  expect(page).not.toContain('ariaLabel="Log retention"');
  expect(page).not.toContain('ariaLabel="Failure recovery"');
  expect(page).toContain('title="Enforced runtime limit"');
  expect(page).toContain('ariaLabel="Max concurrent runtimes"');
  expect(page).not.toContain('ariaLabel="Runtime idle timeout"');
  expect(page).not.toContain('ariaLabel="Max sub-agent depth"');
  expect(page).not.toContain('label="Sub-agent spawning" checked=');
});

test("retention UI supports audited dry runs, execution, and legal holds", () => {
  const page = source("app/chat/page.tsx");

  expect(page).toContain("RetentionSettings");
  expect(page).toContain('apiFetch("/settings/retention/run"');
  expect(page).toContain('apiFetch("/settings/retention/holds"');
  expect(page).toContain('required: "RUN RETENTION"');
  expect(page).toContain("pinned memories and active legal holds are excluded");
  expect(page).toContain("Failed object deletions keep metadata for retry");
});
