"use client";
import { useMemo } from "react";

type Props = {
  kind: string;
  mimeType: string | null;
  content: string | null;   // text content (null for binary)
  blobUrl?: string | null;  // for images/binary preview
  title?: string | null;
};

function classifyRenderer(kind: string, mime: string | null): string {
  const m = (mime ?? "").toLowerCase();
  if (m.startsWith("image/")) return "image";
  if (m.includes("svg")) return "svg";
  if (m.startsWith("text/html") || kind === "html") return "html";
  if (m.includes("json") || kind === "data") return "json";
  if (m.includes("csv") || kind === "csv") return "csv";
  if (kind === "markdown" || m.includes("markdown")) return "markdown";
  if (kind === "code" || m.startsWith("text/")) return "code";
  if (kind === "react") return "code";
  return "download";
}

const SANDBOX_CSP =
  '<meta http-equiv="Content-Security-Policy" ' +
  "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:;\">";

export function ArtifactRenderer({ kind, mimeType, content, blobUrl, title }: Props) {
  const renderer = useMemo(() => classifyRenderer(kind, mimeType), [kind, mimeType]);

  if (renderer === "image" && blobUrl) {
    return <img src={blobUrl} alt={title ?? "artifact"} className="max-w-full rounded-lg" />;
  }
  if (renderer === "svg" && content) {
    const srcdoc = `<!doctype html><html><head>${SANDBOX_CSP}</head><body style="margin:0">${content}</body></html>`;
    return <iframe sandbox="" srcDoc={srcdoc} className="w-full min-h-[320px] rounded-lg border" title="svg" />;
  }
  if (renderer === "html" && content) {
    // Never honor an artifact-supplied CSP — strip any incoming policy meta tags
    // and always wrap the body in our own fixed sandbox shell.
    const sanitized = content.replace(
      /<meta[^>]+http-equiv=["']?content-security-policy["']?[^>]*>/gi,
      ""
    );
    const csp = SANDBOX_CSP.replace("default-src 'none'", "default-src 'none'; script-src 'unsafe-inline'");
    const srcdoc = `<!doctype html><html><head>${csp}</head><body>${sanitized}</body></html>`;
    return <iframe sandbox="allow-scripts" srcDoc={srcdoc} className="w-full min-h-[320px] rounded-lg border" title="html" />;
  }
  if (renderer === "json" && content) {
    let pretty = content;
    try { pretty = JSON.stringify(JSON.parse(content), null, 2); } catch { /* show raw */ }
    return <pre className="text-[12.5px] overflow-auto p-3 rounded-lg" style={{ background: "var(--surface)" }}>{pretty}</pre>;
  }
  if (renderer === "csv" && content) {
    return <CsvTable content={content} />;
  }
  if (renderer === "markdown" && content) {
    return <pre className="whitespace-pre-wrap text-[13.5px] leading-relaxed p-1">{content}</pre>;
  }
  if (renderer === "code" && content) {
    return <pre className="text-[12.5px] overflow-auto p-3 rounded-lg font-mono" style={{ background: "var(--surface)" }}>{content}</pre>;
  }
  return (
    <div className="text-[13px] p-4 rounded-lg" style={{ background: "var(--surface)", color: "var(--text-dim)" }}>
      No inline preview for this type{mimeType ? ` (${mimeType})` : ""}. Use Download to open it.
    </div>
  );
}

function CsvTable({ content }: { content: string }) {
  const rows = useMemo(() => {
    return content.trim().split(/\r?\n/).slice(0, 200).map((line) => {
      const cells: string[] = [];
      let cur = "", inQ = false;
      for (const ch of line) {
        if (ch === '"') inQ = !inQ;
        else if (ch === "," && !inQ) { cells.push(cur); cur = ""; }
        else cur += ch;
      }
      cells.push(cur);
      return cells;
    });
  }, [content]);
  if (!rows.length) return null;
  const [head, ...body] = rows;
  return (
    <div className="overflow-auto rounded-lg border" style={{ borderColor: "var(--border)" }}>
      <table className="text-[12.5px] w-full border-collapse">
        <thead>
          <tr>{head.map((h, i) => <th key={i} className="text-left px-2 py-1.5 font-semibold border-b" style={{ borderColor: "var(--border)", background: "var(--surface)" }}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {body.map((r, ri) => <tr key={ri}>{r.map((c, ci) => <td key={ci} className="px-2 py-1 border-b" style={{ borderColor: "var(--border)" }}>{c}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  );
}

export { classifyRenderer };
