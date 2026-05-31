import { test, expect } from "@playwright/test";

// Model selection is user-controllable and persists across a full reload.
test("model selection persists across reload", async ({ page }) => {
  await page.goto("/chat");

  const model = page.getByLabel("Model", { exact: true });
  await expect(model).toBeVisible();

  // Pick a non-default model.
  await model.selectOption("agent");
  await expect(model).toHaveValue("agent");

  // Survives a full page refresh.
  await page.reload();
  const modelAfter = page.getByLabel("Model", { exact: true });
  await expect(modelAfter).toHaveValue("agent");
});
