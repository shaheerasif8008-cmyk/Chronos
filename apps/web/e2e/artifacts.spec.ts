import { test, expect } from "@playwright/test";

// Artifacts lifecycle through the real UI: create → preview → edit (new version)
// → versions/diff → restore. Seeds via the API, then drives the workspace.
test("artifacts: create, edit into a new version, then restore", async ({ page }) => {
  test.setTimeout(90_000);

  const title = `e2e-artifact-${Date.now()}`;

  await page.goto("/artifacts");

  // Seed a markdown artifact via the API (same auth the app uses).
  const created = await page.evaluate(async (t) => {
    const port = Number(window.location.port || "3001");
    const apiBase = `http://${window.location.hostname}:${8000 + (port - 3000)}`;
    const res = await fetch(`${apiBase}/artifacts`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: "VERSION ONE", kind: "markdown", title: t }),
    });
    return res.ok ? await res.json() : { error: res.status };
  }, title);
  expect(created.error, JSON.stringify(created)).toBeFalsy();

  // Reload so the workspace list includes the new artifact, then open it.
  await page.reload();
  await page.getByRole("button", { name: title }).click();

  // Preview shows the original content.
  await expect(page.getByText("VERSION ONE", { exact: false })).toBeVisible();

  // Edit → replace content → save as a new version.
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  const editor = page.locator("textarea");
  // Guard: the editor must have loaded the current content before we replace it.
  await expect(editor).toHaveValue("VERSION ONE");
  await editor.fill("VERSION TWO");
  await page.getByRole("button", { name: "Save new version" }).click();

  // Save switches back to preview (editor detaches) and shows the new content.
  await expect(editor).toBeHidden();
  await expect(page.getByText("VERSION TWO", { exact: false })).toBeVisible();

  // Versions tab: a second version exists (Restore offered) and a diff is rendered.
  await page.getByRole("button", { name: "Versions", exact: true }).click();
  await expect(page.getByRole("button", { name: "Restore" }).first()).toBeVisible();
  await expect(page.getByText(/Diff v\d+ → v\d+/)).toBeVisible();

  // Restore the first version → content reverts to the original.
  await page.getByRole("button", { name: "Restore" }).first().click();
  await page.getByRole("button", { name: "Preview", exact: true }).click();
  await expect(page.getByText("VERSION ONE", { exact: false })).toBeVisible();
});
