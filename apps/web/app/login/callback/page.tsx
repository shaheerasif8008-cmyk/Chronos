"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";

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
      router.replace(redirect.startsWith("/") ? redirect : "/chat");
      return;
    }

    const code = params.get("code");
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
    const controller = new AbortController();

    (async () => {
      try {
        const res = await fetch(`${apiBase()}/auth/cognito/callback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ code, redirect_uri: redirectUri }),
          signal: controller.signal,
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          setError(body.detail ?? `Sign-in failed (${res.status})`);
          return;
        }
        await res.json();
        router.replace("/chat");
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          setError("Could not complete sign-in. Please try again.");
        }
      }
    })();

    return () => controller.abort();
  }, [params, router]);

  return (
    <main className="min-h-screen bg-[#f6f7f9] px-6 py-10">
      <section className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-md flex-col justify-center">
        <h1 className="text-3xl font-semibold tracking-normal text-[#15171a]">Signing in…</h1>
        {error ? (
          <p className="mt-4 text-sm text-[#b42318]">{error}</p>
        ) : (
          <p className="mt-3 text-sm leading-6 text-[#525866]">Completing Cognito sign-in.</p>
        )}
      </section>
    </main>
  );
}

export default function CognitoCallbackPage() {
  return (
    <Suspense fallback={
      <main className="min-h-screen bg-[#f6f7f9] px-6 py-10">
        <section className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-md flex-col justify-center">
          <h1 className="text-3xl font-semibold tracking-normal text-[#15171a]">Signing in…</h1>
        </section>
      </main>
    }>
      <CognitoCallbackInner />
    </Suspense>
  );
}
