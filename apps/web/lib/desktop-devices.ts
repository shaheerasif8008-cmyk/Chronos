import { apiFetch } from "./api";

export type DesktopDeviceStatus = "active" | "revoked";
export type DesktopGrantStatus = "active" | "revoked";

export type DesktopPairCode = {
  pair_code: string;
  expires_at: string;
};

export type DesktopDevice = {
  id: string;
  organization_id: string;
  member_id: string;
  name: string;
  platform: "macos" | "windows" | "linux" | string;
  client_version: string | null;
  capabilities: Record<string, unknown>;
  status: DesktopDeviceStatus;
  last_seen_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  revoked_at: string | null;
};

export type DesktopFolderGrant = {
  id: string;
  organization_id: string;
  member_id: string;
  device_id: string;
  client_grant_id: string;
  display_name: string;
  purpose: string | null;
  task_id: string | null;
  status: DesktopGrantStatus;
  created_at: string | null;
  updated_at: string | null;
  revoked_at: string | null;
  revocation_command_id?: string;
};

export async function createDesktopPairCode(): Promise<DesktopPairCode> {
  const response = await apiFetch("/desktop-devices/pair-codes", { method: "POST" });
  return response.json() as Promise<DesktopPairCode>;
}

export async function listDesktopDevices(): Promise<DesktopDevice[]> {
  const response = await apiFetch("/desktop-devices/");
  return response.json() as Promise<DesktopDevice[]>;
}

export async function listDesktopDeviceGrants(deviceId: string): Promise<DesktopFolderGrant[]> {
  const response = await apiFetch(`/desktop-devices/${encodeURIComponent(deviceId)}/grants`);
  return response.json() as Promise<DesktopFolderGrant[]>;
}

export async function revokeDesktopDevice(deviceId: string): Promise<DesktopDevice> {
  const response = await apiFetch(`/desktop-devices/${encodeURIComponent(deviceId)}/revoke`, {
    method: "POST",
  });
  return response.json() as Promise<DesktopDevice>;
}

export async function revokeDesktopGrant(grantId: string): Promise<DesktopFolderGrant> {
  const response = await apiFetch(`/desktop-devices/grants/${encodeURIComponent(grantId)}/revoke`, {
    method: "POST",
  });
  return response.json() as Promise<DesktopFolderGrant>;
}
