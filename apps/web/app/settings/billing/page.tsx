"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../../lib/api";

type Entitlements = {
  max_seats: number;
  daily_cost_limit_usd: number;
  daily_token_limit: number;
  features: string[];
};
type PlanResp = {
  plan: string;
  entitlements: Entitlements;
  usage: { seats_used: number; tokens_today: number; cost_today_usd: number };
};
type UsageResp = { period: string; tokens: number; cost_usd: number; over_budget: boolean };

async function getJson<T>(path: string): Promise<T> {
  const res = await apiFetch(path);
  return res.json() as Promise<T>;
}

export default function BillingPage() {
  const [plan, setPlan] = useState<PlanResp | null>(null);
  const [usage, setUsage] = useState<UsageResp | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"checkout" | "portal" | "">("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextPlan, nextUsage] = await Promise.all([getJson<PlanResp>("/settings/plan"), getJson<UsageResp>("/settings/billing/usage")]);
      setPlan(nextPlan);
      setUsage(nextUsage);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load billing");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function act(action: "checkout" | "portal", path: string, body?: object) {
    setError("");
    setNotice("");
    setBusy(action);
    try {
      const res = await apiFetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json();
      const url = data.checkout_url || data.portal_url;
      if (!url) throw new Error("Billing provider did not return a secure redirect.");
      window.location.href = url;
    } catch (e) {
      const message = e instanceof Error ? e.message : "Action failed";
      if (/not (configured|available)|price IDs must be distinct/i.test(message)) {
        setNotice("Billing changes are not configured for this deployment. Contact your workspace administrator.");
      } else {
        setError(message);
      }
    } finally {
      setBusy("");
    }
  }

  if (loading) return <BillingPageShell><p className="surface rounded-xl border border-soft px-4 py-6 text-[13px]" role="status" aria-live="polite" style={{ color: "var(--text-dim)" }}>Loading billing…</p></BillingPageShell>;
  if (!plan || !usage) return <BillingPageShell><div className="surface rounded-xl border px-4 py-3 text-[13px]" role="alert" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}><p>{error || "Billing data is unavailable."}</p><button type="button" className="btn btn-secondary btn-sm mt-3" onClick={() => void load()}>Try again</button></div></BillingPageShell>;

  const ent = plan.entitlements;
  return (
    <BillingPageShell>
      <div className="flex flex-col gap-5" aria-busy={Boolean(busy)}>
        {notice && <p className="rounded-lg border border-soft px-3 py-2 text-[13px]" role="status" aria-live="polite" style={{ color: "var(--text-muted)" }}>{notice}</p>}
        {error && <p className="rounded-lg border px-3 py-2 text-[13px]" role="alert" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</p>}

        <section className="surface rounded-xl border border-soft p-4 sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-[16px] font-semibold">Current plan</h2>
            <span className="tag tag-accent capitalize">{plan.plan}</span>
          </div>
          <dl className="mt-4 grid gap-3 text-[13px] sm:grid-cols-2">
            <div><dt style={{ color: "var(--text-dim)" }}>Seats</dt><dd className="font-medium">{plan.usage.seats_used} / {ent.max_seats}</dd></div>
            <div><dt style={{ color: "var(--text-dim)" }}>Daily cost budget</dt><dd className="font-medium tabular">{ent.daily_cost_limit_usd ? formatUsd(ent.daily_cost_limit_usd) : "Unlimited"}</dd></div>
            <div><dt style={{ color: "var(--text-dim)" }}>Daily token budget</dt><dd className="font-medium">{ent.daily_token_limit ? ent.daily_token_limit.toLocaleString() : "Unlimited"}</dd></div>
            <div><dt style={{ color: "var(--text-dim)" }}>Features</dt><dd className="font-medium break-words">{ent.features.join(", ") || "Standard"}</dd></div>
          </dl>
        </section>

        <section className="surface rounded-xl border border-soft p-4 sm:p-5">
          <h2 className="text-[16px] font-semibold">Usage this period</h2>
          <p className="mt-1 text-[12px]" style={{ color: "var(--text-dim)" }}>{usage.period}</p>
          <div className="mt-4 grid grid-cols-2 gap-3">
            <div><div className="text-[12px]" style={{ color: "var(--text-dim)" }}>Tokens</div><div className="text-[22px] font-semibold tabular">{usage.tokens.toLocaleString()}</div></div>
            <div><div className="text-[12px]" style={{ color: "var(--text-dim)" }}>Cost</div><div className="text-[22px] font-semibold tabular">{formatUsd(usage.cost_usd)}</div></div>
          </div>
          {usage.over_budget && <p className="mt-4 rounded-lg px-3 py-2 text-[13px]" role="alert" style={{ background: "var(--danger-soft)", color: "var(--danger)" }}>This workspace is over budget for the current period.</p>}
        </section>

        <div className="flex flex-col gap-2 sm:flex-row">
          {plan.plan !== "enterprise" && <button className="btn btn-accent justify-center" aria-busy={busy === "checkout"} disabled={Boolean(busy)} onClick={() => void act("checkout", "/settings/billing/checkout", { plan: plan.plan === "trial" ? "pro" : "enterprise" })}>{busy === "checkout" ? "Opening checkout…" : "Upgrade plan"}</button>}
          <button className="btn btn-secondary justify-center" aria-busy={busy === "portal"} disabled={Boolean(busy)} onClick={() => void act("portal", "/settings/billing/portal")}>{busy === "portal" ? "Opening billing…" : "Manage billing"}</button>
        </div>
      </div>
    </BillingPageShell>
  );
}

function BillingPageShell({ children }: { children: React.ReactNode }) {
  return (
    <main className="mobile-safe-bottom h-[100dvh] overflow-y-auto px-4 py-6 sm:px-6 sm:py-8">
      <div className="mx-auto max-w-xl">
        <header className="mb-5">
          <a href="/settings?tab=billing" className="btn btn-ghost btn-sm -ml-2 mb-3">← Settings</a>
          <h1 className="h-page">Billing &amp; plan</h1>
          <p className="mt-1 text-[13.5px]" style={{ color: "var(--text-dim)" }}>Review workspace entitlements and current-period usage.</p>
        </header>
        {children}
      </div>
    </main>
  );
}

function formatUsd(value: number): string {
  return new Intl.NumberFormat(undefined, { style: "currency", currency: "USD" }).format(value);
}
