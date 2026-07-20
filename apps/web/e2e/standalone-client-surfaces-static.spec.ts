import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const source = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("standalone client surfaces share the production shell and mobile-safe spacing", () => {
  const admin = source("app/admin/page.tsx");
  const notifications = source("app/notifications/page.tsx");
  const billing = source("app/settings/billing/page.tsx");

  for (const page of [admin, notifications, billing]) {
    expect(page).toContain("mobile-safe-bottom h-[100dvh] overflow-y-auto");
    expect(page).toContain('className="h-page"');
    expect(page).toContain("btn btn-ghost btn-sm");
  }

  expect(admin).toContain("<h1");
  expect(admin).toContain('aria-label="Organization overview"');
  expect(admin).toContain("grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3");
  expect(admin).toContain('role="status" aria-live="polite"');
});

test("notifications expose non-color state, list semantics, and guarded mobile actions", () => {
  const notifications = source("app/notifications/page.tsx");

  expect(notifications).toContain('aria-busy={loading || Boolean(busy)}');
  expect(notifications).toContain('aria-pressed={showDismissed}');
  expect(notifications).toContain('aria-label="Notifications"');
  expect(notifications).toContain("severityTagClass(n.severity)");
  expect(notifications).toContain('className="sr-only"');
  expect(notifications).toContain('<time dateTime={n.created_at}>');
  expect(notifications).toContain('disabled={Boolean(busy)}');
  expect(notifications).toContain('role="alert"');
});

test("billing preserves context on action errors and exposes redirect progress", () => {
  const billing = source("app/settings/billing/page.tsx");

  expect(billing).toContain('role="alert"');
  expect(billing).toContain("Billing provider did not return a secure redirect.");
  expect(billing).toContain("Billing changes are not configured for this deployment.");
  expect(billing).toContain('aria-busy={busy === "checkout"}');
  expect(billing).toContain('aria-busy={busy === "portal"}');
  expect(billing).toContain("Opening checkout…");
  expect(billing).toContain("Opening billing…");
  expect(billing).toContain("formatUsd(usage.cost_usd)");
  expect(billing.indexOf("{error &&") > billing.indexOf("const ent = plan.entitlements"));
});

test("shared semantic status tokens work in light and dark themes", () => {
  const css = source("app/globals.css");

  expect(css.match(/--ok-text:/g)).toHaveLength(2);
  expect(css.match(/--warn-text:/g)).toHaveLength(2);
  expect(css).toContain(".tag-ok     { background: var(--ok-soft);     color: var(--ok-text); }");
  expect(css).toContain(".tag-warn   { background: var(--warn-soft);   color: var(--warn-text); }");
  expect(css).toContain(".btn:disabled { cursor: not-allowed; opacity: 0.55; transform: none; }");
});
