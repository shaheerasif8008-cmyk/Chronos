import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Shared artifact",
  description: "A private artifact shared through a time-limited Chronos link.",
  robots: { index: false, follow: false, nocache: true },
  referrer: "no-referrer",
};

export default function SharedArtifactLayout({ children }: { children: React.ReactNode }) {
  return children;
}
