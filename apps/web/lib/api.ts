const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;

export function apiBase(): string {
  if (CONFIGURED_API_BASE) return CONFIGURED_API_BASE;
  if (typeof window !== "undefined") {
    const webPort = Number(window.location.port || "3000");
    if (Number.isFinite(webPort) && webPort >= 3000 && webPort < 3100) {
      return `http://${window.location.hostname}:${8000 + (webPort - 3000)}`;
    }
  }
  return "http://localhost:8000";
}

export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (typeof init.body === "string" && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${apiBase()}${path}`, { ...init, headers, credentials: "include" });
  if (res.status === 401 && typeof window !== "undefined") {
    window.location.href = "/login";
  }
  if (!res.ok) {
    const fallback = `${res.status} ${res.statusText || "Request failed"}`.trim();
    let message = fallback;
    try {
      const raw = await res.text();
      if (raw) {
        try {
          const parsed = JSON.parse(raw) as { detail?: unknown; message?: unknown };
          if (typeof parsed.detail === "string" && parsed.detail.trim()) message = parsed.detail;
          else if (
            parsed.detail
            && typeof parsed.detail === "object"
            && "message" in parsed.detail
            && typeof parsed.detail.message === "string"
            && parsed.detail.message.trim()
          ) message = parsed.detail.message;
          else if (typeof parsed.message === "string" && parsed.message.trim()) message = parsed.message;
          else message = raw;
        } catch {
          message = raw;
        }
      }
    } catch {
      // Keep the status-based fallback when the response body cannot be read.
    }
    throw new Error(message);
  }
  return res;
}
