import { test, expect } from "@playwright/test";

// Explicit memory lifecycle through the real UI: add (remember) → retrieve
// (appears in list) → edit → delete. Deterministic — no model involved.
test("memory: add, edit, and delete an explicit memory", async ({ page }) => {
  test.setTimeout(60_000);

  const base = `e2e-memory-${Date.now()}`;
  const edited = `${base} EDITED`;

  await page.goto("/memory");

  // Add (remember).
  await page.getByRole("button", { name: /Add a memory/ }).click();
  await page.getByPlaceholder(/Something Chronos should remember/).fill(base);
  await page.getByRole("button", { name: "Save memory" }).click();

  // Retrieve: the new memory shows up in the list as its own card.
  const card = page.locator(".mem-card").filter({ hasText: base });
  await expect(card).toBeVisible();

  // Edit.
  await card.hover();
  await card.getByTitle("Edit").click();
  await card.locator("textarea").fill(edited);
  await card.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(edited)).toBeVisible();

  // Delete → the card is removed.
  await card.hover();
  await card.getByTitle("Delete").click();
  await expect(page.locator(".mem-card").filter({ hasText: base })).toHaveCount(0);
});
