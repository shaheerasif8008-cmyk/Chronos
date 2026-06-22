"use client";

import { FormEvent, useState } from "react";
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

export default function OnboardingPage() {
  const router = useRouter();
  const [step, setStep] = useState<1 | 2>(1);
  const [invite, setInvite] = useState("");
  const [invited, setInvited] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function sendInvite(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/settings/invitations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: invite, role: "user" }),
        credentials: "include",
      });
      if (!res.ok) throw new Error(await res.text());
      setInvited((prev) => [...prev, invite]);
      setInvite("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invite failed");
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    setBusy(true);
    try {
      await fetch(`${apiBase()}/settings/onboarding/complete`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Non-fatal: proceed into the app regardless.
    } finally {
      router.push("/chat");
    }
  }

  return (
    <div style={{ maxWidth: 440, margin: "10vh auto", display: "flex", flexDirection: "column", gap: 16 }}>
      {step === 1 ? (
        <>
          <h1>Welcome to Chronos</h1>
          <p>Your organization is ready. Let&apos;s invite your team.</p>
          <button onClick={() => setStep(2)}>Invite teammates</button>
          <button onClick={finish} disabled={busy} style={{ background: "transparent" }}>
            Skip for now
          </button>
        </>
      ) : (
        <>
          <h1>Invite your team</h1>
          {error && <p role="alert" style={{ color: "crimson" }}>{error}</p>}
          <form onSubmit={sendInvite} style={{ display: "flex", gap: 8 }}>
            <input
              type="email"
              required
              placeholder="teammate@yourcompany.com"
              value={invite}
              onChange={(e) => setInvite(e.target.value)}
              style={{ flex: 1 }}
            />
            <button type="submit" disabled={busy}>Send</button>
          </form>
          {invited.length > 0 && (
            <ul>{invited.map((e) => <li key={e}>Invited {e}</li>)}</ul>
          )}
          <button onClick={finish} disabled={busy}>Finish setup</button>
        </>
      )}
    </div>
  );
}
