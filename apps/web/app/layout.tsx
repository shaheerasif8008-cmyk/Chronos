import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Source_Serif_4 } from "next/font/google";
import { NetworkStatus } from "../components/system/NetworkStatus";
import { publicWebBaseUrl } from "../lib/public-config";
import "./globals.css";

const geist = Geist({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  variable: "--font-geist",
  display: "swap",
});

const geistMono = Geist_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-geist-mono",
  display: "swap",
});

// Warm transitional serif for display headings — gives Chronos the
// approachable, editorial feel of Claude.ai's wordmark and titles.
const serif = Source_Serif_4({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: ["normal", "italic"],
  variable: "--font-serif",
  display: "swap",
});

export const metadata: Metadata = {
  metadataBase: new URL(publicWebBaseUrl),
  applicationName: "Chronos",
  title: { default: "Chronos", template: "%s · Chronos" },
  description: "Secure AI operations for teams",
  category: "business",
  manifest: "/manifest.webmanifest",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "oklch(0.974 0.008 84)" },
    { media: "(prefers-color-scheme: dark)", color: "oklch(0.215 0.007 70)" },
  ],
};

interface RootLayoutProps {
  children: React.ReactNode;
}

export default function RootLayout({ children }: RootLayoutProps) {
  return (
    <html lang="en" className={`${geist.variable} ${geistMono.variable} ${serif.variable}`}>
      <body><NetworkStatus />{children}</body>
    </html>
  );
}
