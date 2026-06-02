import { test, expect } from "@playwright/test";

// Durable trace replay: a task's execution timeline is persisted and re-renders
// identically after a full page refresh (not just from the live SSE stream).
// Also exercises the /tasks/{id}/events endpoint whose SSE sibling crashed
// before the asyncio-import fix.
test("activity: task execution timeline persists across refresh", async ({ page }) => {
  test.setTimeout(180_000);

  const goal = `e2e trace ${Date.now()} — reply with the single word HELLO`;

  async function api<T>(path: string, init?: RequestInit): Promise<T> {
    return page.evaluate(
      async ({ path, init }) => {
        const port = Number(window.location.port || "3001");
        const apiBase = `http://${window.location.hostname}:${8000 + (port - 3000)}`;
        const token = localStorage.getItem("chronos_token") ?? "";
        const res = await fetch(`${apiBase}${path}`, {
          ...init,
          headers: {
            ...(init?.headers ?? {}),
            "Content-Type": "application/json",
            Authorization: `Bearer ${token}`,
          },
        });
        return res.json();
      },
      { path, init },
    );
  }

  await page.goto("/activity");

  // Deterministic task creation (no reliance on the chat composer / tool choice).
  const created = await api<{ task_id: string }>("/tasks/", {
    method: "POST",
    body: JSON.stringify({ goal, mode: "agent" }),
  });
  const taskId = created.task_id;
  expect(taskId).toBeTruthy();

  // Let it finish so the timeline is stable.
  await expect
    .poll(async () => (await api<{ status: string }>(`/tasks/${taskId}`)).status, {
      timeout: 150_000,
      intervals: [2000],
    })
    .toMatch(/complete|failed/);

  const liveCount = (await api<unknown[]>(`/tasks/${taskId}/events`)).length;
  expect(liveCount).toBeGreaterThan(0);

  // Open the task in the Activity screen and confirm its timeline renders.
  await page.reload();
  await page.getByText(goal).click();
  await expect(page.getByText("Execution timeline")).toBeVisible();

  // Durable replay: refresh again, reopen — the persisted timeline still matches.
  await page.reload();
  await page.getByText(goal).click();
  await expect(page.getByText("Execution timeline")).toBeVisible();

  const afterRefreshCount = (await api<unknown[]>(`/tasks/${taskId}/events`)).length;
  expect(afterRefreshCount).toBe(liveCount);
});
