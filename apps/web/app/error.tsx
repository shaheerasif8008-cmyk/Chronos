"use client";

import { useEffect } from "react";
import * as Sentry from "@sentry/nextjs";
import { publicProductLinks } from "../lib/public-config";

export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <main className="flex min-h-[100dvh] items-center justify-center px-5" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <section className="surface w-full max-w-lg rounded-2xl border border-soft p-6 text-center sm:p-8" role="alert">
        <div className="mx-auto flex h-10 w-10 items-center justify-center rounded-xl text-lg font-semibold" style={{ background: "var(--accent)", color: "white", fontFamily: "var(--font-serif), serif" }}>C</div>
        <h1 className="h-section mt-5">This page hit an unexpected problem</h1>
        <p className="mt-2 text-[13.5px] leading-6" style={{ color: "var(--text-muted)" }}>Your saved work is still available. Retry the page; if the problem continues, give support the incident reference below.</p>
        {error.digest && <code className="mt-3 inline-block rounded-md px-2 py-1 text-[11.5px]" style={{ background: "var(--surface-2)", color: "var(--text-dim)" }}>Reference {error.digest}</code>}
        <div className="mt-6 flex flex-col justify-center gap-2 sm:flex-row">
          <button type="button" className="btn btn-accent justify-center" onClick={reset}>Try again</button>
          <a className="btn btn-secondary justify-center" href="/chat">Return to Chronos</a>
          {publicProductLinks.support ? <a className="btn btn-secondary justify-center" href={publicProductLinks.support} target="_blank" rel="noreferrer">Contact support</a> : null}
        </div>
      </section>
    </main>
  );
}
