/**
 * Shared API utilities for client components.
 */

const CONFIGURED_API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL;

/**
 * Return the base URL for the Chronos API.
 *
 * Resolution order:
 *  1. `NEXT_PUBLIC_API_BASE_URL` env var (set at build time for production).
 *  2. Port-mapped localhost: web port 3000 → API port 8000, 3001 → 8001, etc.
 *  3. `http://localhost:8000` as the final fallback.
 */
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
