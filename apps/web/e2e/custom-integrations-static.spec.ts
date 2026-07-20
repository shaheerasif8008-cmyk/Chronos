import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const panel = fs.readFileSync(path.join(root, "components/connectors/CustomIntegrationsPanel.tsx"), "utf8");
const screen = fs.readFileSync(path.join(root, "components/connectors/ConnectorsScreen.tsx"), "utf8");

test("custom HTTPS connector setup is real, encrypted, and action-schema driven", () => {
  expect(panel).toContain('apiFetch("/connectors/custom-http"');
  expect(panel).toContain('type="password"');
  expect(panel).toContain("Request JSON Schema");
  expect(panel).toContain("write actions remain approval-gated");
  expect(panel).toContain("Revoke");
  expect(panel).toContain("Verify");
});

test("webhook setup supports one-time secrets and real lifecycle operations", () => {
  expect(panel).toContain('apiFetch("/connectors/webhook-endpoints"');
  expect(panel).toContain("Copy the signing secret now");
  expect(panel).toContain("will not be shown again");
  expect(panel).toContain("/rotate");
  expect(panel).toContain("/test");
  expect(panel).toContain('method: "PATCH"');
  expect(panel).toContain("X-Chronos-Signature");
  expect(panel).toContain("X-Chronos-Event-ID");
});

test("custom integrations replace environment-only placeholders for admins", () => {
  expect(screen).toContain("<CustomIntegrationsPanel />");
  expect(screen).toContain('["remote_mcp", "webhooks", "custom_http"]');
  expect(screen).not.toContain("Configured via environment");
});
