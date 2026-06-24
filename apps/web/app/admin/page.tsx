"use client";

import { useEffect, useState } from "react";

const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;
function apiBase() {
  if (CONFIGURED_API_BASE) return CONFIGURED_API_BASE;
  if (typeof window !== "undefined") {
    const webPort = Number(window.location.port || "3000");
    if (Number.isFinite(webPort) && webPort >= 3000 && webPort < 3100) {
      return `http://${window.location.hostname}:${8000 + (webPort - 3000)}`;
    }
  }
  return "http://localhost:8000";
}

type Overview = {
  organization: { id: string; name: string; slug: string; plan: string; region: string };
  members: { total: number; by_role: Record<string, number> };
  connectors: { active: number };
  approvals: { pending: number };
  audit: { total_events: number };
  notifications: { unread_org_wide: number };
  governance: { openfga_enabled: boolean; sso_configured: boolean; email_delivery_configured: boolean };
};

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${apiBase()}${path}`, { credentials: "include" });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<T>;
}

function Stat({ label, value, href }: { label: string; value: string | number; href?: string }) {
  const inner = (
    <div className="surface border border-soft rounded-xl p-4">
      <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>{label}</div>
      <div className="text-[22px] font-semibold mt-1">{value}</div>
    </div>
  );
  return href ? <a href={href} className="block hover:opacity-80">{inner}</a> : inner;
}

function Posture({ label, ok }: { label: string; ok: boolean }) {
  return (
    <div className="flex items-center justify-between px-4 py-3 border-b hairline last:border-b-0">
      <span className="text-[13px]">{label}</span>
      <span className="text-[12px] font-medium" style={{ color: ok ? "var(--ok, #16a34a)" : "var(--text-dim)" }}>
        {ok ? "Enabled" : "Not configured"}
      </span>
    </div>
  );
}

export default function AdminConsolePage() {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    getJson<Overview>("/admin/overview")
      .then(setData)
      .catch(e => setError(e instanceof Error ? e.message : "Failed to load admin overview"));
  }, []);

  if (error) return <div className="max-w-3xl mx-auto p-6 text-[13px]" style={{ color: "var(--danger, #dc2626)" }}>{error}</div>;
  if (!data) return <div className="max-w-3xl mx-auto p-6 text-[13px]" style={{ color: "var(--text-dim)" }}>Loading…</div>;

  return (
    <div className="max-w-3xl mx-auto p-6">
      <div className="mb-1 text-[20px] font-semibold">Admin console</div>
      <div className="mb-5 text-[13px]" style={{ color: "var(--text-dim)" }}>
        {data.organization.name} · plan {data.organization.plan} · {data.organization.region}
      </div>

      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 mb-6">
        <Stat label="Members" value={data.members.total} />
        <Stat label="Active connectors" value={data.connectors.active} href="/connectors" />
        <Stat label="Pending approvals" value={data.approvals.pending} href="/approvals" />
        <Stat label="Audit events" value={data.audit.total_events} href="/audit" />
        <Stat label="Unread org notifications" value={data.notifications.unread_org_wide} href="/notifications" />
      </div>

      <div className="mb-2 text-[13px] font-medium">Members by role</div>
      <div className="surface border border-soft rounded-xl p-4 mb-6 flex flex-wrap gap-3 text-[13px]">
        {Object.entries(data.members.by_role).map(([role, n]) => (
          <span key={role} className="px-2 py-1 rounded-lg" style={{ background: "var(--surface-2)" }}>{role}: {n}</span>
        ))}
      </div>

      <div className="mb-2 text-[13px] font-medium">Governance posture</div>
      <div className="surface border border-soft rounded-xl overflow-hidden">
        <Posture label="Relationship access control (OpenFGA)" ok={data.governance.openfga_enabled} />
        <Posture label="Enterprise SSO" ok={data.governance.sso_configured} />
        <Posture label="Email notification delivery" ok={data.governance.email_delivery_configured} />
      </div>
    </div>
  );
}
