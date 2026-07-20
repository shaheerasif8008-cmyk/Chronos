import { expect, test } from "@playwright/test";

const MEMBER = {
  id: "member-mobile-preview",
  email: "mobile@example.com",
  role: "admin",
  organization_id: "org-mobile-preview",
};

test.beforeEach(async ({ page }) => {
  // The production preview keeps its restrictive connect-src policy. Stub API
  // fetches in-page so the browser check remains credential-free without
  // weakening the application CSP just for tests.
  await page.addInitScript(member => {
    const nativeFetch = window.fetch.bind(window);
    const jsonResponse = (body: unknown) => new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });

    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const rawUrl = typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
      const url = new URL(rawUrl, window.location.href);
      const pathname = url.pathname;

      if (pathname === "/auth/me") return jsonResponse(member);
      if (pathname === "/health") return jsonResponse({ status: "ok" });
      if (pathname === "/auth/config") {
        return jsonResponse({ provider: "development", devOtp: false, cognito: { enabled: false } });
      }
      if (pathname === "/settings" || pathname === "/settings/") {
        return jsonResponse({
          member: { ...member, can_admin: true },
          organization: { id: member.organization_id, name: "Mobile Preview", plan: "trial" },
          sections: { general: {} },
          members: [],
          connectors: [],
          memory_stats: { active: 0, deleted: 0 },
          runtime_health: { connectors: {} },
          capabilities: {},
        });
      }
      if (pathname === "/connectors/mcp") return jsonResponse({ servers: [] });
      if (pathname === "/notifications/unread_count") return jsonResponse({ count: 0 });

      if (url.origin !== window.location.origin) return jsonResponse([]);
      return nativeFetch(input, init);
    }) as typeof window.fetch;
  }, MEMBER);

  await page.route("**/*", async route => {
    const request = route.request();
    if (!['fetch', 'xhr'].includes(request.resourceType())) {
      await route.continue();
      return;
    }

    const url = new URL(request.url());
    const pathname = url.pathname;
    const json = (body: unknown) => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

    if (pathname === "/auth/me") return json(MEMBER);
    if (pathname === "/health") return json({ status: "ok" });
    if (pathname === "/auth/config") {
      return json({ provider: "development", devOtp: false, cognito: { enabled: false } });
    }
    if (pathname === "/settings/") {
      return json({
        member: { ...MEMBER, can_admin: true },
        organization: { id: MEMBER.organization_id, name: "Mobile Preview", plan: "trial" },
        sections: { general: {} },
        members: [],
        connectors: [],
        memory_stats: { active: 0, deleted: 0 },
        runtime_health: { connectors: {} },
        capabilities: {},
      });
    }
    if (pathname === "/connectors/mcp") return json({ servers: [] });
    if (pathname === "/notifications/unread_count") return json({ count: 0 });
    return json([]);
  });
});

async function expectNoViewportOverflow(page: import("@playwright/test").Page) {
  const dimensions = await page.evaluate(() => ({
    viewport: window.innerWidth,
    documentWidth: document.documentElement.scrollWidth,
    bodyWidth: document.body.scrollWidth,
  }));
  expect(dimensions.documentWidth).toBeLessThanOrEqual(dimensions.viewport + 1);
  expect(dimensions.bodyWidth).toBeLessThanOrEqual(dimensions.viewport + 1);
}

test("mobile app shell exposes an accessible drawer without horizontal overflow", async ({ page }) => {
  await page.goto("/chat");
  const openNavigation = page.getByRole("button", { name: "Open navigation" });
  await expect(openNavigation).toBeVisible();
  await expectNoViewportOverflow(page);

  await openNavigation.click();
  const navigation = page.getByRole("dialog", { name: "Primary navigation" });
  await expect(navigation).toBeVisible();
  const closeNavigation = page.getByRole("button", { name: "Close navigation" }).last();
  await expect(closeNavigation).toBeVisible();
  await expect(closeNavigation).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(navigation).toBeHidden();
  await expect(openNavigation).toBeFocused();

  const attach = page.getByRole("button", { name: "Attach" });
  await expect(attach).toBeVisible();
  await attach.click();
  await expect(page.getByText("Upload files", { exact: true })).toBeVisible();
  await expect(page.getByText("Upload photos", { exact: true })).toBeVisible();
  await expectNoViewportOverflow(page);
});

test("mobile core work surfaces remain reachable and contained", async ({ page }) => {
  await page.goto("/chat");
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page.getByRole("button", { name: "Activity" }).click();
  await expect(page).toHaveURL(/\/activity/);
  await expect(page.getByRole("tab", { name: "Tasks" })).toBeVisible();
  await expectNoViewportOverflow(page);

  for (const tab of ["Research", "Browser", "Computer"] as const) {
    await page.getByRole("tab", { name: tab }).click();
    await expectNoViewportOverflow(page);
  }

  for (const route of ["/approvals", "/connectors", "/artifacts", "/settings"] as const) {
    await page.goto(route);
    await expect(page.getByRole("button", { name: "Open navigation" })).toBeVisible();
    await expectNoViewportOverflow(page);

    if (route === "/connectors") {
      const addConnector = page.getByRole("button", { name: "Add custom connector" });
      await addConnector.click();
      const dialog = page.getByRole("dialog", { name: "Add custom connector" });
      await expect(dialog).toBeVisible();
      await expect(page.getByLabel("Name")).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await expect(addConnector).toBeFocused();
    }
  }
});

test("mobile authentication and onboarding forms fit the viewport", async ({ page }) => {
  for (const route of ["/login", "/signup", "/onboarding"] as const) {
    await page.goto(route);
    await expectNoViewportOverflow(page);
  }

  await page.goto("/onboarding");
  await page.getByRole("button", { name: "Invite teammates" }).click();
  await expect(page.getByLabel("Teammate email")).toBeVisible();
  await expectNoViewportOverflow(page);
});
