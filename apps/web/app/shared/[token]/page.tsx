"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ArtifactRenderer } from "../../../components/artifacts/ArtifactRenderer";
import { apiBase } from "../../../lib/api";
import type { Artifact, ArtifactPreview } from "../../../lib/artifacts";
import { PublicProductLinks } from "../../../components/system/PublicProductLinks";

type SharedArtifact = Artifact & { expires_at?: string | null };

async function publicFetch(path: string): Promise<Response> {
  const response = await fetch(`${apiBase()}${path}`, { cache: "no-store" });
  if (!response.ok) {
    let message = "This shared artifact is unavailable or the link was revoked.";
    try {
      const body = await response.json() as { detail?: string };
      if (body.detail) message = body.detail;
    } catch { /* retain the safe fallback */ }
    throw new Error(message);
  }
  return response;
}

export default function SharedArtifactPage() {
  const params = useParams<{ token: string }>();
  const token = params.token;
  const [artifact, setArtifact] = useState<SharedArtifact | null>(null);
  const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let imageUrl: string | null = null;
    async function load() {
      try {
        const [metaResponse, previewResponse] = await Promise.all([
          publicFetch(`/shared/${encodeURIComponent(token)}`),
          publicFetch(`/shared/${encodeURIComponent(token)}/preview`),
        ]);
        const [meta, safePreview] = await Promise.all([
          metaResponse.json() as Promise<SharedArtifact>,
          previewResponse.json() as Promise<ArtifactPreview>,
        ]);
        if (safePreview.renderer === "image") {
          const image = await publicFetch(`/shared/${encodeURIComponent(token)}/content`);
          imageUrl = URL.createObjectURL(await image.blob());
        }
        if (!active) return;
        setArtifact(meta);
        setPreview(safePreview);
        setBlobUrl(imageUrl);
      } catch (reason) {
        if (active) setError(reason instanceof Error ? reason.message : "Shared artifact could not be loaded.");
      }
    }
    void load();
    return () => {
      active = false;
      if (imageUrl) URL.revokeObjectURL(imageUrl);
    };
  }, [token]);

  const loadPdfPage = useCallback(async (page: number) => {
    const response = await publicFetch(`/shared/${encodeURIComponent(token)}/preview/pages/${page}`);
    return URL.createObjectURL(await response.blob());
  }, [token]);

  async function download() {
    if (!artifact) return;
    const response = await publicFetch(`/shared/${encodeURIComponent(token)}/content`);
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = artifact.title?.trim() || "chronos-artifact";
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 5_000);
  }

  return (
    <main className="h-[100dvh] overflow-y-auto px-4 py-6 sm:px-8" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <div className="mx-auto max-w-6xl overflow-hidden rounded-xl border" style={{ borderColor: "var(--border)", background: "var(--panel)" }}>
        <header className="flex flex-wrap items-center gap-3 border-b px-4 py-4 sm:px-6" style={{ borderColor: "var(--border)" }}>
          <div className="min-w-0 flex-1">
            <div className="text-[11px] font-semibold uppercase tracking-wide" style={{ color: "var(--text-dim)" }}>Shared from Chronos</div>
            <h1 className="truncate text-[18px] font-semibold">{artifact?.title ?? (error ? "Artifact unavailable" : "Loading artifact…")}</h1>
            {artifact && <p className="text-[12px]" style={{ color: "var(--text-dim)" }}>{artifact.kind}{artifact.mime_type ? ` · ${artifact.mime_type}` : ""} · version {artifact.version}</p>}
          </div>
          {artifact && <button className="btn btn-primary btn-sm" onClick={() => void download()}>Download original</button>}
        </header>
        <div className="min-h-[360px] p-4 sm:p-6">
          {artifact ? (
            <div className="mb-4 rounded-lg border px-3 py-2 text-[12px] leading-5" style={{ borderColor: "var(--border)", background: "var(--surface-2)", color: "var(--text-muted)" }}>
              Anyone with this link can view and download this artifact until {artifact.expires_at ? new Date(artifact.expires_at).toLocaleString() : "the owner revokes it"}. Do not forward the link unless the owner intended to share it.
            </div>
          ) : null}
          {error && <div role="alert" className="rounded-lg border p-4 text-[13px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div>}
          {!error && (!artifact || !preview) && <div role="status" className="py-12 text-center text-[13px]" style={{ color: "var(--text-dim)" }}>Preparing safe preview…</div>}
          {artifact && preview && <ArtifactRenderer kind={artifact.kind} mimeType={artifact.mime_type} content={preview.text ?? null} blobUrl={blobUrl} title={artifact.title} preview={preview} previewPageLoader={preview.renderer === "pdf" ? loadPdfPage : undefined} />}
        </div>
      </div>
      <PublicProductLinks className="mx-auto mt-5 max-w-6xl pb-4" />
    </main>
  );
}
