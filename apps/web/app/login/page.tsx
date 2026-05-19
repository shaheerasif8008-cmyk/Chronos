"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("admin@example.com");
  const [code, setCode] = useState("");
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState("");

  async function requestOtp(event: FormEvent) {
    event.preventDefault();
    setError("");
    const res = await fetch(`${API_BASE}/auth/request-otp`, {
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
    const res = await fetch(`${API_BASE}/auth/verify-otp`, {
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

  return (
    <main className="min-h-screen bg-[#f6f7f9] px-6 py-10">
      <section className="mx-auto flex min-h-[calc(100vh-5rem)] max-w-md flex-col justify-center">
        <h1 className="text-3xl font-semibold tracking-normal text-[#15171a]">Chronos</h1>
        <p className="mt-3 text-sm leading-6 text-[#525866]">
          Sign in with the seeded admin email. In dev, the OTP prints in the API console.
        </p>
        <form onSubmit={requested ? verifyOtp : requestOtp} className="mt-8 space-y-4">
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
          {error ? <p className="text-sm text-[#b42318]">{error}</p> : null}
          <button className="w-full rounded-md bg-[#15171a] px-4 py-2 text-sm font-medium text-white">
            {requested ? "Verify OTP" : "Request OTP"}
          </button>
        </form>
      </section>
    </main>
  );
}
