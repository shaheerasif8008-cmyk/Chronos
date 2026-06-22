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
  const res = await fetch(`${apiBase()}${path}`, { credentials: "include" });
  if (!res.ok) throw new Error(await res.text());
  return res.json() as Promise<T>;
}

export default function BillingPage() {
  const [plan, setPlan] = useState<PlanResp | null>(null);
  const [usage, setUsage] = useState<UsageResp | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    Promise.all([getJson<PlanResp>("/settings/plan"), getJson<UsageResp>("/settings/billing/usage")])
      .then(([p, u]) => {
        setPlan(p);
        setUsage(u);
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load billing"));
  }, []);

  async function act(path: string, body?: object) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body ? JSON.stringify(body) : undefined,
        credentials: "include",
      });
      if (res.status === 503) {
        setNotice("Billing is not yet configured for this deployment. Plan changes are managed by an administrator.");
        return;
      }
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      const url = data.checkout_url || data.portal_url;
      if (url) window.location.href = url;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed");
    } finally {
      setBusy(false);
    }
  }

  if (error) return <p role="alert" style={{ color: "crimson", margin: "8vh auto", maxWidth: 560 }}>{error}</p>;
  if (!plan || !usage) return <p style={{ margin: "8vh auto", maxWidth: 560 }}>Loading billing…</p>;

  const ent = plan.entitlements;
  return (
    <div style={{ maxWidth: 560, margin: "6vh auto", display: "flex", flexDirection: "column", gap: 20 }}>
      <h1>Billing & plan</h1>
      {notice && <p style={{ color: "var(--text-muted, #888)" }}>{notice}</p>}

      <section style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <h2>Current plan: {plan.plan}</h2>
        <ul>
          <li>Seats: {plan.usage.seats_used} / {ent.max_seats}</li>
          <li>Daily cost budget: {ent.daily_cost_limit_usd ? `$${ent.daily_cost_limit_usd}` : "unlimited"}</li>
          <li>Daily token budget: {ent.daily_token_limit ? ent.daily_token_limit.toLocaleString() : "unlimited"}</li>
          <li>Features: {ent.features.join(", ")}</li>
        </ul>
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <h2>Usage this period ({usage.period})</h2>
        <ul>
          <li>Tokens: {usage.tokens.toLocaleString()}</li>
          <li>Cost: ${usage.cost_usd.toFixed(2)}</li>
          {usage.over_budget && <li style={{ color: "crimson" }}>Over budget for this period</li>}
        </ul>
      </section>

      <div style={{ display: "flex", gap: 10 }}>
        {plan.plan !== "enterprise" && (
          <button disabled={busy} onClick={() => act("/settings/billing/checkout", { plan: plan.plan === "trial" ? "pro" : "enterprise" })}>
            Upgrade plan
          </button>
        )}
        <button disabled={busy} onClick={() => act("/settings/billing/portal")} style={{ background: "transparent" }}>
          Manage billing
        </button>
      </div>
    </div>
  );
}
