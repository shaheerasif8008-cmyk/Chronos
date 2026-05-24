"use client";

/**
 * /login/callback
 *
 * Cognito redirects here after sign-in with ?code=<auth_code>&state=<state>.
 * The state value is read from sessionStorage (stored by the login page) and
 * forwarded to the backend for CSRF verification before the code is exchanged.
 */
import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { apiBase } from "@/lib/api";

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
    const stateFromUrl = searchParams.get("state");
    const errorParam = searchParams.get("error");
    const errorDesc = searchParams.get("error_description");

    if (errorParam) {
      setErrorMsg(errorDesc ?? errorParam);
      setStatus("error");
      return;
    }

    if (!code) {
      setErrorMsg("No authorization code in the callback URL.");
      setStatus("error");
      return;
    }

    // Retrieve the state we stored before the Cognito redirect.
    const storedState = sessionStorage.getItem("cognito_oauth_state");
    sessionStorage.removeItem("cognito_oauth_state");

    if (!storedState) {
      setErrorMsg("OAuth state missing from session — please try signing in again.");
      setStatus("error");
      return;
    }

    // Client-side check: state echoed by Cognito must match what we sent.
    if (stateFromUrl && stateFromUrl !== storedState) {
      setErrorMsg("OAuth state mismatch — possible CSRF attempt.");
      setStatus("error");
      return;
    }

    (async () => {
      try {
        const res = await fetch(`${apiBase()}/auth/cognito/callback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          // Forward state to the backend for server-side HMAC verification.
          body: JSON.stringify({ code, state: storedState }),
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
    <Suspense
      fallback={
        <main className="min-h-screen bg-[#f6f7f9]">
          <section className="flex min-h-screen items-center justify-center">
            <span className="h-6 w-6 animate-spin rounded-full border-2 border-[#15171a] border-t-transparent" />
          </section>
        </main>
      }
    >
      <CallbackHandler />
    </Suspense>
  );
}
