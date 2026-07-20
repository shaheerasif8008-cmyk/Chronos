"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { apiFetch } from "../../lib/api";

type CreatedInvitation = {
  id: string;
  email: string;
  delivery_status: "sent" | "manual_required" | string;
  invite_url?: string;
};

type ReadinessCheck = {
  id: string;
  label: string;
  status: "healthy" | "degraded" | "unavailable";
  summary: string;
  remediation?: string;
};

type RuntimeReadiness = {
  status: "ready" | "degraded" | "blocked";
  can_complete_onboarding: boolean;
  checked_at: string;
  required: ReadinessCheck[];
  optional: ReadinessCheck[];
  summary: {
    required_healthy: number;
    required_total: number;
    optional_degraded: number;
    optional_total: number;
  };
};

type FirstUseStep = {
  id: string;
  label: string;
  description: string;
  href: string;
  complete: boolean;
  evidence_count: number;
};

type FirstUseGuide = {
  complete: number;
  total: number;
  steps: FirstUseStep[];
};

function readinessColor(status: ReadinessCheck["status"] | RuntimeReadiness["status"]): string {
  if (status === "healthy" || status === "ready") return "var(--ok)";
  if (status === "unavailable" || status === "blocked") return "var(--danger)";
  return "var(--warn)";
}

export default function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [invite, setInvite] = useState("");
  const [invitations, setInvitations] = useState<CreatedInvitation[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [readiness, setReadiness] = useState<RuntimeReadiness | null>(null);
  const [checking, setChecking] = useState(true);
  const [guide, setGuide] = useState<FirstUseGuide | null>(null);

  const loadReadiness = useCallback(async (refresh: boolean): Promise<RuntimeReadiness | null> => {
    setChecking(true);
    setError("");
    try {
      const response = await apiFetch(`/settings/runtime-health${refresh ? "?refresh=true" : ""}`);
      const report = await response.json() as RuntimeReadiness;
      setReadiness(report);
      return report;
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Workspace readiness could not be checked.");
      return null;
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    void loadReadiness(false);
  }, [loadReadiness]);

  async function copyInvite(created: CreatedInvitation) {
    if (!created.invite_url) return;
    try {
      await navigator.clipboard.writeText(created.invite_url);
      setCopiedId(created.id);
    } catch {
      setError("Could not copy the invitation link. Select it and copy it manually.");
    }
  }

  async function sendInvite(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await apiFetch("/settings/invitations", {
        method: "POST",
        body: JSON.stringify({ email: invite, role: "viewer" }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(typeof body.detail === "string" ? body.detail : "Invitation could not be created");
      }
      setInvitations((prev) => [body as CreatedInvitation, ...prev]);
      setInvite("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invite failed");
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setError("");
    setBusy(true);
    try {
      const current = await loadReadiness(true);
      if (!current?.can_complete_onboarding) {
        throw new Error("Required runtime services are not ready. Resolve the blockers and check again.");
      }
      await apiFetch("/settings/onboarding/complete", { method: "POST" });
      const guideResponse = await apiFetch("/settings/onboarding/guide");
      setGuide(await guideResponse.json() as FirstUseGuide);
      setStep(3);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not finish setup");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="h-[100dvh] overflow-y-auto px-4 py-8 sm:px-6 sm:py-10" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <section className="mx-auto flex min-h-[calc(100dvh-4rem)] w-full max-w-lg flex-col justify-center sm:min-h-[calc(100dvh-5rem)]">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl" style={{ background: "var(--accent)", color: "white", fontFamily: "var(--font-serif), serif", fontWeight: 600 }}>C</div>
          <span className="text-[24px]" style={{ fontFamily: "var(--font-serif), serif", fontWeight: 500 }}>Chronos</span>
        </div>
        <div className="surface rounded-2xl border border-soft p-5 sm:p-7" style={{ boxShadow: "var(--shadow-md)" }}>
          <div className="mb-5 flex items-center gap-2" role="progressbar" aria-label="Onboarding progress" aria-valuemin={1} aria-valuemax={3} aria-valuenow={step} aria-valuetext={`Step ${step} of 3`}>
            {[1, 2, 3].map(item => <span key={item} className="h-1.5 flex-1 rounded-full" style={{ background: item <= step ? "var(--accent)" : "var(--border-soft)" }}/>) }
          </div>
          {step === 1 ? (
            <>
              <h1 className="h-page">Workspace readiness</h1>
              <p className="mt-2 text-[14px] leading-6" style={{ color: "var(--text-dim)" }}>Chronos checks the services required for safe task execution before setup can finish. Optional capabilities can be configured later without blocking the workspace.</p>
              {error && <p role="alert" className="mt-4 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}>{error}</p>}
              <div className="mt-5 surface overflow-hidden rounded-xl border border-soft" aria-busy={checking}>
                {checking && !readiness ? (
                  <p className="px-4 py-6 text-[13px]" role="status" style={{ color: "var(--text-dim)" }}>Checking required services…</p>
                ) : readiness ? (
                  <>
                    <div className="flex items-center justify-between gap-3 border-b hairline px-4 py-3">
                      <div>
                        <div className="text-[13.5px] font-medium">Required services</div>
                        <div className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{readiness.summary.required_healthy} of {readiness.summary.required_total} healthy</div>
                      </div>
                      <span className="text-[12px] font-semibold" style={{ color: readinessColor(readiness.status) }}>{readiness.can_complete_onboarding ? "Ready" : "Action required"}</span>
                    </div>
                    <ul className="m-0 list-none p-0">
                      {readiness.required.map(check => (
                        <li key={check.id} className="border-b hairline px-4 py-3 last:border-b-0">
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <div className="text-[13px] font-medium">{check.label}</div>
                              {check.status !== "healthy" && <p className="mt-1 text-[11.5px] leading-4" style={{ color: "var(--text-dim)" }}>{check.summary}</p>}
                              {check.status !== "healthy" && check.remediation && <p className="mt-1 text-[11.5px] leading-4" style={{ color: "var(--text-muted)" }}>{check.remediation}</p>}
                            </div>
                            <span className="flex-shrink-0 text-[11.5px] font-semibold" style={{ color: readinessColor(check.status) }}>{check.status === "healthy" ? "Healthy" : check.status === "degraded" ? "Degraded" : "Unavailable"}</span>
                          </div>
                        </li>
                      ))}
                    </ul>
                    <div className="border-t hairline px-4 py-3 text-[11.5px]" style={{ color: "var(--text-dim)" }}>
                      {readiness.summary.optional_degraded > 0
                        ? `${readiness.summary.optional_degraded} optional capabilities need configuration; core workspace use is still available.`
                        : "All optional capabilities are healthy."}
                    </div>
                  </>
                ) : (
                  <p className="px-4 py-6 text-[13px]" role="alert" style={{ color: "var(--danger)" }}>Readiness could not be loaded.</p>
                )}
              </div>
              <div className="mt-6 flex flex-col gap-2 sm:flex-row">
                <button type="button" className="btn btn-secondary flex-1 justify-center" onClick={() => void loadReadiness(true)} disabled={checking}>{checking ? "Checking…" : "Check again"}</button>
                <button type="button" className="btn btn-accent flex-1 justify-center" onClick={() => setStep(2)} disabled={checking || !readiness?.can_complete_onboarding}>Continue</button>
              </div>
            </>
          ) : step === 2 ? (
            <>
              <button type="button" className="btn btn-ghost btn-sm -ml-2 mb-3" onClick={() => setStep(1)}>Back</button>
              <h1 className="h-page">Invite your team</h1>
              <p className="mt-2 text-[14px] leading-6" style={{ color: "var(--text-dim)" }}>Create least-privilege viewer invitations. You can change roles after teammates join.</p>
              {error && <p role="alert" className="mt-4 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}>{error}</p>}
              <form onSubmit={sendInvite} className="mt-6 flex flex-col gap-2 sm:flex-row">
                <label className="sr-only" htmlFor="invite-email">Teammate email</label>
                <input
                  id="invite-email"
                  className="surface min-w-0 flex-1 rounded-lg border border-soft px-3 py-2.5 outline-none"
                  type="email"
                  autoComplete="email"
                  required
                  placeholder="teammate@yourcompany.com"
                  value={invite}
                  onChange={(e) => setInvite(e.target.value)}
                />
                <button className="btn btn-accent justify-center" type="submit" disabled={busy}>{busy ? "Creating…" : "Create invitation"}</button>
              </form>
              {invitations.length > 0 && (
                <ul className="mt-5 grid list-none gap-3 p-0">
                  {invitations.map((created) => (
                    <li key={created.id} className="rounded-xl border border-soft p-3 text-[13px]">
                      {created.delivery_status === "sent" ? (
                        <p>Invitation email sent to <strong>{created.email}</strong>.</p>
                      ) : (
                        <div className="grid gap-2">
                          <p role="status">Invitation created for <strong>{created.email}</strong>, but email delivery is unavailable. Copy this secure link and send it directly.</p>
                          {created.invite_url ? (
                            <div className="flex flex-col gap-2 sm:flex-row">
                              <input className="surface min-w-0 flex-1 rounded-lg border border-soft px-3 py-2" aria-label={`Invitation link for ${created.email}`} readOnly value={created.invite_url} />
                              <button className="btn btn-secondary justify-center" type="button" onClick={() => void copyInvite(created)}>{copiedId === created.id ? "Copied" : "Copy link"}</button>
                            </div>
                          ) : null}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              <button type="button" className="btn btn-accent mt-6 w-full justify-center" onClick={() => void finish()} disabled={busy || checking || !readiness?.can_complete_onboarding}>{busy ? "Saving…" : "Continue to first-use guide"}</button>
            </>
          ) : (
            <>
              <h1 className="h-page">Complete your first workflow</h1>
              <p className="mt-2 text-[14px] leading-6" style={{ color: "var(--text-dim)" }}>These steps use real workspace records, so progress stays accurate across browsers. They are optional now and remain available from the welcome panel.</p>
              {error && <p role="alert" className="mt-4 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}>{error}</p>}
              <div className="mt-5 grid gap-2">
                {(guide?.steps ?? []).map((item, index) => (
                  <a key={item.id} href={item.href} className="surface flex items-start gap-3 rounded-xl border border-soft p-3.5 hover:border-[var(--accent)]">
                    <span className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full text-[12px] font-semibold" style={{ background: item.complete ? "var(--ok-soft)" : "var(--surface-2)", color: item.complete ? "var(--ok-text)" : "var(--text-dim)" }}>{index + 1}</span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-[13.5px] font-medium">{item.label}</span>
                      <span className="mt-0.5 block text-[12px] leading-5" style={{ color: "var(--text-dim)" }}>{item.description}</span>
                    </span>
                    <span className="text-[11.5px] font-semibold" style={{ color: item.complete ? "var(--ok-text)" : "var(--accent-text)" }}>{item.complete ? "Done" : "Open"}</span>
                  </a>
                ))}
              </div>
              {guide && <p className="mt-4 text-center text-[12.5px]" role="status" style={{ color: "var(--text-dim)" }}>{guide.complete} of {guide.total} first-use steps complete</p>}
              <button type="button" className="btn btn-accent mt-5 w-full justify-center" onClick={() => router.push("/chat")}>Go to Chronos</button>
            </>
          )}
        </div>
      </section>
    </main>
  );
}
