import { test, expect } from "@playwright/test";

// Proves the harness end-to-end: authenticated session (from auth.setup.ts) +
// both servers up + a real authenticated page render (no redirect to /login).
test("authenticated user reaches the chat workspace", async ({ page }) => {
  await page.goto("/chat");

  // Auth guard would bounce an unauthenticated client to /login.
  await expect(page).toHaveURL(/\/chat/);

  // The composer is the stable anchor of the chat workspace.
  await expect(page.getByPlaceholder(/Ask Chronos anything/)).toBeVisible();
});
