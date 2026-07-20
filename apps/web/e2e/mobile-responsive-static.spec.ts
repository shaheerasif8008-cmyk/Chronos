import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");

function source(relativePath: string): string {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("Playwright defines isolated mobile Chromium and WebKit projects", () => {
  const config = source("playwright.config.ts");
  expect(config).toContain('name: "mobile-chromium"');
  expect(config).toContain('devices["Pixel 7"]');
  expect(config).toContain('name: "mobile-webkit"');
  expect(config).toContain('devices["iPhone 15"]');
  expect(config).toContain("mobile-responsive\\.spec\\.ts");
});

test("the authenticated shell has a mobile app bar, modal drawer, and safe composer", () => {
  const chat = source("app/chat/page.tsx");
  const css = source("app/globals.css");
  expect(chat).toContain('className="mobile-app-bar');
  expect(chat).toContain('id="chronos-primary-navigation"');
  expect(chat).toContain('aria-label="Open navigation"');
  expect(chat).toContain('aria-label="Primary navigation"');
  expect(chat).toContain("mobile-safe-bottom");
  expect(chat).toContain("composer-menu-popover");
  expect(chat).toContain("flex-1 flex-wrap items-center gap-1 overflow-visible sm:flex-nowrap sm:overflow-x-auto");
  expect(chat).toContain("flex-shrink-0 px-3 py-2.5 text-[13.5px] font-medium transition-colors sm:px-4");
  expect(chat).toContain("flex flex-wrap items-center gap-1 px-4 pb-4 md:px-10");
  expect(css).toContain("env(safe-area-inset-bottom)");
  expect(css).toContain("min-height: 44px");
});

test("core split panes collapse into contained mobile layouts", () => {
  const research = source("components/research/ResearchScreen.tsx");
  const browser = source("components/browser/BrowserOperatorScreen.tsx");
  const computer = source("components/computer/ComputerScreen.tsx");
  const data = source("components/data/DataScreen.tsx");
  const artifacts = source("components/artifacts/ArtifactsScreen.tsx");

  expect(research).toContain("flex-col md:flex-row");
  expect(browser).toContain("md:grid-cols-[320px_minmax(0,1fr)]");
  expect(computer).toContain("md:grid-cols-[320px_minmax(0,1fr)]");
  expect(data).toContain("data-workspace-body");
  expect(artifacts).toContain("flex-col md:flex-row");
});

test("signup and onboarding use responsive accessible application surfaces", () => {
  const signup = source("app/signup/page.tsx");
  const onboarding = source("app/onboarding/page.tsx");
  expect(signup).toContain('className="h-[100dvh]');
  expect(signup).toContain('autoComplete="one-time-code"');
  expect(onboarding).toContain('htmlFor="invite-email"');
  expect(onboarding).toContain("flex-col gap-2 sm:flex-row");
});
