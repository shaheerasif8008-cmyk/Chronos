"use client";
import { useEffect, useRef, useState } from "react";
import { ArtifactDetail } from "./ArtifactsScreen";
import type { CollaborationIdentity } from "../../lib/collaboration";

export default function InChatArtifactPanel({ currentMember }: { currentMember: CollaborationIdentity }) {
  const [artifactId, setArtifactId] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    function onOpen(e: Event) {
      const id = (e as CustomEvent<{ id: string }>).detail?.id;
      if (id) {
        previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setArtifactId(id);
      }
    }
    window.addEventListener("chronos:open-artifact", onOpen as EventListener);
    return () => window.removeEventListener("chronos:open-artifact", onOpen as EventListener);
  }, []);

  useEffect(() => {
    if (!artifactId) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setArtifactId(null);
      if (event.key === "Tab" && panelRef.current) {
        const focusable = [...panelRef.current.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
          .filter(element => element.offsetParent !== null);
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [artifactId]);

  useEffect(() => {
    if (!artifactId) previousFocusRef.current?.focus();
  }, [artifactId]);

  if (!artifactId) return null;

  return (
    <>
      <div aria-hidden="true" onClick={() => setArtifactId(null)}
           className="fixed inset-0 z-40" style={{ background: "rgba(0,0,0,0.25)" }} />
      <div ref={panelRef} className="fixed right-0 top-0 z-50 flex h-full w-full max-w-[640px] flex-col shadow-xl sm:w-[92vw]"
           style={{ background: "var(--bg)", borderLeft: "1px solid var(--border)" }} role="dialog" aria-modal="true" aria-labelledby="in-chat-artifact-heading">
        <div className="flex items-center justify-between px-4 py-2 border-b" style={{ borderColor: "var(--border)" }}>
          <h2 id="in-chat-artifact-heading" className="text-[13px] font-semibold">Artifact</h2>
          <button autoFocus onClick={() => setArtifactId(null)} className="btn btn-ghost btn-sm">Close</button>
        </div>
        <div className="flex-1 min-h-0 overflow-hidden">
          <ArtifactDetail
            key={artifactId}
            artifactId={artifactId}
            memberRole={currentMember.role}
            currentMember={currentMember}
            onChanged={() => { /* panel content reloads itself */ }}
            onDeleted={() => setArtifactId(null)}
          />
        </div>
      </div>
    </>
  );
}
