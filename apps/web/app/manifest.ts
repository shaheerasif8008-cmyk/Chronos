import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Chronos",
    short_name: "Chronos",
    description: "Secure AI operations for teams",
    start_url: "/chat",
    display: "standalone",
    background_color: "#f7f5ef",
    theme_color: "#b86d4c",
  };
}
