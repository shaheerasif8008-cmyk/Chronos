"use client";

import { useEffect, useMemo, useState } from "react";
import type { ArtifactPreview } from "../../lib/artifacts";
import Markdown from "../Markdown";

type Props = {
  kind: string;
  mimeType: string | null;
  content: string | null;
  blobUrl?: string | null;
  title?: string | null;
  preview?: ArtifactPreview | null;
  previewPageUrls?: string[];
  previewPageLoader?: (page: number) => Promise<string>;
};

function classifyRenderer(kind: string, mime: string | null): string {
  const m = (mime ?? "").toLowerCase();
  if (m.includes("svg")) return "svg";
  if (m.startsWith("image/")) return "image";
  if (m.startsWith("text/html") || kind === "html") return "html";
  if (m.includes("json") || kind === "data") return "json";
  if (m.includes("csv") || kind === "csv") return "csv";
  if (kind === "markdown" || m.includes("markdown")) return "markdown";
  if (kind === "code" || m.startsWith("text/") || kind === "react") return "code";
  return "download";
}

const SANDBOX_CSP =
  '<meta http-equiv="Content-Security-Policy" ' +
  'content="default-src \'none\'; script-src \'none\'; connect-src \'none\'; form-action \'none\'; base-uri \'none\'; style-src \'unsafe-inline\'; img-src data:; font-src data:">';
const EMPTY_PAGE_URLS: string[] = [];

function Limitations({ items }: { items: string[] }) {
  if (!items.length) return null;
  return (
    <div role="note" className="mb-4 rounded-lg border border-soft px-3 py-2.5 text-[12.5px]" style={{ background: "var(--surface)", color: "var(--text-dim)" }}>
      <div className="mb-1 font-semibold" style={{ color: "var(--text)" }}>Safe preview notes</div>
      <ul className="list-disc space-y-1 pl-5">
        {items.map((item) => <li key={item}>{item}</li>)}
      </ul>
    </div>
  );
}

function DataTable({ rows, label }: { rows: string[][]; label: string }) {
  if (!rows.length) return <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>No populated cells.</div>;
  return (
    <div className="overflow-auto rounded-lg border" style={{ borderColor: "var(--border)" }}>
      <table aria-label={label} className="w-full border-collapse text-[12px]">
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => (
                <td key={cellIndex} className="max-w-[360px] whitespace-pre-wrap break-words border-b border-r px-2 py-1.5 align-top" style={{ borderColor: "var(--border)" }}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PdfPreview({ preview, pageUrls, title, loadPage }: { preview: ArtifactPreview; pageUrls: string[]; title?: string | null; loadPage?: (page: number) => Promise<string> }) {
  const pageCount = preview.preview_page_count ?? preview.page_count ?? pageUrls.length;
  const [page, setPage] = useState(0);
  const [url, setUrl] = useState<string | null>(pageUrls[0] ?? null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setPage(0), [preview]);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    setError(null);
    if (pageUrls[page]) {
      setUrl(pageUrls[page]);
      return () => { active = false; };
    }
    if (!loadPage) {
      setUrl(null);
      return () => { active = false; };
    }
    setUrl(null);
    void loadPage(page).then((nextUrl) => {
      objectUrl = nextUrl;
      if (active) setUrl(nextUrl);
      else if (nextUrl.startsWith("blob:")) URL.revokeObjectURL(nextUrl);
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "Page could not be rendered.");
    });
    return () => {
      active = false;
      if (objectUrl?.startsWith("blob:")) URL.revokeObjectURL(objectUrl);
    };
  }, [loadPage, page, pageUrls]);

  if (pageCount < 1) return <div className="text-[13px]" style={{ color: "var(--text-dim)" }}>This PDF has no renderable pages.</div>;
  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center justify-center gap-2">
        <button className="btn btn-ghost btn-sm" disabled={page === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>Previous page</button>
        <span aria-live="polite" className="min-w-[110px] text-center text-[12px]" style={{ color: "var(--text-dim)" }}>Page {page + 1} of {preview.page_count ?? pageCount}</span>
        <button className="btn btn-ghost btn-sm" disabled={page + 1 >= pageCount} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}>Next page</button>
      </div>
      {error && <div role="alert" className="rounded-lg border p-3 text-[13px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div>}
      {!error && !url && <div role="status" className="py-8 text-center text-[13px]" style={{ color: "var(--text-dim)" }}>Rendering page image…</div>}
      {url && <img src={url} alt={`${title ?? "PDF"}, page ${page + 1}`} className="mx-auto h-auto w-full max-w-[920px] rounded-lg border border-soft bg-white shadow-sm" />}
    </div>
  );
}

function StructuredPreview({ preview, pageUrls, title, loadPage }: { preview: ArtifactPreview; pageUrls: string[]; title?: string | null; loadPage?: (page: number) => Promise<string> }) {
  if (preview.status !== "ready") {
    return (
      <div>
        <Limitations items={preview.limitations} />
        <div role="status" className="rounded-lg border border-soft p-4 text-[13px]" style={{ color: "var(--text-dim)" }}>
          {preview.status === "error" ? "Chronos could not safely render this artifact." : "No safe inline preview is available for this artifact."} Use Download to inspect the original file.
        </div>
      </div>
    );
  }

  const body = (() => {
    if (preview.renderer === "document") {
      return (
        <article className="mx-auto max-w-[760px] rounded-xl border border-soft bg-white px-5 py-6 text-slate-900 shadow-sm sm:px-8">
          {(preview.blocks ?? []).map((block, index) => {
            if (block.type === "heading") return <h2 key={index} className="mb-2 mt-5 text-lg font-semibold first:mt-0">{block.text}</h2>;
            return <p key={index} className="mb-3 whitespace-pre-wrap text-[14px] leading-6">{block.text}</p>;
          })}
          {(preview.tables ?? []).map((table, index) => <div key={index} className="my-4"><DataTable rows={table} label={`Document table ${index + 1}`} /></div>)}
        </article>
      );
    }

    if (preview.renderer === "presentation") {
      return (
        <div className="grid gap-4 xl:grid-cols-2">
          {(preview.slides ?? []).map((slide) => (
            <section key={slide.number} aria-labelledby={`preview-slide-${slide.number}`} className="min-w-0 rounded-xl border border-soft p-4" style={{ background: "var(--surface)" }}>
              <div id={`preview-slide-${slide.number}`} className="mb-3 text-[11px] font-semibold uppercase tracking-wide" style={{ color: "var(--text-dim)" }}>Slide {slide.number}</div>
              {slide.texts.map((text, index) => <p key={index} className={`${index === 0 ? "text-[16px] font-semibold" : "text-[13px]"} mb-2 whitespace-pre-wrap`}>{text}</p>)}
              {slide.tables.map((table, index) => <div key={index} className="my-3"><DataTable rows={table} label={`Slide ${slide.number} table ${index + 1}`} /></div>)}
              {slide.charts.map((chart, index) => (
                <div key={index} className="mt-3 rounded-lg border border-soft px-3 py-2 text-[12px]">
                  <div className="font-semibold">Cached chart data</div>
                  {chart.series.map((series, seriesIndex) => <div key={seriesIndex} className="mt-1 break-words">{series.name || `Series ${seriesIndex + 1}`}: {series.values.join(", ") || "No cached values"}</div>)}
                </div>
              ))}
            </section>
          ))}
        </div>
      );
    }

    if (preview.renderer === "workbook") {
      return (
        <div className="space-y-3">
          {(preview.sheets ?? []).map((sheet, index) => (
            <details key={sheet.name} open={index === 0} className="rounded-lg border border-soft p-3">
              <summary className="cursor-pointer text-[13px] font-semibold">{sheet.name}{sheet.truncated ? " · preview truncated" : ""}</summary>
              <div className="mt-3"><DataTable rows={sheet.rows} label={`${sheet.name} worksheet`} /></div>
            </details>
          ))}
        </div>
      );
    }

    if (preview.renderer === "notebook") {
      return (
        <div className="space-y-3">
          {(preview.cells ?? []).map((cell) => (
            <section key={cell.number} aria-labelledby={`notebook-cell-${cell.number}`} className="overflow-hidden rounded-lg border border-soft">
              <div id={`notebook-cell-${cell.number}`} className="border-b border-soft px-3 py-1.5 text-[11px] font-semibold uppercase tracking-wide" style={{ background: "var(--surface)", color: "var(--text-dim)" }}>{cell.cell_type} cell {cell.number}</div>
              {cell.cell_type === "markdown"
                ? <div className="prose-body max-w-none p-3"><Markdown content={cell.source} /></div>
                : <pre className="overflow-auto whitespace-pre-wrap break-words p-3 text-[12px]">{cell.source}</pre>}
              {cell.outputs.map((output, index) => <pre key={index} className="overflow-auto whitespace-pre-wrap break-words border-t border-soft p-3 text-[12px]" style={{ background: "var(--surface)" }}>{output}</pre>)}
            </section>
          ))}
        </div>
      );
    }

    if (preview.renderer === "archive") {
      const rows = (preview.entries ?? []).map((entry) => [entry.path, entry.directory ? "Folder" : `${entry.size_bytes.toLocaleString()} bytes`, entry.directory ? "—" : `${entry.compressed_bytes.toLocaleString()} bytes`]);
      return <DataTable rows={[["Path", "Original size", "Compressed size"], ...rows]} label="Archive contents" />;
    }

    if (preview.renderer === "pdf") {
      return <PdfPreview preview={preview} pageUrls={pageUrls} title={title} loadPage={loadPage} />;
    }

    if (preview.renderer === "markup" && preview.html != null) {
      const srcdoc = `<!doctype html><html><head>${SANDBOX_CSP}</head><body>${preview.html}</body></html>`;
      return <iframe sandbox="" referrerPolicy="no-referrer" srcDoc={srcdoc} className="min-h-[420px] w-full rounded-lg border" title={`${title ?? "Artifact"} safe preview`} />;
    }

    if (preview.renderer === "markdown" && preview.text != null) return <div className="prose-body max-w-none"><Markdown content={preview.text} /></div>;
    if (preview.renderer === "csv" && preview.text != null) return <CsvTable content={preview.text} />;
    if (preview.renderer === "json" && preview.text != null) {
      let pretty = preview.text;
      try { pretty = JSON.stringify(JSON.parse(preview.text), null, 2); } catch { /* show bounded source */ }
      return <pre className="overflow-auto whitespace-pre-wrap break-words rounded-lg p-3 text-[12.5px]" style={{ background: "var(--surface)" }}>{pretty}</pre>;
    }
    if ((preview.renderer === "text" || preview.renderer === "source") && preview.text != null) return <pre className="overflow-auto whitespace-pre-wrap break-words rounded-lg p-3 font-mono text-[12.5px]" style={{ background: "var(--surface)" }}>{preview.text}</pre>;
    return <div className="rounded-lg border border-soft p-4 text-[13px]" style={{ color: "var(--text-dim)" }}>Use Download to inspect this file.</div>;
  })();

  return <><Limitations items={preview.limitations} />{body}</>;
}

export function ArtifactRenderer({ kind, mimeType, content, blobUrl, title, preview, previewPageUrls = EMPTY_PAGE_URLS, previewPageLoader }: Props) {
  const renderer = useMemo(() => classifyRenderer(kind, mimeType), [kind, mimeType]);
  if (preview) return <StructuredPreview preview={preview} pageUrls={previewPageUrls} title={title} loadPage={previewPageLoader} />;

  if (renderer === "image" && blobUrl) return <img src={blobUrl} alt={title ?? "artifact"} className="max-w-full rounded-lg" />;
  if ((renderer === "svg" || renderer === "html") && content) {
    const srcdoc = `<!doctype html><html><head>${SANDBOX_CSP}</head><body style="margin:0">${content}</body></html>`;
    return <iframe sandbox="" referrerPolicy="no-referrer" srcDoc={srcdoc} className="min-h-[320px] w-full rounded-lg border" title={`${renderer} safe preview`} />;
  }
  if (renderer === "json" && content) {
    let pretty = content;
    try { pretty = JSON.stringify(JSON.parse(content), null, 2); } catch { /* show raw */ }
    return <pre className="overflow-auto rounded-lg p-3 text-[12.5px]" style={{ background: "var(--surface)" }}>{pretty}</pre>;
  }
  if (renderer === "csv" && content) return <CsvTable content={content} />;
  if (renderer === "markdown" && content) return <div className="prose-body max-w-none"><Markdown content={content} /></div>;
  if (renderer === "code" && content) return <pre className="overflow-auto whitespace-pre-wrap break-words rounded-lg p-3 font-mono text-[12.5px]" style={{ background: "var(--surface)" }}>{content}</pre>;
  return <div className="rounded-lg p-4 text-[13px]" style={{ background: "var(--surface)", color: "var(--text-dim)" }}>No safe inline preview is available{mimeType ? ` for ${mimeType}` : ""}. Use Download to open it.</div>;
}

function CsvTable({ content }: { content: string }) {
  const rows = useMemo(() => content.trim().split(/\r?\n/).slice(0, 200).map((line) => {
    const cells: string[] = [];
    let current = "";
    let quoted = false;
    for (const character of line) {
      if (character === '"') quoted = !quoted;
      else if (character === "," && !quoted) { cells.push(current); current = ""; }
      else current += character;
    }
    cells.push(current);
    return cells;
  }), [content]);
  return <DataTable rows={rows} label="CSV preview" />;
}

export { classifyRenderer };
