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
  const [email, setEmail] = useState("admin@example.com");
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
      body: JSON.stringify({ email, code }),
    });
    if (!res.ok) {
      setError(await res.text());
      return;
    }
    const data = await res.json();
    localStorage.setItem("chronos_token", data.access_token);
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

  const cognitoEnabled = authConfig?.cognito?.enabled;
  const devOtpEnabled = authConfig?.devOtp ?? false;

  return (
    <main className="min-h-screen bg-[#f6f7f9] px-6 py-10">
      <section className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-md flex-col justify-center">
        <h1 className="text-3xl font-semibold tracking-normal text-[#15171a]">Chronos</h1>
        <p className="mt-3 text-sm leading-6 text-[#525866]">
          {cognitoEnabled
            ? "Sign in with your organization account (Amazon Cognito)."
            : "Sign in with the seeded admin email. In dev, the OTP prints in the API console."}
        </p>

        {!authConfig && (
          <div className="mt-12 flex justify-center">
            <div className="h-6 w-6 animate-spin rounded-full border-2 border-[#c9ced6] border-t-[#15171a]" />
          </div>
        )}

        {cognitoEnabled ? (
          <div className="mt-8 space-y-4">
            <button
              type="button"
              onClick={signInWithCognito}
              className="w-full rounded-md bg-[#15171a] px-4 py-2.5 text-sm font-medium text-white"
            >
              Sign in with Cognito
            </button>
            {error ? <p className="text-sm text-[#b42318]">{error}</p> : null}
          </div>
        ) : null}

        {devOtpEnabled ? (
          <>
            {cognitoEnabled ? (
              <div className="my-6 flex items-center gap-3">
                <div className="h-px flex-1 bg-[#c9ced6]" />
                <span className="text-xs text-[#525866]">or dev OTP</span>
                <div className="h-px flex-1 bg-[#c9ced6]" />
              </div>
            ) : null}
            <form onSubmit={requested ? verifyOtp : requestOtp} className="space-y-4">
              <label className="block">
                <span className="text-sm font-medium text-[#2d333b]">Email</span>
                <input
                  className="mt-2 w-full rounded-md border border-[#c9ced6] bg-white px-3 py-2 outline-none focus:border-[#1f6feb]"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  type="email"
                />
              </label>
              {requested ? (
                <label className="block">
                  <span className="text-sm font-medium text-[#2d333b]">OTP</span>
                  <input
                    className="mt-2 w-full rounded-md border border-[#c9ced6] bg-white px-3 py-2 outline-none focus:border-[#1f6feb]"
                    value={code}
                    onChange={(event) => setCode(event.target.value)}
                    inputMode="numeric"
                  />
                </label>
              ) : null}
              {error && !cognitoEnabled ? <p className="text-sm text-[#b42318]">{error}</p> : null}
              <button className="w-full rounded-md border border-[#c9ced6] bg-white px-4 py-2 text-sm font-medium text-[#15171a]">
                {requested ? "Verify OTP" : "Request OTP"}
              </button>
            </form>
          </>
        ) : null}
      </section>
    </main>
  );
}
