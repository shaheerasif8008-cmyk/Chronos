import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

const root = path.resolve(__dirname, "..");
const source = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("runtime settings distinguish blocking services from optional capabilities", () => {
  const chat = source("app/chat/page.tsx");
  const admin = source("app/admin/page.tsx");
  const panel = source("components/settings/RuntimeHealthPanel.tsx");

  expect(chat).toContain("<RuntimeHealthPanel canAdmin={canAdmin}/>");
  expect(admin).toContain("<RuntimeHealthPanel canAdmin />");
  expect(panel).toContain('apiFetch(`/settings/runtime-health${refresh ? "?refresh=true" : ""}`)');
  expect(panel).toContain("Required services");
  expect(panel).toContain("Optional capabilities");
  expect(panel).toContain("Does not block core workspace use");
  expect(panel).toContain("canAdmin && check.remediation");
  expect(panel).toContain('aria-live="polite"');
});

test("first-run setup gates completion on server-backed readiness", () => {
  const onboarding = source("app/onboarding/page.tsx");
  const chat = source("app/chat/page.tsx");

  expect(onboarding).toContain('apiFetch(`/settings/runtime-health${refresh ? "?refresh=true" : ""}`)');
  expect(onboarding).toContain("!readiness?.can_complete_onboarding");
  expect(onboarding).toContain('apiFetch("/settings/onboarding/complete"');
  expect(onboarding).toContain('role="progressbar"');
  expect(onboarding).toContain('role="alert"');
  expect(onboarding).toContain('apiFetch("/settings/onboarding/guide")');
  expect(onboarding).toContain("Complete your first workflow");
  expect(onboarding).toContain("guide.complete} of {guide.total}");
  expect(chat).toContain('apiFetch("/settings/onboarding/guide")');
  expect(chat).toContain("firstUseGuide.steps.filter");
});
