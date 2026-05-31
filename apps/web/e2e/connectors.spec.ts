import { test, expect } from "@playwright/test";

// Connector directory UI. The real "connect" path is OAuth2 (can't be driven in
// E2E), so this proves the deterministic, product-visible parts: the directory
// renders the app catalog, and a connector that IS connected (seeded via
// apps/api/seed_connector.py) is reflected as "Connected".
//
// The framework connectors' install → actions → policy → execute path is proven
// separately and deterministically in apps/api/tests/test_connector_framework.py
// and tests/test_connector_operations.py.
test("connectors: directory renders catalog and reflects a connected app", async ({ page }) => {
  test.setTimeout(60_000);

  await page.goto("/connectors");

  // The directory loaded and rendered the catalog.
  await expect(page.getByText("Integrations")).toBeVisible();
  const gmailCard = page.getByText("Gmail", { exact: true }).first();
  await expect(gmailCard).toBeVisible();

  // The seeded active connector is reflected as connected.
  await expect(page.getByText("Connected").first()).toBeVisible();
  await expect(page.getByText("e2e@example.com")).toBeVisible();
});
