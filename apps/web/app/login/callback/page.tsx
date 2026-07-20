"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;
type CallbackResult = { ok: boolean; status: number; body: Record<string, unknown> };
let activeCodeExchange: { code: string; promise: Promise<CallbackResult> } | null = null;

// A redirect target is only safe if it is a same-site path. Bare `startsWith("/")`
// is not enough: `//evil.com` and `/\evil.com` are protocol-relative URLs that
// the router treats as external navigations (open redirect). Require a single
// leading slash followed by a non-slash, non-backslash character.
function isSafeInternalPath(target: string): boolean {
  return /^\/(?![/\\])/.test(target);
}

function isSafeTenantRedirect(target: string): boolean {
  try {
    const url = new URL(target);
    const currentLabels = window.location.hostname.toLowerCase().split(".");
    const baseDomain = currentLabels.slice(-2).join(".");
    return url.protocol === "https:" && (
      url.hostname.toLowerCase() === baseDomain
      || url.hostname.toLowerCase().endsWith(`.${baseDomain}`)
    );
  } catch {
    return false;
  }
}

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

function CallbackStatus({ error = "" }: { error?: string }) {
  return (
    <main className="h-[100dvh] overflow-y-auto px-4 py-8 sm:px-6 sm:py-10" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <section className="mx-auto flex min-h-[calc(100dvh-4rem)] max-w-md flex-col justify-center sm:min-h-[calc(100dvh-5rem)]">
        <div className="mb-6 flex items-center gap-2.5">
          <div className="flex h-8 w-8 items-center justify-center rounded-xl" style={{ background: "var(--accent)", color: "white", fontFamily: "var(--font-serif), serif", fontWeight: 600 }}>C</div>
          <span className="text-[24px]" style={{ fontFamily: "var(--font-serif), serif", fontWeight: 500 }}>Chronos</span>
        </div>
        <div className="surface rounded-2xl border border-soft p-5 sm:p-6" style={{ boxShadow: "var(--shadow-md)" }} role={error ? "alert" : "status"}>
          <h1 className="h-page">{error ? "Sign-in could not be completed" : "Signing in…"}</h1>
          <p className="mt-3 text-[14px] leading-6" style={{ color: error ? "var(--danger)" : "var(--text-dim)" }}>
            {error || "Completing your secure organization sign-in."}
          </p>
          {error && <a className="btn btn-secondary mt-5 justify-center" href="/login">Return to sign in</a>}
        </div>
      </section>
    </main>
  );
}

function CognitoCallbackInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [error, setError] = useState("");
  // Prevent React 18 Strict Mode from exchanging the same single-use code twice.
  const exchanged = useRef(false);

  useEffect(() => {
    if (exchanged.current) return;
    exchanged.current = true;

    const redirect = params.get("redirect");
    if (redirect) {
      router.replace(isSafeInternalPath(redirect) ? redirect : "/chat");
      return;
    }

    const code = params.get("code");
    const state = params.get("state");
    const oauthError = params.get("error_description") || params.get("error");
    if (oauthError) {
      setError(oauthError);
      return;
    }
    if (!code) {
      setError("Missing authorization code from Cognito.");
      return;
    }

    const redirectUri = `${window.location.origin}/login/callback`;

    (async () => {
      try {
        const existing = activeCodeExchange?.code === code ? activeCodeExchange.promise : null;
        const promise = existing ?? fetch(`${apiBase()}/auth/cognito/callback`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "include",
            body: JSON.stringify({ code, state, redirect_uri: redirectUri }),
          }).then(async (res) => ({
            ok: res.ok,
            status: res.status,
            body: await res.json().catch(() => ({})),
          }));
        activeCodeExchange = { code, promise };
        const result = await promise;
        if (!result.ok) {
          setError(typeof result.body.detail === "string" ? result.body.detail : `Sign-in failed (${result.status})`);
          return;
        }
        const tenantRedirect = typeof result.body.redirect_url === "string" ? result.body.redirect_url : "";
        if (tenantRedirect && isSafeTenantRedirect(tenantRedirect)) {
          window.location.replace(tenantRedirect);
          return;
        }
        router.replace("/chat");
      } catch {
        setError("Could not complete sign-in. Please try again.");
      }
    })();
  }, [params, router]);

  return <CallbackStatus error={error}/>;
}

export default function CognitoCallbackPage() {
  return (
    <Suspense fallback={<CallbackStatus/>}>
      <CognitoCallbackInner />
    </Suspense>
  );
}
