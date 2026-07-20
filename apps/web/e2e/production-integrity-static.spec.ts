import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");

function source(relativePath: string): string {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("multipart uploads use cookie authentication without synthetic bearer tokens", () => {
  const chat = source("app/chat/page.tsx");
  const data = source("components/data/DataScreen.tsx");
  const api = source("lib/api.ts");

  expect(chat).not.toContain("function getToken");
  expect(chat).not.toContain("Authorization: `Bearer ${token}`");
  expect(data).not.toContain("Authorization");
  expect(chat).toContain('apiFetch("/attachments"');
  expect(data).toContain('apiFetch("/attachments"');
  expect(api).toContain('credentials: "include"');
  expect(api).toContain('typeof init.body === "string"');
});

test("sign-out clears the server cookie and production login hides dev signup", () => {
  const chat = source("app/chat/page.tsx");
  const login = source("app/login/page.tsx");

  expect(chat).toContain('apiFetch("/auth/logout"');
  expect(login).toContain("{devOtpEnabled ? (");
  expect(login).toContain('provider: "unavailable"');
  expect(login).not.toContain("FALLBACK_DEV_AUTH_CONFIG");
});

test("monitor UI never fabricates observations or change alerts", () => {
  const chat = source("app/chat/page.tsx");

  expect(chat).not.toContain("hash: String(Date.now())");
  expect(chat).not.toContain("`${monitor.name} changed`");
  expect(chat).toContain('apiFetch(`/monitors/${id}/run`');
  expect(chat).toContain("queued for its first real observation");
});

test("onboarding uses a valid least-privilege role and fails visibly", () => {
  const onboarding = source("app/onboarding/page.tsx");

  expect(onboarding).toContain('role: "viewer"');
  expect(onboarding).not.toContain('role: "user"');
  expect(onboarding).toContain('apiFetch("/settings/invitations"');
  expect(onboarding).toContain('setError(e instanceof Error ? e.message : "Invite failed")');
});
