"use client";

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "../../lib/api";

type HealthStatus = "healthy" | "degraded" | "unavailable";

type HealthCheck = {
  id: string;
  label: string;
  required: boolean;
  status: HealthStatus;
  summary: string;
  remediation?: string;
  metadata?: {
    active_replicas?: number;
    last_seen_at?: string | null;
    age_seconds?: number | null;
    checked_at?: string | null;
    verified_at?: string | null;
    latency_ms?: number | null;
  };
};

type RuntimeHealthReport = {
  status: "ready" | "degraded" | "blocked";
  can_complete_onboarding: boolean;
  environment: string;
  checked_at: string;
  required: HealthCheck[];
  optional: HealthCheck[];
  summary: {
    required_healthy: number;
    required_total: number;
    optional_degraded: number;
    optional_total: number;
  };
  admin_actions_available: boolean;
};

function statusColor(status: HealthStatus | RuntimeHealthReport["status"]): string {
  if (status === "healthy" || status === "ready") return "var(--ok)";
  if (status === "blocked" || status === "unavailable") return "var(--danger)";
  return "var(--warn)";
}

function statusLabel(status: HealthStatus | RuntimeHealthReport["status"]): string {
  if (status === "healthy") return "Healthy";
  if (status === "unavailable") return "Unavailable";
  if (status === "ready") return "Ready";
  if (status === "blocked") return "Action required";
  return "Degraded";
}

function checkTimestamp(check: HealthCheck): string | null {
  const raw = check.metadata?.last_seen_at || check.metadata?.verified_at || check.metadata?.checked_at;
  if (!raw) return null;
  const date = new Date(raw);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
}

function HealthRows({ checks, canAdmin }: { checks: HealthCheck[]; canAdmin: boolean }) {
  return (
    <ul className="m-0 list-none p-0">
      {checks.map((check) => {
        const timestamp = checkTimestamp(check);
        return (
          <li key={check.id} className="border-b hairline px-4 py-4 last:border-b-0 sm:px-5">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-5">
              <div className="min-w-0">
                <div className="text-[14px] font-medium">{check.label}</div>
                <p className="mt-1 text-[12.5px] leading-5" style={{ color: "var(--text-dim)" }}>
                  {check.summary}
                </p>
                {(timestamp || check.metadata?.active_replicas !== undefined) && (
                  <p className="mt-1 text-[11px] font-mono" style={{ color: "var(--text-faint)" }}>
                    {check.metadata?.active_replicas !== undefined
                      ? `${check.metadata.active_replicas} active replica${check.metadata.active_replicas === 1 ? "" : "s"}`
                      : null}
                    {check.metadata?.active_replicas !== undefined && timestamp ? " · " : null}
                    {timestamp ? `Last verified ${timestamp}` : null}
                    {check.metadata?.latency_ms != null ? ` · ${check.metadata.latency_ms} ms` : null}
                  </p>
                )}
                {canAdmin && check.remediation && check.status !== "healthy" && (
                  <p className="mt-2 rounded-lg px-3 py-2 text-[12px]" style={{ background: "var(--surface-2)", color: "var(--text-muted)" }}>
                    <span className="font-medium" style={{ color: "var(--text)" }}>Admin action:</span> {check.remediation}
                  </p>
                )}
              </div>
              <span
                className="flex-shrink-0 text-[12px] font-semibold"
                style={{ color: statusColor(check.status) }}
                aria-label={`${check.label}: ${statusLabel(check.status)}`}
              >
                {statusLabel(check.status)}
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

export default function RuntimeHealthPanel({ canAdmin }: { canAdmin: boolean }) {
  const [report, setReport] = useState<RuntimeHealthReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async (refresh = false) => {
    if (refresh) setRefreshing(true);
    else setLoading(true);
    setError("");
    try {
      const response = await apiFetch(`/settings/runtime-health${refresh ? "?refresh=true" : ""}`);
      setReport(await response.json() as RuntimeHealthReport);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Runtime health could not be loaded.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load(false);
  }, [load]);

  if (loading) {
    return (
      <section className="mb-8" aria-labelledby="runtime-health-heading">
        <h2 id="runtime-health-heading" className="mb-3 text-[16px] font-semibold">Runtime health</h2>
        <div className="surface rounded-xl border border-soft px-5 py-8 text-[13px]" role="status" style={{ color: "var(--text-dim)" }}>
          Checking required services…
        </div>
      </section>
    );
  }

  if (!report) {
    return (
      <section className="mb-8" aria-labelledby="runtime-health-heading">
        <h2 id="runtime-health-heading" className="mb-3 text-[16px] font-semibold">Runtime health</h2>
        <div className="surface rounded-xl border border-soft px-5 py-4 text-[13px]" role="alert" style={{ color: "var(--danger)" }}>
          {error || "Runtime health is unavailable."}
          <button className="btn btn-secondary btn-sm ml-3" type="button" onClick={() => void load(false)}>Try again</button>
        </div>
      </section>
    );
  }

  return (
    <section className="mb-8" aria-labelledby="runtime-health-heading">
      <div className="mb-3 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="runtime-health-heading" className="text-[16px] font-semibold">Runtime health</h2>
            <span className="text-[12px] font-semibold" style={{ color: statusColor(report.status) }}>
              {statusLabel(report.status)}
            </span>
          </div>
          <p className="mt-1 text-[13px]" style={{ color: "var(--text-dim)" }}>
            {report.summary.required_healthy}/{report.summary.required_total} required services healthy
            {report.summary.optional_degraded > 0 ? ` · ${report.summary.optional_degraded} optional capabilities degraded` : " · optional capabilities healthy"}
          </p>
        </div>
        <button
          className="btn btn-secondary btn-sm justify-center"
          type="button"
          disabled={refreshing}
          onClick={() => void load(canAdmin)}
        >
          {refreshing ? "Checking…" : canAdmin ? "Run live checks" : "Refresh status"}
        </button>
      </div>

      <div aria-live="polite" aria-atomic="true" className="sr-only">
        Runtime health is {statusLabel(report.status)}. {report.summary.required_healthy} of {report.summary.required_total} required services are healthy.
      </div>
      {error && <p role="alert" className="mb-3 text-[13px]" style={{ color: "var(--danger)" }}>{error}</p>}

      <div className="mb-4">
        <div className="mb-2 flex items-baseline justify-between gap-3">
          <h3 className="text-[13px] font-semibold">Required services</h3>
          <span className="text-[11px]" style={{ color: "var(--text-dim)" }}>Affects workspace readiness</span>
        </div>
        <div className="surface overflow-hidden rounded-xl border border-soft">
          <HealthRows checks={report.required} canAdmin={canAdmin} />
        </div>
      </div>

      <div>
        <div className="mb-2 flex items-baseline justify-between gap-3">
          <h3 className="text-[13px] font-semibold">Optional capabilities</h3>
          <span className="text-[11px]" style={{ color: "var(--text-dim)" }}>Does not block core workspace use</span>
        </div>
        <div className="surface overflow-hidden rounded-xl border border-soft">
          <HealthRows checks={report.optional} canAdmin={canAdmin} />
        </div>
      </div>
    </section>
  );
}
