"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

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

type AuthConfig = {
  provider: string;
  devOtp: boolean;
  cognito: {
    enabled: boolean;
    loginUrl?: string | null;
    callbackUrl?: string;
  };
};

const FALLBACK_DEV_AUTH_CONFIG: AuthConfig = {
  provider: "dev_otp",
  devOtp: true,
  cognito: { enabled: false },
};

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState("");
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);

  useEffect(() => {
    fetch(`${apiBase()}/auth/config`)
      .then(r => r.json())
      .then((data: AuthConfig) => setAuthConfig(data))
      .catch((err) => {
        console.error("Auth config fetch failed:", err);
        setError("Connection failed: Ensure the API is running on the correct port.");
        setAuthConfig(FALLBACK_DEV_AUTH_CONFIG);
      });
  }, []);

  async function requestOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    const res = await fetch(`${apiBase()}/auth/request-otp`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (!res.ok) {
      setError(await res.text());
      return;
    }
    setRequested(true);
  }

  async function verifyOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    const res = await fetch(`${apiBase()}/auth/verify-otp`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ email, code }),
    });
    if (!res.ok) {
      setError(await res.text());
      return;
    }
    await res.json();
    router.push("/chat");
  }

  function signInWithCognito() {
    const loginUrl = authConfig?.cognito?.loginUrl;
    if (!loginUrl) {
      setError("Cognito is not configured. See docs/cognito-setup.md.");
      return;
    }
    window.location.href = loginUrl;
  }

  async function signInWithSSO() {
    setError("");
    if (!email || !email.includes("@")) {
      setError("Enter your work email to continue with SSO.");
      return;
    }
    try {
      const res = await fetch(`${apiBase()}/auth/sso/start?email=${encodeURIComponent(email)}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail ?? "Single sign-on is not configured for this email domain.");
        return;
      }
      const data = await res.json();
      window.location.href = data.login_url;
    } catch {
      setError("Could not start single sign-on. Please try again.");
    }
  }

  const cognitoEnabled = authConfig?.cognito?.enabled;
  const devOtpEnabled = authConfig?.devOtp ?? false;

  const inputClass =
    "mt-2 w-full rounded-[var(--r-sm)] border px-3 py-2.5 text-[14px] outline-none smooth focus:border-[var(--accent)] focus:shadow-[0_0_0_3px_color-mix(in_oklch,var(--accent)_12%,transparent)]";

  return (
    <main className="min-h-screen px-6 py-10" style={{ background: "var(--bg)", color: "var(--text)", overflow: "auto" }}>
      <section className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-md flex-col justify-center">
        <div className="flex items-center gap-2.5 mb-6">
          <div className="w-8 h-8 rounded-xl flex items-center justify-center" style={{ background: "var(--accent)", color: "white", fontFamily: "var(--font-serif), serif", fontWeight: 600 }}>
            C
          </div>
          <h1 className="h-display" style={{ fontSize: 28 }}>Chronos</h1>
        </div>
        <p className="text-[14px] leading-6" style={{ color: "var(--text-muted)" }}>
          Chronos helps you complete work through chat, files, and durable AI tasks.
        </p>
        <p className="mt-3 text-[13.5px] leading-6" style={{ color: "var(--text-dim)" }}>
          {!authConfig
            ? "Loading sign-in options…"
            : cognitoEnabled
              ? "Sign in with your organization account."
              : "Sign in with your email to receive a one-time code."}
        </p>

        {!authConfig && (
          <div className="mt-12 flex justify-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2" style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }} />
          </div>
        )}

        {authConfig && (
          <div className="mt-8 space-y-3">
            <label className="block">
              <span className="text-[13px] font-medium" style={{ color: "var(--text-muted)" }}>Work email</span>
              <input
                className={inputClass}
                style={{ borderColor: "var(--border)", background: "var(--surface)", color: "var(--text)" }}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                type="email"
                placeholder="you@company.com"
              />
            </label>
            <button type="button" onClick={signInWithSSO} className="btn btn-secondary w-full justify-center" style={{ padding: "11px 16px" }}>
              Continue with SSO
            </button>
            <p className="text-xs" style={{ color: "var(--text-dim)" }}>
              Uses your organization's single sign-on. We route you to your identity provider by email domain.
            </p>
          </div>
        )}

        {cognitoEnabled ? (
          <div className="mt-8 space-y-4">
            <button
              type="button"
              onClick={signInWithCognito}
              className="btn btn-accent w-full justify-center"
              style={{ padding: "11px 16px" }}
            >
              Sign in with Cognito
            </button>
            {error ? <p className="text-[13px]" style={{ color: "var(--danger)" }}>{error}</p> : null}
          </div>
        ) : null}

        {devOtpEnabled ? (
          <>
            {cognitoEnabled ? (
              <div className="my-6 flex items-center gap-3">
                <div className="h-px flex-1" style={{ background: "var(--border)" }} />
                <span className="text-xs" style={{ color: "var(--text-dim)" }}>or one-time code</span>
                <div className="h-px flex-1" style={{ background: "var(--border)" }} />
              </div>
            ) : null}
            <form onSubmit={requested ? verifyOtp : requestOtp} className="mt-8 space-y-4">
              <label className="block">
                <span className="text-[13px] font-medium" style={{ color: "var(--text-muted)" }}>Email</span>
                <input
                  className={inputClass}
                  style={{ borderColor: "var(--border)", background: "var(--surface)", color: "var(--text)" }}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  type="email"
                />
              </label>
              {requested ? (
                <label className="block">
                  <span className="text-[13px] font-medium" style={{ color: "var(--text-muted)" }}>OTP</span>
                  <input
                    className={inputClass}
                    style={{ borderColor: "var(--border)", background: "var(--surface)", color: "var(--text)" }}
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                    inputMode="numeric"
                  />
                </label>
              ) : null}
              {error && !cognitoEnabled ? <p className="text-[13px]" style={{ color: "var(--danger)" }}>{error}</p> : null}
              <button className="btn btn-accent w-full justify-center" style={{ padding: "11px 16px" }}>
                {requested ? "Verify OTP" : "Request OTP"}
              </button>
            </form>
          </>
        ) : null}
        <p style={{ marginTop: 16, textAlign: "center" }}>
          <a href="/signup">Create an organization</a>
        </p>
      </section>
    </main>
  );
}
