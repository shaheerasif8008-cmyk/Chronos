"use client";
import { useEffect, useState } from "react";
import { ArtifactDetail } from "./ArtifactsScreen";

export default function InChatArtifactPanel() {
  const [artifactId, setArtifactId] = useState<string | null>(null);

  useEffect(() => {
    function onOpen(e: Event) {
      const id = (e as CustomEvent<{ id: string }>).detail?.id;
      if (id) setArtifactId(id);
    }
    window.addEventListener("chronos:open-artifact", onOpen as EventListener);
    return () => window.removeEventListener("chronos:open-artifact", onOpen as EventListener);
  }, []);

  if (!artifactId) return null;

  return (
    <>
      <div onClick={() => setArtifactId(null)}
           className="fixed inset-0 z-40" style={{ background: "rgba(0,0,0,0.25)" }} />
      <div className="fixed top-0 right-0 h-full z-50 flex flex-col shadow-xl"
           style={{ width: "min(640px, 92vw)", background: "var(--bg)", borderLeft: "1px solid var(--border)" }}>
        <div className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: "var(--border)" }}>
          <div className="text-[13px] font-semibold">Artifact</div>
          <button onClick={() => setArtifactId(null)} className="btn btn-ghost btn-sm">Close</button>
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <ArtifactDetail
            key={artifactId}
            artifactId={artifactId}
            onChanged={() => { /* panel content reloads itself */ }}
            onDeleted={() => setArtifactId(null)}
          />
        </div>
      </div>
    </>
  );
}
