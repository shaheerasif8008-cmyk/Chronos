import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Chronos",
  description: "Chronos local development console",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
