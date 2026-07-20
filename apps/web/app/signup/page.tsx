"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";
import { PublicProductLinks } from "../../components/system/PublicProductLinks";

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

export default function SignupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [orgName, setOrgName] = useState("");
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function requestOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/auth/request-otp`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
        credentials: "include",
      });
      if (!res.ok) throw new Error("Could not send a verification code. Is signup enabled?");
      setRequested(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function submitSignup(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, code, org_name: orgName || undefined }),
        credentials: "include",
      });
      if (!res.ok) throw new Error(await res.text());
      const body = await res.json();
      // A newly created org goes through the first-run onboarding wizard.
      const dest = body.created ? "/onboarding" : "/chat";
      // Prod: land on the org's subdomain. Dev single-host: route in place.
      if (typeof window !== "undefined") {
        const host = window.location.host;
        const isLocal = host.includes("localhost") || host.startsWith("127.");
        if (!isLocal && body.subdomain) {
          const baseDomain = host.split(".").slice(-2).join(".");
          window.location.href = `${window.location.protocol}//${body.subdomain}.${baseDomain}${dest}`;
          return;
        }
      }
      router.push(dest);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Signup failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="h-[100dvh] overflow-y-auto px-4 py-8 sm:px-6 sm:py-10" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <section className="mx-auto flex min-h-[calc(100dvh-4rem)] w-full max-w-md flex-col justify-center sm:min-h-[calc(100dvh-5rem)]">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl" style={{ background: "var(--accent)", color: "white", fontFamily: "var(--font-serif), serif", fontWeight: 600 }}>C</div>
          <span className="text-[24px]" style={{ fontFamily: "var(--font-serif), serif", fontWeight: 500 }}>Chronos</span>
        </div>
        <div className="surface rounded-2xl border border-soft p-5 sm:p-6" style={{ boxShadow: "var(--shadow-md)" }}>
          <div className="mb-5 flex items-center gap-2" aria-label={`Step ${requested ? 2 : 1} of 2`}>
            {[1, 2].map(item => <span key={item} className="h-1.5 flex-1 rounded-full" style={{ background: item <= (requested ? 2 : 1) ? "var(--accent)" : "var(--border-soft)" }}/>) }
          </div>
          <h1 className="h-page">Create your organization</h1>
          <p className="mt-2 text-[13.5px] leading-6" style={{ color: "var(--text-dim)" }}>
            {requested ? "Enter the code from your email, then name your workspace." : "Start with your work email. We’ll send a short verification code."}
          </p>
          {error && <p role="alert" className="mt-4 rounded-lg border px-3 py-2 text-[13px]" style={{ borderColor: "var(--danger)", background: "var(--danger-soft)", color: "var(--danger)" }}>{error}</p>}
          {!requested ? (
            <form onSubmit={requestOtp} className="mt-6 space-y-4">
              <label className="block text-[13px] font-medium">
                Work email
                <input
                  className="mt-2 w-full rounded-lg border border-soft px-3 py-2.5 outline-none surface"
                  type="email"
                  autoComplete="email"
                  required
                  placeholder="you@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </label>
              <button className="btn btn-accent w-full justify-center" type="submit" disabled={busy}>{busy ? "Sending…" : "Send verification code"}</button>
            </form>
          ) : (
            <form onSubmit={submitSignup} className="mt-6 space-y-4">
              <label className="block text-[13px] font-medium">
                Verification code
                <input
                  autoFocus
                  className="mt-2 w-full rounded-lg border border-soft px-3 py-2.5 outline-none surface"
                  required
                  inputMode="numeric"
                  autoComplete="one-time-code"
                  placeholder="000000"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                />
              </label>
              <label className="block text-[13px] font-medium">
                Organization name <span className="font-normal" style={{ color: "var(--text-dim)" }}>(optional)</span>
                <input
                  className="mt-2 w-full rounded-lg border border-soft px-3 py-2.5 outline-none surface"
                  autoComplete="organization"
                  placeholder="Acme"
                  value={orgName}
                  onChange={(e) => setOrgName(e.target.value)}
                />
              </label>
              <button className="btn btn-accent w-full justify-center" type="submit" disabled={busy}>{busy ? "Creating…" : "Create organization"}</button>
            </form>
          )}
        </div>
        <p className="mt-5 text-center text-[13px]" style={{ color: "var(--text-dim)" }}>
          Already have an account? <a className="underline underline-offset-2" href="/login" style={{ color: "var(--accent-text)" }}>Sign in</a>
        </p>
        <PublicProductLinks discloseSessionCookie className="mt-5" />
      </section>
    </main>
  );
}
