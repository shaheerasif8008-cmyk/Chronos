import { test, expect } from "@playwright/test";

// Approvals inbox UI — deterministic. A pending approval is seeded for the
// default org (apps/api/seed_approval.py) before the run; this test proves the
// real inbox renders it and the Approve action decides it through the real API.
//
// The broker GATE (gmail.draft → ApprovalRequired) and the decide→resume path
// are proven deterministically in apps/api/tests/test_approval_flow_http.py.
// The live agent-mode trigger is intentionally NOT used here: it depends on the
// model choosing to call the tool, which is non-deterministic (flaky).
//
// "unauthorized cannot approve" is NOT asserted: permission.check only enforces
// with OpenFGA configured, which is off in this environment.
const APPROVAL_ID = process.env.E2E_SEED_APPROVAL_ID;

test.skip(!APPROVAL_ID, "set E2E_SEED_APPROVAL_ID (run apps/api/seed_approval.py first)");

test("approvals: seeded approval shows in inbox and Approve decides it", async ({ page }) => {
  test.setTimeout(60_000);

  await page.goto("/approvals");

  // The seeded pending approval is visible and the batch action is offered.
  const approveBtn = page.getByRole("button", { name: /Approve all/ });
  await expect(approveBtn).toBeVisible();

  await approveBtn.click();

  // The approval is decided (status flips to approved) through the real API.
  await expect
    .poll(async () => {
      return page.evaluate(async (id) => {
        const port = Number(window.location.port || "3001");
        const apiBase = `http://${window.location.hostname}:${8000 + (port - 3000)}`;
        const token = localStorage.getItem("chronos_token") ?? "";
        const res = await fetch(`${apiBase}/approvals/${id}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        return res.ok ? (await res.json()).status : "missing";
      }, APPROVAL_ID);
    }, { timeout: 30_000 })
    .toBe("approved");
});
