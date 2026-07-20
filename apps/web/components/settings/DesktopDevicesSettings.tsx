"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  createDesktopPairCode,
  listDesktopDeviceGrants,
  listDesktopDevices,
  revokeDesktopDevice,
  revokeDesktopGrant,
  type DesktopDevice,
  type DesktopFolderGrant,
  type DesktopPairCode,
} from "../../lib/desktop-devices";

type DevicePresence = "active" | "offline" | "revoked";

const ONLINE_WINDOW_MS = 2 * 60 * 1000;

function formatDate(value: string | null): string {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Not available";
  return date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function devicePresence(device: DesktopDevice, now: number): DevicePresence {
  if (device.status === "revoked") return "revoked";
  const lastSeen = device.last_seen_at ? new Date(device.last_seen_at).getTime() : Number.NaN;
  return Number.isFinite(lastSeen) && now - lastSeen <= ONLINE_WINDOW_MS ? "active" : "offline";
}

function statusStyle(status: DevicePresence | DesktopFolderGrant["status"]): { color: string; background: string } {
  if (status === "active") return { color: "var(--ok)", background: "var(--ok-soft)" };
  if (status === "offline") return { color: "var(--warn)", background: "var(--warn-soft)" };
  return { color: "var(--text-dim)", background: "var(--surface-2)" };
}

function StatusBadge({ status }: { status: DevicePresence | DesktopFolderGrant["status"] }) {
  const style = statusStyle(status);
  return (
    <span className="inline-flex rounded-full px-2 py-0.5 text-[11px] font-medium capitalize" style={style}>
      {status}
    </span>
  );
}

function PairingCodePanel({
  pairCode,
  now,
  copied,
  busy,
  error,
  onCreate,
  onCopy,
}: {
  pairCode: DesktopPairCode | null;
  now: number;
  copied: boolean;
  busy: boolean;
  error: string;
  onCreate: () => void;
  onCopy: () => void;
}) {
  const expiresAt = pairCode ? new Date(pairCode.expires_at).getTime() : 0;
  const secondsLeft = Number.isFinite(expiresAt) ? Math.max(0, Math.ceil((expiresAt - now) / 1000)) : 0;
  const active = Boolean(pairCode && secondsLeft > 0);
  const minutes = Math.floor(secondsLeft / 60);
  const seconds = String(secondsLeft % 60).padStart(2, "0");

  return (
    <section className="mb-8" aria-labelledby="desktop-pairing-title">
      <h2 id="desktop-pairing-title" className="mb-1 text-[16px] font-semibold">Pair a desktop app</h2>
      <p className="mb-3 text-[13px]" style={{ color: "var(--text-dim)" }}>
        Open Chronos Desktop, enter your API URL and device name, then use this one-time code to pair securely.
      </p>
      <div className="surface overflow-hidden rounded-xl border border-soft">
        <div className="flex flex-col items-stretch justify-between gap-4 px-4 py-4 sm:flex-row sm:items-start sm:px-5">
          <div className="min-w-0">
            <div className="text-[14px] font-medium">One-time pairing code</div>
            <p className="mt-0.5 max-w-2xl text-[13px] leading-relaxed" style={{ color: "var(--text-dim)" }}>
              The code expires after 10 minutes and works once. Anyone who gets it before it expires can pair a device to your account, so enter it only in the signed Chronos Desktop app and never send it in chat or email.
            </p>
          </div>
          <div className="w-full min-w-0 sm:w-auto sm:min-w-[300px]">
            {pairCode ? (
              <div className="rounded-lg border border-soft px-3 py-3" style={{ background: "var(--bg)" }}>
                <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                  <code className="select-all break-all font-mono text-[20px] font-semibold tracking-[0.12em]" aria-label="One-time pairing code">
                    {pairCode.pair_code}
                  </code>
                  <button type="button" className="btn btn-secondary btn-sm self-start" disabled={!active} onClick={onCopy}>
                    {copied ? "Copied" : active ? "Copy code" : "Expired"}
                  </button>
                </div>
                <p className="mt-2 text-[12px]" style={{ color: active ? "var(--warn)" : "var(--danger)" }} role="status" aria-live="polite">
                  {active ? `Expires in ${minutes}:${seconds}` : "This code has expired. Create a new code to pair."}
                  {active && <span> · <time dateTime={pairCode.expires_at}>{formatDate(pairCode.expires_at)}</time></span>}
                </p>
              </div>
            ) : (
              <button type="button" className="btn btn-accent btn-sm w-full justify-center sm:w-auto" disabled={busy} onClick={onCreate}>
                {busy ? "Creating code…" : "Create one-time code"}
              </button>
            )}
            {pairCode && !active && (
              <button type="button" className="btn btn-accent btn-sm mt-2 w-full justify-center sm:w-auto" disabled={busy} onClick={onCreate}>
                {busy ? "Creating code…" : "Create new code"}
              </button>
            )}
            {error && <p className="mt-2 text-[12px]" style={{ color: "var(--danger)" }} role="alert">{error}</p>}
          </div>
        </div>
      </div>
    </section>
  );
}

interface DeviceCardProps {
  device: DesktopDevice;
  grants: DesktopFolderGrant[];
  now: number;
  busy: string;
  onRevokeDevice: (device: DesktopDevice) => void;
  onRevokeGrant: (grant: DesktopFolderGrant) => void;
}

function DeviceCard({ device, grants, now, busy, onRevokeDevice, onRevokeGrant }: DeviceCardProps) {
  const presence = devicePresence(device, now);
  const activeGrants = grants.filter(grant => grant.status === "active");

  return (
    <article className="surface overflow-hidden rounded-xl border border-soft" aria-labelledby={`device-${device.id}`}>
      <div className="flex flex-col gap-4 px-4 py-4 sm:flex-row sm:items-start sm:justify-between sm:px-5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 id={`device-${device.id}`} className="truncate text-[14px] font-semibold">{device.name}</h3>
            <StatusBadge status={presence}/>
          </div>
          <p className="mt-1 text-[12px] capitalize" style={{ color: "var(--text-dim)" }}>
            {device.platform}{device.client_version ? ` · Chronos Desktop ${device.client_version}` : ""}
          </p>
          <p className="mt-1 text-[12px]" style={{ color: "var(--text-dim)" }}>
            {presence === "revoked" ? `Revoked ${formatDate(device.revoked_at)}` : `Last check-in ${formatDate(device.last_seen_at)}`} · Paired {formatDate(device.created_at)}
          </p>
        </div>
        {device.status === "active" && (
          <button
            type="button"
            className="btn btn-danger-soft btn-sm self-start disabled:opacity-50"
            disabled={busy !== ""}
            onClick={() => onRevokeDevice(device)}
          >
            {busy === `device:${device.id}` ? "Revoking…" : "Revoke device"}
          </button>
        )}
      </div>

      <div className="border-t hairline px-4 py-4 sm:px-5">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h4 className="text-[13px] font-medium">Authorized folders</h4>
            <p className="mt-0.5 text-[12px]" style={{ color: "var(--text-dim)" }}>
              Folder paths and security-scoped bookmarks stay on the device. Chronos stores only the display name and opaque authorization ID.
            </p>
          </div>
          <span className="text-[12px]" style={{ color: "var(--text-dim)" }}>{activeGrants.length} active</span>
        </div>
        {grants.length === 0 ? (
          <p className="mt-3 rounded-lg border border-soft px-3 py-3 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
            No folders have been authorized on this device.
          </p>
        ) : (
          <ul className="mt-3 space-y-2">
            {grants.map(grant => (
              <li key={grant.id} className="flex flex-col gap-3 rounded-lg border border-soft px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-[13px] font-medium">{grant.display_name}</span>
                    <StatusBadge status={grant.status}/>
                  </div>
                  <p className="mt-1 truncate font-mono text-[11px]" style={{ color: "var(--text-dim)" }}>
                    Authorization {grant.client_grant_id}
                  </p>
                </div>
                {grant.status === "active" && (
                  <button
                    type="button"
                    className="btn btn-danger-soft btn-sm self-start disabled:opacity-50 sm:self-auto"
                    disabled={busy !== "" || device.status !== "active"}
                    onClick={() => onRevokeGrant(grant)}
                  >
                    {busy === `grant:${grant.id}` ? "Revoking…" : "Revoke folder"}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </article>
  );
}

export default function DesktopDevicesSettings() {
  const [devices, setDevices] = useState<DesktopDevice[]>([]);
  const [grants, setGrants] = useState<Record<string, DesktopFolderGrant[]>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [pairError, setPairError] = useState("");
  const [pairCode, setPairCode] = useState<DesktopPairCode | null>(null);
  const [creatingCode, setCreatingCode] = useState(false);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState("");
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setError("");
    try {
      const nextDevices = await listDesktopDevices();
      const grantEntries = await Promise.all(
        nextDevices.map(async device => [device.id, await listDesktopDeviceGrants(device.id)] as const),
      );
      setDevices(nextDevices);
      setGrants(Object.fromEntries(grantEntries));
    } catch (requestError) {
      setDevices([]);
      setGrants({});
      setError(requestError instanceof Error ? requestError.message : "Desktop devices could not be loaded.");
    } finally {
      if (showLoading) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!pairCode) return;
    const expiresAt = new Date(pairCode.expires_at).getTime();
    if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) return;
    const refreshTimer = window.setInterval(() => {
      if (Date.now() >= expiresAt) {
        window.clearInterval(refreshTimer);
        return;
      }
      void load(false);
    }, 15_000);
    return () => window.clearInterval(refreshTimer);
  }, [load, pairCode]);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const sortedDevices = useMemo(() => {
    const rank: Record<DevicePresence, number> = { active: 0, offline: 1, revoked: 2 };
    return [...devices].sort((left, right) => {
      const presenceDifference = rank[devicePresence(left, now)] - rank[devicePresence(right, now)];
      if (presenceDifference) return presenceDifference;
      return (right.created_at || "").localeCompare(left.created_at || "");
    });
  }, [devices, now]);

  async function createCode() {
    if (creatingCode) return;
    setCreatingCode(true);
    setPairError("");
    setCopied(false);
    try {
      setPairCode(await createDesktopPairCode());
      setNow(Date.now());
    } catch (requestError) {
      setPairError(requestError instanceof Error ? requestError.message : "A pairing code could not be created.");
    } finally {
      setCreatingCode(false);
    }
  }

  async function copyCode() {
    if (!pairCode) return;
    try {
      await navigator.clipboard.writeText(pairCode.pair_code);
      setCopied(true);
      setPairError("");
    } catch {
      setPairError("Copy was blocked by the browser. Select the code and copy it manually.");
    }
  }

  async function revokeDevice(device: DesktopDevice) {
    if (!window.confirm(`Revoke ${device.name}? The device token, queued commands, and its folder authorizations will stop working immediately.`)) return;
    setBusy(`device:${device.id}`);
    setError("");
    try {
      await revokeDesktopDevice(device.id);
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The device could not be revoked.");
    } finally {
      setBusy("");
    }
  }

  async function revokeGrant(grant: DesktopFolderGrant) {
    if (!window.confirm(`Revoke access to ${grant.display_name}? New commands will no longer be able to use this folder.`)) return;
    setBusy(`grant:${grant.id}`);
    setError("");
    try {
      await revokeDesktopGrant(grant.id);
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The folder authorization could not be revoked.");
    } finally {
      setBusy("");
    }
  }

  return (
    <div>
      <PairingCodePanel
        pairCode={pairCode}
        now={now}
        copied={copied}
        busy={creatingCode}
        error={pairError}
        onCreate={() => void createCode()}
        onCopy={() => void copyCode()}
      />

      <section className="mb-8" aria-labelledby="paired-devices-title">
        <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h2 id="paired-devices-title" className="text-[16px] font-semibold">Paired devices</h2>
            <p className="mt-1 text-[13px]" style={{ color: "var(--text-dim)" }}>
              Active means the signed app checked in within two minutes. Offline devices keep no cloud-side filesystem access; commands wait only within their short server expiry.
            </p>
          </div>
          <button type="button" className="btn btn-ghost btn-sm self-start sm:self-auto" disabled={loading || busy !== ""} onClick={() => void load()}>
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>

        {error && (
          <div className="mb-3 rounded-xl border px-4 py-3 text-[13px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }} role="alert">
            <p>{error}</p>
            <button type="button" className="btn btn-secondary btn-sm mt-3" disabled={loading} onClick={() => void load()}>Try again</button>
          </div>
        )}

        {loading ? (
          <div className="space-y-3" aria-label="Loading desktop devices" role="status">
            {[0, 1].map(item => <div key={item} className="h-36 rounded-xl" style={{ background: "var(--surface-2)" }}/>) }
          </div>
        ) : !error && sortedDevices.length === 0 ? (
          <div className="surface rounded-xl border border-soft px-5 py-8 text-center">
            <h3 className="text-[14px] font-semibold">No desktop devices paired</h3>
            <p className="mx-auto mt-1 max-w-xl text-[13px] leading-relaxed" style={{ color: "var(--text-dim)" }}>
              Install the signed Chronos Desktop app from your organization’s approved release channel, create a one-time code above, and pair from the app. No local files are available until you authorize a folder on that device.
            </p>
          </div>
        ) : !error ? (
          <div className="space-y-3">
            {sortedDevices.map(device => (
              <DeviceCard
                key={device.id}
                device={device}
                grants={grants[device.id] || []}
                now={now}
                busy={busy}
                onRevokeDevice={value => void revokeDevice(value)}
                onRevokeGrant={value => void revokeGrant(value)}
              />
            ))}
          </div>
        ) : null}
      </section>
    </div>
  );
}
