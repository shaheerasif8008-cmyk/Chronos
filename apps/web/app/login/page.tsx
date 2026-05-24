"use client";

import { useEffect, useState } from "react";
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

export default function LoginPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // If the user already has a valid token, skip straight to chat.
  useEffect(() => {
    if (localStorage.getItem("chronos_token")) router.replace("/chat");
  }, [router]);

  async function signInWithCognito() {
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${apiBase()}/auth/cognito/authorize`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail ?? "Failed to reach the auth service.");
        return;
      }
      const { authorize_url } = await res.json();
      // Full-page redirect to Cognito Hosted UI.
      window.location.href = authorize_url;
    } catch {
      setError("Could not connect to the API. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="min-h-screen bg-[#f6f7f9] px-6 py-10">
      <section className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-md flex-col justify-center">
        {/* Logo / wordmark */}
        <div className="mb-8 flex items-center gap-2">
          <span className="text-3xl font-semibold tracking-tight text-[#15171a]">Chronos</span>
          <span className="rounded-full bg-[#eef2ff] px-2 py-0.5 text-xs font-medium text-[#4f46e5]">
            by Cognisia
          </span>
        </div>

        <h1 className="text-xl font-semibold text-[#15171a]">Sign in to your workspace</h1>
        <p className="mt-2 text-sm leading-6 text-[#525866]">
          You&apos;ll be redirected to your organization&apos;s sign-in page.
        </p>

        {error && (
          <p className="mt-4 rounded-md bg-[#fef2f2] px-3 py-2 text-sm text-[#b42318]">
            {error}
          </p>
        )}

        <button
          onClick={signInWithCognito}
          disabled={loading}
          className="mt-6 flex w-full items-center justify-center gap-2 rounded-md bg-[#15171a] px-4 py-2.5 text-sm font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
        >
          {loading ? (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
          ) : (
            <LockIcon />
          )}
          {loading ? "Redirecting…" : "Continue with SSO"}
        </button>

        <p className="mt-6 text-center text-xs text-[#8a94a6]">
          Secured by Amazon Cognito · Chronos v1
        </p>
      </section>
    </main>
  );
}

function LockIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
      <path
        d="M11.5 7H4.5C3.67 7 3 7.67 3 8.5V13.5C3 14.33 3.67 15 4.5 15H11.5C12.33 15 13 14.33 13 13.5V8.5C13 7.67 12.33 7 11.5 7Z"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M5.5 7V4.5C5.5 3.12 6.62 2 8 2C9.38 2 10.5 3.12 10.5 4.5V7"
        stroke="currentColor"
        strokeWidth="1.25"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="8" cy="11" r="1" fill="currentColor" />
    </svg>
  );
}
