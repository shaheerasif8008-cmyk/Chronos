import { test, expect } from "@playwright/test";

// Deep Research lifecycle: create run → wait for complete → open report → reload proof
test("research: create run, wait for completion, open report, persist after reload", async ({ page }) => {
  // Live run uses a real model + live web fetches; give it room.
  test.setTimeout(300_000);

  await page.goto("/research");

  // Open composer
  await page.getByTestId("research-new-run").click();

  // Fill question
  const question = `e2e-research-${Date.now()}`;
  await page.getByTestId("research-question-input").fill(question);

  // Use the lightest depth (fewest model calls + fetches) to keep the live run snappy.
  await page.locator("select").first().selectOption("quick");

  // Ensure Web scope is checked (default).
  const webCheckbox = page.locator('input[type="checkbox"]').first();
  if (!(await webCheckbox.isChecked())) await webCheckbox.check();

  // Submit
  await page.getByTestId("research-start").click();

  // The run appears in the list and becomes selected — status badge visible
  const statusBadge = page.getByTestId("research-status");
  await expect(statusBadge).toBeVisible({ timeout: 15_000 });

  // Wait for completion (run ends with complete even if search falls back with limitations).
  // Real model + live browser fetches → allow a generous window.
  await expect(statusBadge).toHaveText(/complete/i, { timeout: 240_000 });

  // "Open report" control must be visible
  const openReportBtn = page.getByTestId("research-open-report");
  await expect(openReportBtn).toBeVisible();
  await openReportBtn.click();

  // Report body renders with some text content
  const reportBody = page.getByTestId("research-report-body");
  await expect(reportBody).toBeVisible({ timeout: 15_000 });
  // Expect the report to have some real content
  await expect(reportBody).not.toBeEmpty();

  // Persistence: reload the page, navigate back to the run, report still accessible
  await page.reload();
  await page.waitForLoadState("networkidle");

  // Find the run in the list by question text and click it
  const runItem = page.locator("button").filter({ hasText: question }).first();
  await expect(runItem).toBeVisible({ timeout: 10_000 });
  await runItem.click();

  // Status is still complete
  const statusAfterReload = page.getByTestId("research-status");
  await expect(statusAfterReload).toHaveText(/complete/i, { timeout: 15_000 });

  // Open report button still available
  await expect(page.getByTestId("research-open-report")).toBeVisible();
  await page.getByTestId("research-open-report").click();
  await expect(page.getByTestId("research-report-body")).toBeVisible({ timeout: 15_000 });
});
