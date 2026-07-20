import { defineConfig, devices } from "@playwright/test";

// Harness runs fully isolated from any other Chronos instance on this machine:
//   web on :3001  ->  API on :8001 (the web auto-derives API port from its own).
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3001);
const API_PORT = Number(process.env.E2E_API_PORT ?? 8001);
const BASE_URL = `http://localhost:${WEB_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  outputDir: "output/playwright",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], storageState: "e2e/.auth/user.json" },
      dependencies: ["setup"],
      testIgnore: /mobile-responsive\.spec\.ts/,
    },
    {
      name: "mobile-chromium",
      testMatch: /mobile-responsive\.spec\.ts/,
      use: { ...devices["Pixel 7"], storageState: "e2e/.auth/user.json" },
      dependencies: ["setup"],
    },
    {
      name: "mobile-webkit",
      testMatch: /mobile-responsive\.spec\.ts/,
      use: { ...devices["iPhone 15"], storageState: "e2e/.auth/user.json" },
      dependencies: ["setup"],
    },
  ],
  webServer: [
    {
      command: "bash e2e/start-api.sh",
      url: `http://localhost:${API_PORT}/health`,
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      env: { E2E_API_PORT: String(API_PORT) },
    },
    {
      command: `npx next start -p ${WEB_PORT}`,
      url: BASE_URL,
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
    },
  ],
});
