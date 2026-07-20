"use client";

import { FormEvent, useEffect, useState } from "react";
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

type AuthConfig = {
  provider: string;
  devOtp: boolean;
  cognito: {
    enabled: boolean;
    loginUrl?: string | null;
    callbackUrl?: string;
    requiresTenant?: boolean;
    tenant?: string | null;
  };
};

type InvitationContext = {
  email: string;
  role: string;
  organization_name: string;
  tenant: string;
  expires_at?: string | null;
};

const UNAVAILABLE_AUTH_CONFIG: AuthConfig = {
  provider: "unavailable",
  devOtp: false,
  cognito: { enabled: false },
};

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState("");
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [workspace, setWorkspace] = useState("");
  const [cognitoBusy, setCognitoBusy] = useState(false);
  const [invitation, setInvitation] = useState<InvitationContext | null>(null);

  useEffect(() => {
    void (async () => {
      const hostname = window.location.hostname.toLowerCase();
      const labels = hostname.split(".");
      const reserved = new Set(["app", "www", "api", "admin", "static", "assets"]);
      let tenant = labels.length >= 3 && !reserved.has(labels[0]) ? labels[0] : "";
      const inviteToken = new URLSearchParams(window.location.search).get("invite");
      if (inviteToken) {
        try {
          const res = await fetch(`${apiBase()}/auth/invitations/${encodeURIComponent(inviteToken)}`);
          const body = await res.json().catch(() => ({}));
          if (!res.ok) {
            setError(typeof body.detail === "string" ? body.detail : "Invitation is invalid or expired.");
          } else {
            const context = body as InvitationContext;
            setInvitation(context);
            setEmail(context.email);
            tenant = context.tenant || tenant;
          }
        } catch {
          setError("Could not verify this invitation. Please try again.");
        }
      }
      if (tenant) setWorkspace(tenant);
      const query = tenant ? `?tenant=${encodeURIComponent(tenant)}` : "";
      try {
        const response = await fetch(`${apiBase()}/auth/config${query}`, { credentials: "include" });
        if (!response.ok) throw new Error(`Auth config failed (${response.status})`);
        setAuthConfig(await response.json() as AuthConfig);
      } catch (err) {
        console.error("Auth config fetch failed:", err);
        setError("Connection failed: Ensure the API is running on the correct port.");
        setAuthConfig(UNAVAILABLE_AUTH_CONFIG);
      }
    })();
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

  async function signInWithCognito() {
    setError("");
    let loginUrl = authConfig?.cognito?.loginUrl;
    if (!loginUrl && authConfig?.cognito?.requiresTenant) {
      const tenant = workspace.trim().toLowerCase();
      if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(tenant)) {
        setError("Enter your organization workspace, such as novatech.");
        return;
      }
      setCognitoBusy(true);
      try {
        const res = await fetch(`${apiBase()}/auth/config?tenant=${encodeURIComponent(tenant)}`, {
          credentials: "include",
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          setError(typeof body.detail === "string" ? body.detail : "Organization not found.");
          return;
        }
        const scoped = body as AuthConfig;
        setAuthConfig(scoped);
        loginUrl = scoped.cognito.loginUrl;
      } catch {
        setError("Could not load your organization sign-in. Please try again.");
        return;
      } finally {
        setCognitoBusy(false);
      }
    }
    if (!loginUrl) {
      setError("Cognito sign-in is unavailable for this organization.");
      return;
    }
    window.location.assign(loginUrl);
  }

  async function signInWithSSO() {
    setError("");
    if (!email || !email.includes("@")) {
      setError("Enter your work email to continue with SSO.");
      return;
    }
    try {
      const res = await fetch(`${apiBase()}/auth/sso/start?email=${encodeURIComponent(email)}`, {
        credentials: "include",
      });
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
  const authUnavailable = authConfig?.provider === "unavailable";

  const inputClass =
    "mt-2 w-full rounded-[var(--r-sm)] border px-3 py-2.5 text-[16px] outline-none smooth focus:border-[var(--accent)] focus:shadow-[0_0_0_3px_color-mix(in_oklch,var(--accent)_12%,transparent)] sm:text-[14px]";

  return (
    <main className="h-[100dvh] px-4 py-8 sm:px-6 sm:py-10" style={{ background: "var(--bg)", color: "var(--text)", overflow: "auto" }}>
      <section className="mx-auto flex min-h-[calc(100dvh-4rem)] max-w-md flex-col justify-center sm:min-h-[calc(100dvh-5rem)]">
        <div className="flex items-center gap-2.5 mb-6">
          <div className="w-8 h-8 rounded-xl flex items-center justify-center" style={{ background: "var(--accent)", color: "white", fontFamily: "var(--font-serif), serif", fontWeight: 600 }}>
            C
          </div>
          <h1 className="h-display" style={{ fontSize: 28 }}>Chronos</h1>
        </div>
        <p className="text-[14px] leading-6" style={{ color: "var(--text-muted)" }}>
          Chronos helps you complete work through chat, files, and durable AI tasks.
        </p>

        {invitation ? (
          <div className="mt-5 rounded-lg border px-4 py-3 text-[13px]" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>
            <strong>You&apos;re invited to {invitation.organization_name}.</strong>
            <div className="mt-1" style={{ color: "var(--text-muted)" }}>
              Sign in as {invitation.email} to accept the {invitation.role} invitation.
            </div>
          </div>
        ) : null}
        <p className="mt-3 text-[13.5px] leading-6" style={{ color: "var(--text-dim)" }}>
          {!authConfig
            ? "Loading sign-in options…"
            : authUnavailable
              ? "Sign-in services are temporarily unavailable."
              : cognitoEnabled
              ? "Sign in with your organization account."
              : devOtpEnabled
                ? "Sign in with your email to receive a one-time code."
                : "Sign in with your organization single sign-on."}
        </p>

        {!authConfig && (
          <div className="mt-12 flex justify-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2" style={{ borderColor: "var(--border)", borderTopColor: "var(--accent)" }} />
          </div>
        )}

        {authUnavailable && (
          <div className="mt-8 rounded-xl border p-4" style={{ borderColor: "var(--danger)", background: "var(--surface)" }} role="alert">
            <div className="text-[14px] font-semibold">Chronos cannot reach its authentication service.</div>
            <p className="mt-1 text-[13px] leading-5" style={{ color: "var(--text-muted)" }}>{error || "Please try again in a moment."}</p>
            <button type="button" onClick={() => window.location.reload()} className="btn btn-secondary mt-4 w-full justify-center">Retry connection</button>
          </div>
        )}

        {authConfig && !authUnavailable && (
          <div className="mt-8 space-y-3">
            <label className="block">
              <span className="text-[13px] font-medium" style={{ color: "var(--text-muted)" }}>Work email</span>
              <input
                className={inputClass}
                style={{ borderColor: "var(--border)", background: "var(--surface)", color: "var(--text)" }}
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                readOnly={Boolean(invitation)}
                type="email"
                placeholder="you@company.com"
              />
            </label>
            <button type="button" onClick={signInWithSSO} className="btn btn-secondary w-full justify-center" style={{ padding: "11px 16px" }}>
              Continue with SSO
            </button>
            <p className="text-xs" style={{ color: "var(--text-dim)" }}>
              Uses your organization&apos;s single sign-on. We route you to your identity provider by email domain.
            </p>
          </div>
        )}

        {cognitoEnabled ? (
          <div className="mt-8 space-y-4">
            {authConfig?.cognito?.requiresTenant && !authConfig.cognito.tenant ? (
              <label className="block">
                <span className="text-[13px] font-medium" style={{ color: "var(--text-muted)" }}>Organization workspace</span>
                <div className="relative">
                  <input
                    className={`${inputClass} sm:pr-40`}
                    style={{ borderColor: "var(--border)", background: "var(--surface)", color: "var(--text)" }}
                    value={workspace}
                    onChange={(event) => setWorkspace(event.target.value.toLowerCase())}
                    autoCapitalize="none"
                    autoCorrect="off"
                    placeholder="novatech"
                    aria-describedby="workspace-domain"
                  />
                  <span id="workspace-domain" className="pointer-events-none absolute right-3 top-1/2 mt-1 hidden -translate-y-1/2 text-xs sm:block" style={{ color: "var(--text-dim)" }}>
                    .cognisiatech.com
                  </span>
                </div>
                <span className="mt-1 block text-xs sm:hidden" style={{ color: "var(--text-dim)" }}>Your workspace URL ends in .cognisiatech.com</span>
              </label>
            ) : null}
            <button
              type="button"
              onClick={signInWithCognito}
              disabled={cognitoBusy}
              className="btn btn-accent w-full justify-center"
              style={{ padding: "11px 16px" }}
            >
              {cognitoBusy ? "Loading organization…" : "Sign in with Cognito"}
            </button>
            {error ? <p role="alert" className="text-[13px]" style={{ color: "var(--danger)" }}>{error}</p> : null}
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
              {error && !cognitoEnabled ? <p role="alert" className="text-[13px]" style={{ color: "var(--danger)" }}>{error}</p> : null}
              <button className="btn btn-accent w-full justify-center" style={{ padding: "11px 16px" }}>
                {requested ? "Verify OTP" : "Request OTP"}
              </button>
            </form>
          </>
        ) : null}
        {devOtpEnabled ? (
          <p style={{ marginTop: 16, textAlign: "center" }}>
            <a href="/signup">Create an organization</a>
          </p>
        ) : null}
        <PublicProductLinks discloseSessionCookie className="mt-8" />
      </section>
    </main>
  );
}
