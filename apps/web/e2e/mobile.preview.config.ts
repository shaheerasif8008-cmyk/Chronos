import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const WEB_PORT = Number(process.env.MOBILE_E2E_WEB_PORT ?? 3002);
const BASE_URL = `http://localhost:${WEB_PORT}`;

export default defineConfig({
  testDir: ".",
  testMatch: /mobile-responsive\.spec\.ts/,
  outputDir: "../output/playwright/mobile",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "mobile-chromium", use: { ...devices["Pixel 7"] } },
    { name: "mobile-webkit", use: { ...devices["iPhone 15"] } },
  ],
  webServer: {
    command: `NEXT_DIST_DIR=.next-mobile npx next build --webpack && NEXT_DIST_DIR=.next-mobile npx next start -p ${WEB_PORT}`,
    cwd: path.resolve(__dirname, ".."),
    url: BASE_URL,
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
  },
});
