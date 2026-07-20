"use client";

import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";
import { publicProductLinks } from "../lib/public-config";

export default function GlobalError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <html lang="en">
      <body style={{ margin: 0, background: "#f7f5ef", color: "#292720", fontFamily: "ui-sans-serif, system-ui, sans-serif" }}>
        <main style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}>
          <section role="alert" style={{ width: "min(100%, 520px)", border: "1px solid #ded9cc", borderRadius: 16, background: "white", padding: 32, textAlign: "center" }}>
            <h1 style={{ margin: 0, fontSize: 22 }}>Chronos hit an unexpected problem</h1>
            <p style={{ margin: "12px 0 0", lineHeight: 1.6, color: "#625e54" }}>Your saved work is still available. Retry the page; if the problem continues, share the incident reference with support.</p>
            {error.digest ? <code style={{ display: "inline-block", marginTop: 12, fontSize: 12 }}>Reference {error.digest}</code> : null}
            <div style={{ marginTop: 24, display: "flex", justifyContent: "center", gap: 10, flexWrap: "wrap" }}>
              <button type="button" onClick={reset} style={{ border: 0, borderRadius: 8, padding: "10px 16px", background: "#6754d9", color: "white", fontWeight: 600, cursor: "pointer" }}>Try again</button>
              <a href="/chat" style={{ border: "1px solid #d7d1c4", borderRadius: 8, padding: "9px 16px", color: "inherit", textDecoration: "none", fontWeight: 600 }}>Return to Chronos</a>
              {publicProductLinks.support ? <a href={publicProductLinks.support} target="_blank" rel="noreferrer" style={{ border: "1px solid #d7d1c4", borderRadius: 8, padding: "9px 16px", color: "inherit", textDecoration: "none", fontWeight: 600 }}>Contact support</a> : null}
            </div>
          </section>
        </main>
      </body>
    </html>
  );
}
