"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";
import RuntimeHealthPanel from "../../components/settings/RuntimeHealthPanel";
import FileQuarantinePanel from "../../components/settings/FileQuarantinePanel";

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
  const res = await apiFetch(path);
  return res.json() as Promise<T>;
}

function Stat({ label, value, href }: { label: string; value: string | number; href?: string }) {
  const inner = (
    <div className="surface h-full rounded-xl border border-soft p-4">
      <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>{label}</div>
      <div className="mt-1 text-[22px] font-semibold tabular">{value}</div>
    </div>
  );
  return href ? <a href={href} className="block rounded-xl hover:opacity-80">{inner}</a> : inner;
}

function Posture({ label, ok }: { label: string; ok: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4 border-b hairline px-4 py-3 last:border-b-0 sm:items-center">
      <span className="text-[13px]">{label}</span>
      <span className="shrink-0 text-right text-[12px] font-medium" style={{ color: ok ? "var(--ok-text)" : "var(--text-dim)" }}>
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

  if (error) return <AdminPageShell><div className="surface rounded-xl border px-4 py-3 text-[13px]" role="alert" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div></AdminPageShell>;
  if (!data) return <AdminPageShell><div className="surface rounded-xl border border-soft px-4 py-6 text-[13px]" role="status" aria-live="polite" style={{ color: "var(--text-dim)" }}>Loading admin overview…</div></AdminPageShell>;

  return (
    <AdminPageShell>
      <p className="mb-6 text-[13px]" style={{ color: "var(--text-dim)" }}>
        {data.organization.name} · plan {data.organization.plan} · {data.organization.region}
      </p>

      <section aria-label="Organization overview" className="mb-7 grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-3">
        <Stat label="Members" value={data.members.total} />
        <Stat label="Active connectors" value={data.connectors.active} href="/connectors" />
        <Stat label="Pending approvals" value={data.approvals.pending} href="/approvals" />
        <Stat label="Audit events" value={data.audit.total_events} href="/audit" />
        <Stat label="Unread org notifications" value={data.notifications.unread_org_wide} href="/notifications" />
      </section>

      <h2 className="mb-2 text-[13px] font-medium">Members by role</h2>
      <div className="surface border border-soft rounded-xl p-4 mb-6 flex flex-wrap gap-3 text-[13px]">
        {Object.entries(data.members.by_role).map(([role, n]) => (
          <span key={role} className="px-2 py-1 rounded-lg" style={{ background: "var(--surface-2)" }}>{role}: {n}</span>
        ))}
      </div>

      <RuntimeHealthPanel canAdmin />

      <FileQuarantinePanel />

      <h2 className="mb-2 text-[13px] font-medium">Governance posture</h2>
      <div className="surface border border-soft rounded-xl overflow-hidden">
        <Posture label="Relationship access control (OpenFGA)" ok={data.governance.openfga_enabled} />
        <Posture label="Enterprise SSO" ok={data.governance.sso_configured} />
        <Posture label="Email notification delivery" ok={data.governance.email_delivery_configured} />
      </div>
    </AdminPageShell>
  );
}

function AdminPageShell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mobile-safe-bottom h-[100dvh] overflow-y-auto px-4 py-6 sm:px-6 sm:py-8">
      <div className="mx-auto max-w-3xl">
        <header className="mb-5">
          <a href="/chat" className="btn btn-ghost btn-sm -ml-2 mb-3">← Chronos workspace</a>
          <h1 className="h-page">Admin console</h1>
        </header>
        {children}
      </div>
    </main>
  );
}
