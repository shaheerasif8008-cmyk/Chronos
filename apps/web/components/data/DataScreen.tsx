"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiBase, getToken } from "../../lib/api";
// apiBase and getToken are used for the file upload helper below

// ─── Types ────────────────────────────────────────────────────────────────────

type ColumnSchema = { name: string; dtype: string };

type Dataset = {
  id: string;
  name: string;
  organization_id: string;
  source_artifact_id: string;
  schema: { columns: ColumnSchema[] } | null;
  row_count: number | null;
  status: string;
  created_at?: string;
};

type AnalysisResult = {
  dataset_id: string;
  status: string;
  artifact_ids: string[];
  stdout_preview: string;
  summary: string;
};

type ArtifactMeta = {
  id: string;
  kind: string;
  title: string | null;
  mime_type: string | null;
  minio_path: string;
};

// ─── Helpers ──────────────────────────────────────────────────────────────────

async function uploadCsvFile(file: File): Promise<string> {
  const form = new FormData();
  form.append("file", file);
  const token = getToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  // POST /attachments (no trailing slash — route is mounted at "")
  const res = await fetch(`${apiBase()}/attachments`, { method: "POST", body: form, headers });
  if (!res.ok) throw new Error(await res.text());
  const data = await res.json();
  // Response key is attachment_id (same UUID as the artifact)
  return (data.attachment_id ?? data.artifact_id) as string;
}

async function createDataset(artifactId: string, name?: string): Promise<Dataset> {
  const res = await apiFetch("/datasets/", {
    method: "POST",
    body: JSON.stringify({ source_artifact_id: artifactId, name }),
  });
  return res.json() as Promise<Dataset>;
}

async function listDatasets(): Promise<Dataset[]> {
  const res = await apiFetch("/datasets/");
  return res.json() as Promise<Dataset[]>;
}

async function runAnalysis(datasetId: string, code: string): Promise<AnalysisResult> {
  const res = await apiFetch(`/datasets/${datasetId}/analyze`, {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  return res.json() as Promise<AnalysisResult>;
}

async function fetchArtifactMeta(artifactId: string): Promise<ArtifactMeta> {
  const res = await apiFetch(`/artifacts/${artifactId}`);
  return res.json() as Promise<ArtifactMeta>;
}


const DEFAULT_CODE = `import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

df = pd.read_csv('data.csv')
print(df.describe().to_string())
print()
print(df.head(10).to_string())

# Save a chart
if df.shape[1] >= 2:
    numeric = df.select_dtypes(include='number')
    if not numeric.empty:
        col = numeric.columns[0]
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.hist(df[col].dropna(), bins=20)
        ax.set_title(f'Distribution of {col}')
        ax.set_xlabel(col)
        ax.set_ylabel('Count')
        plt.tight_layout()
        plt.savefig('chart_1.png')
        plt.close()
`;

// ─── Sub-components ───────────────────────────────────────────────────────────

function SchemaTable({ columns }: { columns: ColumnSchema[] }) {
  if (!columns.length) return <p style={{ color: "var(--text-dim)", fontSize: 13 }}>No columns detected.</p>;
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
        <thead>
          <tr style={{ borderBottom: "1px solid var(--border)" }}>
            <th style={{ textAlign: "left", padding: "4px 8px", color: "var(--text-dim)", fontWeight: 600 }}>#</th>
            <th style={{ textAlign: "left", padding: "4px 8px", color: "var(--text-dim)", fontWeight: 600 }}>Column</th>
            <th style={{ textAlign: "left", padding: "4px 8px", color: "var(--text-dim)", fontWeight: 600 }}>Type</th>
          </tr>
        </thead>
        <tbody>
          {columns.map((col, i) => (
            <tr key={col.name} style={{ borderBottom: "1px solid var(--border)" }}>
              <td style={{ padding: "4px 8px", color: "var(--text-dim)" }}>{i + 1}</td>
              <td style={{ padding: "4px 8px", fontFamily: "var(--font-geist-mono, monospace)" }}>{col.name}</td>
              <td style={{ padding: "4px 8px", color: "var(--accent)" }}>{col.dtype}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ArtifactCard({ artifactId }: { artifactId: string }) {
  const [meta, setMeta] = useState<ArtifactMeta | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [textContent, setTextContent] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let revoked = false;
    let objectUrl: string | null = null;  // captured locally so cleanup can revoke it
    async function load() {
      try {
        const m = await fetchArtifactMeta(artifactId);
        if (revoked) return;
        setMeta(m);
        const isImage = m.kind === "image" || (m.mime_type ?? "").startsWith("image/");
        if (isImage) {
          const blob = await apiFetch(`/artifacts/${artifactId}/content`).then(r => r.blob());
          if (revoked) return;
          objectUrl = URL.createObjectURL(blob);
          setBlobUrl(objectUrl);
        } else {
          const text = await apiFetch(`/artifacts/${artifactId}/content`).then(r => r.text());
          if (revoked) return;
          setTextContent(text);
        }
      } catch (e: unknown) {
        if (!revoked) setErr(String(e));
      }
    }
    void load();
    return () => {
      revoked = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifactId]);

  if (err) return <div style={{ color: "var(--danger)", fontSize: 12 }}>Failed to load artifact {artifactId.slice(0, 8)}</div>;
  if (!meta) return <div style={{ color: "var(--text-dim)", fontSize: 12 }}>Loading…</div>;

  const isImage = meta.kind === "image" || (meta.mime_type ?? "").startsWith("image/");

  return (
    <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden", background: "var(--surface)" }}>
      <div style={{ padding: "8px 12px", borderBottom: "1px solid var(--border)", display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-dim)" }}>{meta.kind}</span>
        <span style={{ fontSize: 12.5, color: "var(--text)" }}>{meta.title ?? "Untitled"}</span>
      </div>
      <div style={{ padding: 12 }}>
        {isImage && blobUrl ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={blobUrl} alt={meta.title ?? "chart"} style={{ maxWidth: "100%", borderRadius: 6 }} />
        ) : isImage ? (
          <div style={{ color: "var(--text-dim)", fontSize: 12 }}>Loading image…</div>
        ) : textContent != null ? (
          <pre style={{ fontSize: 12, overflowX: "auto", margin: 0, whiteSpace: "pre-wrap", color: "var(--text)", maxHeight: 300 }}>
            {textContent.slice(0, 2000)}{textContent.length > 2000 ? "\n…(truncated)" : ""}
          </pre>
        ) : (
          <div style={{ color: "var(--text-dim)", fontSize: 12 }}>Loading content…</div>
        )}
      </div>
    </div>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

export default function DataScreen() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);

  // Analysis state
  const [code, setCode] = useState(DEFAULT_CODE);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisResult, setAnalysisResult] = useState<AnalysisResult | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);

  const refreshList = useCallback(async () => {
    setLoadingList(true);
    try {
      const data = await listDatasets();
      setDatasets(data.sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")));
    } catch {
      // silent — list stays empty
    } finally {
      setLoadingList(false);
    }
  }, []);

  useEffect(() => { void refreshList(); }, [refreshList]);

  const selected = datasets.find(d => d.id === selectedId) ?? null;

  async function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setUploadError(null);
    try {
      const artifactId = await uploadCsvFile(file);
      const ds = await createDataset(artifactId, file.name);
      await refreshList();
      setSelectedId(ds.id);
    } catch (err: unknown) {
      setUploadError(err instanceof Error ? err.message : String(err));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function handleAnalyze() {
    if (!selectedId) return;
    setAnalyzing(true);
    setAnalysisError(null);
    setAnalysisResult(null);
    try {
      const result = await runAnalysis(selectedId, code);
      setAnalysisResult(result);
    } catch (err: unknown) {
      setAnalysisError(err instanceof Error ? err.message : String(err));
    } finally {
      setAnalyzing(false);
    }
  }

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0, fontFamily: "var(--font-geist, sans-serif)" }}>
      {/* ─── Sidebar ───────────────────────────────────────────────────────── */}
      <aside style={{ width: 280, flexShrink: 0, borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
        <div style={{ padding: "12px 14px", borderBottom: "1px solid var(--border)" }}>
          <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 10 }}>Data Workspace</div>
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            style={{ width: "100%", padding: "7px 12px", borderRadius: 8, border: "1px dashed var(--border)", background: "transparent", cursor: uploading ? "default" : "pointer", fontSize: 13, color: "var(--accent)", opacity: uploading ? 0.6 : 1 }}
          >
            {uploading ? "Uploading…" : "+ Upload CSV / XLSX / JSON"}
          </button>
          <input ref={fileRef} type="file" accept=".csv,.xlsx,.json,text/csv,application/json" style={{ display: "none" }} onChange={handleFileSelect} />
          {uploadError && <div style={{ marginTop: 6, fontSize: 12, color: "var(--danger)" }}>{uploadError}</div>}
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: "8px 8px" }}>
          {loadingList && <div style={{ padding: "12px 6px", fontSize: 13, color: "var(--text-dim)" }}>Loading…</div>}
          {!loadingList && datasets.length === 0 && (
            <div style={{ padding: "12px 6px", fontSize: 13, color: "var(--text-dim)" }}>No datasets yet. Upload a CSV to start.</div>
          )}
          {datasets.map(ds => (
            <button
              key={ds.id}
              onClick={() => { setSelectedId(ds.id); setAnalysisResult(null); setAnalysisError(null); }}
              style={{ width: "100%", textAlign: "left", padding: "8px 10px", borderRadius: 8, marginBottom: 2, background: selectedId === ds.id ? "var(--accent-soft)" : "transparent", border: "none", cursor: "pointer" }}
            >
              <div style={{ fontSize: 13.5, fontWeight: 500, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{ds.name}</div>
              <div style={{ fontSize: 11.5, color: "var(--text-dim)", marginTop: 2 }}>
                {ds.row_count != null ? `${ds.row_count} rows` : "—"}
                {ds.schema?.columns.length ? ` · ${ds.schema.columns.length} cols` : ""}
              </div>
            </button>
          ))}
        </div>
      </aside>

      {/* ─── Main panel ────────────────────────────────────────────────────── */}
      <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden" }}>
        {!selected ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-dim)", fontSize: 14 }}>
            Select or upload a dataset to begin.
          </div>
        ) : (
          <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0, overflow: "hidden" }}>
            {/* Dataset header */}
            <div style={{ padding: "14px 20px", borderBottom: "1px solid var(--border)" }}>
              <div style={{ fontSize: 16, fontWeight: 600 }}>{selected.name}</div>
              <div style={{ fontSize: 12.5, color: "var(--text-dim)", marginTop: 2 }}>
                {selected.row_count != null ? `${selected.row_count} rows` : "unknown rows"}
                {selected.schema?.columns.length ? ` · ${selected.schema.columns.length} columns` : ""}
                {" · "}
                <span style={{ color: selected.status === "ready" ? "var(--ok)" : "var(--text-dim)" }}>{selected.status}</span>
              </div>
            </div>

            <div style={{ flex: 1, display: "flex", minHeight: 0, overflow: "hidden" }}>
              {/* Left: schema + code editor */}
              <div style={{ width: "50%", borderRight: "1px solid var(--border)", display: "flex", flexDirection: "column", overflow: "hidden" }}>
                {/* Schema */}
                <div style={{ padding: "12px 16px", borderBottom: "1px solid var(--border)" }}>
                  <div style={{ fontSize: 12, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-dim)", marginBottom: 8 }}>Schema</div>
                  <SchemaTable columns={selected.schema?.columns ?? []} />
                </div>

                {/* Code editor */}
                <div style={{ flex: 1, display: "flex", flexDirection: "column", padding: "12px 16px", overflow: "hidden" }}>
                  <div style={{ fontSize: 12, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-dim)", marginBottom: 8 }}>Analysis Code</div>
                  <textarea
                    value={code}
                    onChange={e => setCode(e.target.value)}
                    spellCheck={false}
                    style={{ flex: 1, resize: "none", fontFamily: "var(--font-geist-mono, monospace)", fontSize: 12.5, lineHeight: 1.55, padding: "10px 12px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", color: "var(--text)", outline: "none", minHeight: 160 }}
                  />
                  <button
                    onClick={handleAnalyze}
                    disabled={analyzing}
                    style={{ marginTop: 10, padding: "8px 16px", borderRadius: 8, border: "none", background: "var(--accent)", color: "#fff", fontSize: 13.5, fontWeight: 500, cursor: analyzing ? "default" : "pointer", opacity: analyzing ? 0.7 : 1 }}
                  >
                    {analyzing ? "Running…" : "Run Analysis"}
                  </button>
                  {analysisError && (
                    <div style={{ marginTop: 8, padding: "8px 10px", borderRadius: 8, background: "var(--danger-soft)", color: "var(--danger)", fontSize: 12.5 }}>
                      {analysisError}
                    </div>
                  )}
                </div>
              </div>

              {/* Right: results */}
              <div style={{ flex: 1, overflow: "auto", padding: "12px 16px" }}>
                {!analysisResult && !analyzing && (
                  <div style={{ color: "var(--text-dim)", fontSize: 13.5, paddingTop: 20 }}>
                    Run analysis to see charts and tables here.
                  </div>
                )}
                {analyzing && (
                  <div style={{ color: "var(--text-dim)", fontSize: 13.5, paddingTop: 20 }}>
                    Running analysis…
                  </div>
                )}
                {analysisResult && (
                  <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
                    <div style={{ fontSize: 12, color: analysisResult.status === "success" ? "var(--ok)" : "var(--danger)" }}>
                      {analysisResult.summary}
                    </div>

                    {analysisResult.stdout_preview && (
                      <div>
                        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-dim)", marginBottom: 6 }}>Output</div>
                        <pre style={{ fontSize: 12, overflowX: "auto", padding: "10px 12px", borderRadius: 8, background: "var(--surface)", border: "1px solid var(--border)", whiteSpace: "pre-wrap", margin: 0 }}>
                          {analysisResult.stdout_preview}
                        </pre>
                      </div>
                    )}

                    {analysisResult.artifact_ids.length > 0 && (
                      <div>
                        <div style={{ fontSize: 11, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-dim)", marginBottom: 8 }}>
                          Generated Artifacts ({analysisResult.artifact_ids.length})
                        </div>
                        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                          {analysisResult.artifact_ids.map(aid => (
                            <ArtifactCard key={aid} artifactId={aid} />
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
