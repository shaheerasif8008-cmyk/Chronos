"use client";

/**
 * /login/callback
 *
 * Cognito redirects here after sign-in with ?code=<auth_code>.
 * This page sends the code to the backend, stores the returned JWT, then
 * navigates to /chat.
 *
 * On error it falls back to /login with a query-string error message so the
 * login page can surface it.
 */
import { useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

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

function CallbackHandler() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<"exchanging" | "error">("exchanging");
  const [errorMsg, setErrorMsg] = useState("");
  const ran = useRef(false);

  useEffect(() => {
    if (ran.current) return;
    ran.current = true;

    const code = searchParams.get("code");
    const errorParam = searchParams.get("error");
    const errorDesc = searchParams.get("error_description");

    if (errorParam) {
      const msg = errorDesc ?? errorParam;
      setErrorMsg(msg);
      setStatus("error");
      return;
    }

    if (!code) {
      setErrorMsg("No authorization code in the callback URL.");
      setStatus("error");
      return;
    }

    (async () => {
      try {
        const res = await fetch(`${apiBase()}/auth/cognito/callback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code }),
        });

        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail ?? `Server error ${res.status}`);
        }

        const data = await res.json();
        localStorage.setItem("chronos_token", data.access_token);
        router.replace("/chat");
      } catch (err: unknown) {
        const msg = err instanceof Error ? err.message : String(err);
        setErrorMsg(msg);
        setStatus("error");
      }
    })();
  }, [searchParams, router]);

  if (status === "error") {
    return (
      <main className="min-h-screen bg-[#f6f7f9] px-6 py-10">
        <section className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-md flex-col justify-center gap-4">
          <h1 className="text-xl font-semibold text-[#15171a]">Sign-in failed</h1>
          <p className="rounded-md bg-[#fef2f2] px-3 py-2 text-sm text-[#b42318]">{errorMsg}</p>
          <button
            onClick={() => router.replace("/login")}
            className="self-start rounded-md bg-[#15171a] px-4 py-2 text-sm font-medium text-white"
          >
            Back to login
          </button>
        </section>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-[#f6f7f9]">
      <section className="flex min-h-screen flex-col items-center justify-center gap-3">
        <span className="h-6 w-6 animate-spin rounded-full border-2 border-[#15171a] border-t-transparent" />
        <p className="text-sm text-[#525866]">Signing you in…</p>
      </section>
    </main>
  );
}

export default function CallbackPage() {
  return (
    <Suspense fallback={
      <main className="min-h-screen bg-[#f6f7f9]">
        <section className="flex min-h-screen items-center justify-center">
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-[#15171a] border-t-transparent" />
        </section>
      </main>
    }>
      <CallbackHandler />
    </Suspense>
  );
}
