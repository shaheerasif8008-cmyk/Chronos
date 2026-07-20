import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");
const source = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("private product routes opt out of indexing and expose install metadata", () => {
  const layout = source("app/layout.tsx");
  const robots = source("app/robots.ts");
  const manifest = source("app/manifest.ts");
  expect(layout).toContain('robots: { index: false, follow: false, nocache: true }');
  expect(layout).toContain('manifest: "/manifest.webmanifest"');
  expect(robots).toContain('disallow: "/"');
  expect(manifest).toContain('start_url: "/chat"');
  expect(manifest).toContain('display: "standalone"');
});

test("login and account surfaces expose approved legal support and status links", () => {
  const links = source("components/system/PublicProductLinks.tsx");
  const login = source("app/login/page.tsx");
  const shell = source("app/chat/page.tsx");
  expect(links).toContain('Chronos uses an essential, secure session cookie');
  for (const label of ["Terms", "Privacy", "Support", "Service status"]) {
    expect(links).toContain(label);
  }
  expect(login).toContain("<PublicProductLinks discloseSessionCookie");
  expect(shell).toContain("publicProductLinks.support");
  expect(shell).toContain("publicProductLinks.status");
});

test("offline and public-share states explain impact and credential lifetime", () => {
  const network = source("components/system/NetworkStatus.tsx");
  const shared = source("app/shared/[token]/page.tsx");
  const artifacts = source("components/artifacts/ArtifactsScreen.tsx");
  expect(network).toContain("You’re offline. Your open work stays visible");
  expect(network).toContain("Connection restored");
  expect(shared).toContain("Do not forward the link");
  expect(artifacts).toContain('aria-label="Public link duration"');
  expect(artifacts).toContain("Create a public link that anyone can open");
});
