import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");

function source(relativePath: string): string {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("desktop device settings use the authenticated one-time pairing and revocation APIs", () => {
  const chat = source("app/chat/page.tsx");
  const screen = source("components/settings/DesktopDevicesSettings.tsx");
  const api = source("lib/desktop-devices.ts");

  expect(chat).toContain('id: "devices", label: "Desktop devices"');
  expect(chat).toContain("<DesktopDevicesSettings/>");
  expect(chat).toContain('tab === "memory-settings" || tab === "devices"');
  expect(chat).toContain("Desktop-device pairing requires an operator");
  expect(api).toContain('apiFetch("/desktop-devices/pair-codes", { method: "POST" })');
  expect(api).toContain("/desktop-devices/${encodeURIComponent(deviceId)}/grants");
  expect(api).toContain("/desktop-devices/${encodeURIComponent(deviceId)}/revoke");
  expect(api).toContain("/desktop-devices/grants/${encodeURIComponent(grantId)}/revoke");

  expect(screen).toContain("The code expires after 10 minutes and works once");
  expect(screen).toContain("Anyone who gets it before it expires can pair a device to your account");
  expect(screen).toContain('type DevicePresence = "active" | "offline" | "revoked"');
  expect(screen).toContain("ONLINE_WINDOW_MS = 2 * 60 * 1000");
  expect(screen).toContain("Folder paths and security-scoped bookmarks stay on the device");
  expect(screen).toContain("window.confirm(`Revoke ${device.name}?");
  expect(screen).toContain("window.confirm(`Revoke access to ${grant.display_name}?");
  expect(screen).toContain('role="alert"');
  expect(screen).toContain('aria-label="Loading desktop devices"');
});

test("generic task controls keep operator pause separate from approval resume", () => {
  const chat = source("app/chat/page.tsx");
  const controls = source("components/tasks/TaskControls.tsx");
  const api = source("lib/task-controls.ts");

  expect(api).toContain("/tasks/${encodeURIComponent(taskId)}/${action}");
  expect(api).toContain('taskMutation(taskId, "pause", { body: JSON.stringify({ reason }) })');
  expect(api).toContain('taskMutation(taskId, "resume")');
  expect(api).toContain('taskMutation(taskId, "cancel")');

  expect(controls).toContain('const canResume = status === "paused"');
  expect(controls).toContain('const awaitingApproval = status === "awaiting_approval"');
  expect(controls).toContain("a task resume cannot bypass it");
  expect(controls).not.toContain('status === "paused" || status === "awaiting_approval"');
  expect(controls).toContain("window.confirm(\"Cancel this task?");
  expect(controls).toContain("Task controls are unavailable while the runtime reports");

  expect(chat.match(/<TaskControls/g)?.length).toBeGreaterThanOrEqual(2);
  expect(chat).toContain('{ id: "paused", label: "Paused"');
  expect(chat).toContain('{ id: "cancelled", label: "Cancelled"');
  expect(chat).toContain('if (task.status === "paused") return "paused"');
  expect(chat).not.toContain('task.status === "awaiting_approval" || task.status === "paused"');
});
