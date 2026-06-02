import { test, expect } from "@playwright/test";

// Core assistant loop: type → send → stream a live model reply → persist.
// Uses the real configured model (DeepSeek via OpenRouter), so timeouts are generous.
// We poll the server for the persisted assistant reply rather than racing the UI —
// this both proves persistence and keeps the page open so the SSE stream completes.
test("chat: send a message, stream a reply, and persist it", async ({ page }) => {
  test.setTimeout(150_000);

  const prompt = `e2e ping ${Date.now()} — reply briefly`;

  await page.goto("/chat");
  const composer = page.getByPlaceholder(/Ask Chronos anything/);
  await expect(composer).toBeVisible();

  await composer.fill(prompt);
  await composer.press("Enter");

  // The user's message renders immediately.
  await expect(page.getByText(prompt, { exact: false }).first()).toBeVisible();

  // Poll the API until both the user prompt and a non-empty assistant reply are
  // durably persisted for the conversation that received this prompt.
  const checkPersisted = () =>
    page.evaluate(async (sentPrompt) => {
      const port = Number(window.location.port || "3001");
      const apiBase = `http://${window.location.hostname}:${8000 + (port - 3000)}`;
      const token = localStorage.getItem("chronos_token") ?? "";
      const auth = { Authorization: `Bearer ${token}` };

      const convos = await (await fetch(`${apiBase}/chat/conversations`, { headers: auth })).json();
      if (!Array.isArray(convos)) return false;
      for (const c of convos.slice(0, 5)) {
        const msgs = await (
          await fetch(`${apiBase}/chat/conversations/${c.id}/messages`, { headers: auth })
        ).json();
        const hasUser = msgs.some(
          (m: any) => m.role === "user" && (m.content ?? "").includes(sentPrompt),
        );
        if (!hasUser) continue;
        const hasAssistant = msgs.some(
          (m: any) => m.role === "assistant" && (m.content ?? "").trim().length > 0,
        );
        return hasAssistant;
      }
      return false;
    }, prompt);

  await expect.poll(checkPersisted, { timeout: 120_000, intervals: [1000] }).toBeTruthy();

  // And the assistant reply is visible in the UI.
  await expect(page.locator(".prose-body").last()).not.toBeEmpty();
});
