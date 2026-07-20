function publicHttpsUrl(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

export const publicProductLinks = {
  terms: publicHttpsUrl(process.env.NEXT_PUBLIC_TERMS_URL),
  privacy: publicHttpsUrl(process.env.NEXT_PUBLIC_PRIVACY_URL),
  support: publicHttpsUrl(process.env.NEXT_PUBLIC_SUPPORT_URL),
  status: publicHttpsUrl(process.env.NEXT_PUBLIC_STATUS_URL),
} as const;

export const publicWebBaseUrl =
  publicHttpsUrl(process.env.NEXT_PUBLIC_WEB_BASE_URL) ?? "http://localhost:3000";
