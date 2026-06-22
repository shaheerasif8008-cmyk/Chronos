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

export default function SignupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [orgName, setOrgName] = useState("");
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function requestOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/auth/request-otp`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
        credentials: "include",
      });
      if (!res.ok) throw new Error("Could not send a verification code. Is signup enabled?");
      setRequested(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  async function submitSignup(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch(`${apiBase()}/auth/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, code, org_name: orgName || undefined }),
        credentials: "include",
      });
      if (!res.ok) throw new Error(await res.text());
      const body = await res.json();
      // Prod: land on the org's subdomain. Dev single-host: go straight to the app.
      if (typeof window !== "undefined") {
        const host = window.location.host;
        const isLocal = host.includes("localhost") || host.startsWith("127.");
        if (!isLocal && body.subdomain) {
          const baseDomain = host.split(".").slice(-2).join(".");
          window.location.href = `${window.location.protocol}//${body.subdomain}.${baseDomain}/chat`;
          return;
        }
      }
      router.push("/chat");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Signup failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ maxWidth: 380, margin: "10vh auto", display: "flex", flexDirection: "column", gap: 12 }}>
      <h1>Create your organization</h1>
      {error && <p role="alert" style={{ color: "crimson" }}>{error}</p>}
      {!requested ? (
        <form onSubmit={requestOtp} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <input
            type="email"
            required
            placeholder="Work email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <button type="submit" disabled={busy}>Send verification code</button>
        </form>
      ) : (
        <form onSubmit={submitSignup} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <input
            required
            placeholder="Verification code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
          <input
            placeholder="Organization name (optional)"
            value={orgName}
            onChange={(e) => setOrgName(e.target.value)}
          />
          <button type="submit" disabled={busy}>Create organization</button>
        </form>
      )}
      <a href="/login">Already have an account? Sign in</a>
    </div>
  );
}
