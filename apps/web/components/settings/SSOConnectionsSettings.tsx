"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";

type SSOConnection = {
  id: string;
  issuer: string;
  client_id: string;
  display_name?: string;
  email_domain?: string | null;
  default_role?: string;
  enabled?: boolean;
  has_client_secret?: boolean;
};

const EMPTY = {
  issuer: "",
  client_id: "",
  client_secret: "",
  display_name: "",
  email_domain: "",
  default_role: "user",
  enabled: true,
};

export default function SSOConnectionsSettings() {
  const [connections, setConnections] = useState<SSOConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ ...EMPTY });
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setConnections(await (await apiFetch("/auth/sso/connections")).json());
    } catch {
      setConnections([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  async function create() {
    if (!form.issuer.trim() || !form.client_id.trim()) {
      setToast({ kind: "danger", text: "Issuer and client ID are required." });
      return;
    }
    setBusy(true);
    try {
      await apiFetch("/auth/sso/connections", { method: "POST", body: JSON.stringify(form) });
      setAdding(false);
      setForm({ ...EMPTY });
      await load();
      setToast({ kind: "ok", text: "SSO connection created." });
    } catch (err) {
      setToast({ kind: "danger", text: err instanceof Error ? err.message : "Could not create connection." });
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(conn: SSOConnection) {
    setBusy(true);
    try {
      await apiFetch(`/auth/sso/connections/${conn.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          issuer: conn.issuer,
          client_id: conn.client_id,
          display_name: conn.display_name || "",
          email_domain: conn.email_domain || null,
          default_role: conn.default_role || "user",
          enabled: !conn.enabled,
        }),
      });
      await load();
    } catch (err) {
      setToast({ kind: "danger", text: err instanceof Error ? err.message : "Could not update connection." });
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    if (!confirm("Delete this SSO connection? Users routed through it will no longer be able to sign in.")) return;
    setBusy(true);
    try {
      await apiFetch(`/auth/sso/connections/${id}`, { method: "DELETE" });
      setConnections(prev => prev.filter(c => c.id !== id));
    } catch (err) {
      setToast({ kind: "danger", text: err instanceof Error ? err.message : "Could not delete connection." });
    } finally {
      setBusy(false);
    }
  }

  const inputCls = "surface border border-soft rounded-md px-3 py-1.5 text-[13px] outline-none w-full";

  return (
    <section className="mb-8">
      <div className="flex items-center justify-between mb-1">
        <h2 className="text-[16px] font-semibold">Enterprise SSO (OIDC)</h2>
        <button className="btn btn-secondary btn-sm" onClick={() => setAdding(a => !a)}>{adding ? "Cancel" : "Add connection"}</button>
      </div>
      <p className="text-[13px] mb-3" style={{ color: "var(--text-dim)" }}>
        Route members to your identity provider by email domain. Client secrets are write-only and never returned.
      </p>

      {toast && (
        <div className="mb-3 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: toast.kind === "ok" ? "var(--ok)" : "var(--danger)", color: toast.kind === "ok" ? "var(--ok)" : "var(--danger)" }}>
          {toast.text}
        </div>
      )}

      {adding && (
        <div className="surface border border-soft rounded-xl p-4 mb-3 grid grid-cols-2 gap-3">
          <label className="text-[12px]">Issuer URL<input className={inputCls} value={form.issuer} onChange={e => setForm({ ...form, issuer: e.target.value })} placeholder="https://idp.example.com" /></label>
          <label className="text-[12px]">Display name<input className={inputCls} value={form.display_name} onChange={e => setForm({ ...form, display_name: e.target.value })} placeholder="Okta" /></label>
          <label className="text-[12px]">Client ID<input className={inputCls} value={form.client_id} onChange={e => setForm({ ...form, client_id: e.target.value })} /></label>
          <label className="text-[12px]">Client secret<input className={inputCls} type="password" value={form.client_secret} onChange={e => setForm({ ...form, client_secret: e.target.value })} /></label>
          <label className="text-[12px]">Email domain<input className={inputCls} value={form.email_domain} onChange={e => setForm({ ...form, email_domain: e.target.value })} placeholder="example.com" /></label>
          <label className="text-[12px]">Default role
            <select className={inputCls} value={form.default_role} onChange={e => setForm({ ...form, default_role: e.target.value })}>
              {["user", "operator", "manager", "admin"].map(r => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <div className="col-span-2 flex justify-end">
            <button className="btn btn-accent btn-sm" disabled={busy} onClick={() => void create()}>Create connection</button>
          </div>
        </div>
      )}

      <div className="surface border border-soft rounded-xl overflow-hidden">
        {loading && <div className="px-4 py-4 text-[13px]" style={{ color: "var(--text-dim)" }}>Loading…</div>}
        {!loading && connections.length === 0 && <div className="px-4 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No SSO connections configured.</div>}
        {connections.map(conn => (
          <div key={conn.id} className="flex items-center justify-between gap-3 px-4 py-3 border-b hairline last:border-b-0">
            <div className="min-w-0">
              <div className="text-[14px] font-medium truncate">{conn.display_name || conn.issuer}</div>
              <div className="text-[12px] truncate" style={{ color: "var(--text-dim)" }}>
                {conn.email_domain || "no domain"} · {conn.issuer} · role {conn.default_role || "user"}
              </div>
            </div>
            <div className="flex items-center gap-2 flex-shrink-0">
              <span className="tag" style={{ color: conn.enabled ? "var(--ok)" : "var(--text-dim)" }}>{conn.enabled ? "enabled" : "disabled"}</span>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void toggleEnabled(conn)}>{conn.enabled ? "Disable" : "Enable"}</button>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => void remove(conn.id)}>Delete</button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
