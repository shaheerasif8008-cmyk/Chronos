import { test as setup, expect } from "@playwright/test";
import fs from "fs";
import path from "path";

const AUTH_FILE = path.join(__dirname, ".auth", "user.json");
const API_PORT = Number(process.env.E2E_API_PORT ?? 8001);
const API_BASE = `http://localhost:${API_PORT}`;
const EMAIL = (process.env.E2E_EMAIL ?? "admin@example.com").toLowerCase();
const LOG_FILE = path.join(__dirname, ".artifacts", "api.log");

/** Scrape the dev OTP the API prints to stdout (teed into api.log by start-api.sh). */
async function readOtp(email: string): Promise<string> {
  const re = new RegExp(`Chronos dev OTP for ${email}: (\\d{6})`, "g");
  for (let attempt = 0; attempt < 60; attempt++) {
    if (fs.existsSync(LOG_FILE)) {
      const txt = fs.readFileSync(LOG_FILE, "utf8");
      let m: RegExpExecArray | null;
      let last: string | null = null;
      while ((m = re.exec(txt)) !== null) last = m[1];
      re.lastIndex = 0;
      if (last) return last;
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`OTP for ${email} not found in ${LOG_FILE}`);
}

setup("authenticate", async ({ page, request }) => {
  const reqRes = await request.post(`${API_BASE}/auth/request-otp`, {
    data: { email: EMAIL },
  });
  expect(reqRes.ok()).toBeTruthy();

  const code = await readOtp(EMAIL);

  const verifyRes = await request.post(`${API_BASE}/auth/verify-otp`, {
    data: { email: EMAIL, code },
  });
  expect(verifyRes.ok()).toBeTruthy();
  const { access_token } = await verifyRes.json();
  expect(access_token, "verify-otp returned a token").toBeTruthy();

  // Seed the token into the web origin's localStorage, then persist storageState.
  await page.goto("/login");
  await page.evaluate((t) => localStorage.setItem("chronos_token", t), access_token);

  fs.mkdirSync(path.dirname(AUTH_FILE), { recursive: true });
  await page.context().storageState({ path: AUTH_FILE });
});
