import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

test("connector UI distinguishes configured, connected, verified, stale, and degraded", async () => {
  const connectors = fs.readFileSync(
    path.join(process.cwd(), "components/connectors/ConnectorsScreen.tsx"),
    "utf8",
  );
  const settings = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(connectors).toContain("Connected · not verified");
  expect(connectors).toContain("Verification stale");
  expect(connectors).toContain("Verification failed");
  expect(connectors).toContain("healthPresentation(app)");
  expect(connectors).toContain("refresh_health=true");
  expect(connectors).toContain("Refresh status");
  expect(connectors).toContain("aria-live=\"polite\"");
  expect(connectors).toContain("aria-busy={refreshing}");
  expect(connectors).not.toMatch(/app\.connected\s*&&[^\n]+var\(--ok/);

  expect(settings).toContain("Configured means credentials exist. Verified means");
  expect(settings).toContain("item.status === \"verified\" && !item.stale");
  expect(settings).toContain("Last verified");
});
