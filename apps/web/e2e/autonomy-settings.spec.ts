import { test, expect } from "@playwright/test";

test("settings autonomy: renders trust governance and exercises live autonomy endpoints", async ({ page }) => {
  test.setTimeout(60_000);

  await page.goto("/settings?tab=approval-settings");

  await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Trust levels" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Graduation proposals" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Learned policies" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Risk overrides" })).toBeVisible();

  const tool = `e2e.risk.${Date.now()}`;
  await page.getByLabel("Risk override tool").fill(tool);
  await page.getByLabel("Blast radius").fill("0.3");
  await page.getByLabel("Irreversibility").fill("0.7");
  await page.getByRole("button", { name: "Save override" }).click();

  await expect(page.getByText("Risk override saved")).toBeVisible();
  await expect(page.getByText(tool)).toBeVisible();

  const apiProof = await page.evaluate(async ({ toolName }) => {
    const port = Number(window.location.port || "3001");
    const apiBase = `http://${window.location.hostname}:${8000 + (port - 3000)}`;
    const [trust, proposals, policies, overrides, evidence] = await Promise.all([
      fetch(`${apiBase}/autonomy/trust?workspace_id=default`, { credentials: "include" }),
      fetch(`${apiBase}/autonomy/proposals`, { credentials: "include" }),
      fetch(`${apiBase}/autonomy/learned-policies`, { credentials: "include" }),
      fetch(`${apiBase}/autonomy/risk-overrides`, { credentials: "include" }),
      fetch(`${apiBase}/autonomy/evidence?scope=workspace%3Adefault&action_class=${encodeURIComponent(toolName)}`, { credentials: "include" }),
    ]);
    const overrideRows = overrides.ok ? await overrides.json() : [];
    const evidenceBody = evidence.ok ? await evidence.json() : null;
    return {
      statuses: [trust.status, proposals.status, policies.status, overrides.status, evidence.status],
      savedOverride: Array.isArray(overrideRows) && overrideRows.some((row) => row.tool === toolName),
      evidenceShape: Boolean(evidenceBody?.chain_head && evidenceBody?.signature && evidenceBody?.algorithm),
    };
  }, { toolName: tool });

  expect(apiProof.statuses).toEqual([200, 200, 200, 200, 200]);
  expect(apiProof.savedOverride).toBe(true);
  expect(apiProof.evidenceShape).toBe(true);
});
