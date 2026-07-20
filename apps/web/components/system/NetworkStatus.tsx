"use client";

import { useEffect, useState } from "react";

type NetworkState = "online" | "offline" | "restored";

export function NetworkStatus() {
  const [state, setState] = useState<NetworkState>("online");

  useEffect(() => {
    let restoredTimer: ReturnType<typeof setTimeout> | null = null;
    const clearRestoredTimer = () => {
      if (restoredTimer) clearTimeout(restoredTimer);
      restoredTimer = null;
    };
    const offline = () => {
      clearRestoredTimer();
      setState("offline");
    };
    const online = () => {
      clearRestoredTimer();
      setState(previous => previous === "offline" ? "restored" : "online");
      restoredTimer = setTimeout(() => setState("online"), 4_000);
    };

    if (!navigator.onLine) offline();
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    return () => {
      clearRestoredTimer();
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
    };
  }, []);

  if (state === "online") return null;
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-x-3 top-3 z-[200] mx-auto max-w-xl rounded-xl border px-4 py-3 text-center text-[13px] shadow-lg"
      style={{
        borderColor: state === "offline" ? "var(--warn)" : "var(--ok)",
        background: "var(--surface)",
        color: "var(--text)",
      }}
    >
      {state === "offline"
        ? "You’re offline. Your open work stays visible, but changes cannot be sent until the connection returns."
        : "Connection restored. Chronos is ready to continue."}
    </div>
  );
}
