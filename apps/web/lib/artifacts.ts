import { apiFetch } from "./api";

export type Artifact = {
  id: string;
  title: string | null;
  kind: string;
  mime_type: string | null;
  size_bytes: number | null;
  version: number;
  conversation_id: string | null;
  task_id: string | null;
  created_at: string;
  updated_at?: string | null;
};

export type ArtifactVersion = {
  id: string;
  version: number;
  mime_type: string | null;
  size_bytes: number | null;
  edit_summary: string | null;
  created_by: string | null;
  created_at: string;
};

export type DiffResult = { is_binary: boolean; from_version: number; to_version: number; diff: string };

export type PreviewBlock = { type: string; style?: string; text: string };
export type PreviewSlide = {
  number: number;
  texts: string[];
  tables: string[][][];
  charts: { series: { name: string; values: string[] }[] }[];
  omitted_images: number;
};
export type PreviewSheet = { name: string; rows: string[][]; truncated: boolean };
export type PreviewCell = {
  number: number;
  cell_type: string;
  source: string;
  outputs: string[];
};
export type PreviewArchiveEntry = {
  path: string;
  size_bytes: number;
  compressed_bytes: number;
  directory: boolean;
};
export type ArtifactPreview = {
  status: "ready" | "unsupported" | "error";
  renderer: "download" | "document" | "presentation" | "workbook" | "notebook" | "archive" | "pdf" | "markup" | "image" | "markdown" | "json" | "csv" | "text" | "source";
  format: string;
  mime_type: string;
  size_bytes: number;
  limitations: string[];
  text?: string;
  html?: string;
  blocks?: PreviewBlock[];
  tables?: string[][][];
  slides?: PreviewSlide[];
  sheets?: PreviewSheet[];
  cells?: PreviewCell[];
  entries?: PreviewArchiveEntry[];
  page_count?: number;
  preview_page_count?: number;
  width?: number;
  height?: number;
  frames?: number;
  image_format?: string;
};

export async function listArtifacts(params: { conversation_id?: string; kind?: string } = {}): Promise<Artifact[]> {
  const q = new URLSearchParams();
  if (params.conversation_id) q.set("conversation_id", params.conversation_id);
  if (params.kind) q.set("kind", params.kind);
  const res = await apiFetch(`/artifacts${q.toString() ? `?${q}` : ""}`);
  return res.json();
}

export async function getArtifact(id: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}`)).json();
}

export async function getContentText(id: string): Promise<string> {
  return (await apiFetch(`/artifacts/${id}/content`)).text();
}

export async function getContentBlob(id: string): Promise<Blob> {
  return (await apiFetch(`/artifacts/${id}/content`)).blob();
}

export async function getPreview(id: string): Promise<ArtifactPreview> {
  return (await apiFetch(`/artifacts/${id}/preview`)).json();
}

export async function getPreviewPageBlob(id: string, page: number): Promise<Blob> {
  return (await apiFetch(`/artifacts/${id}/preview/pages/${page}`)).blob();
}

export async function listVersions(id: string): Promise<ArtifactVersion[]> {
  return (await apiFetch(`/artifacts/${id}/versions`)).json();
}

export async function getVersionText(id: string, version: number): Promise<string> {
  return (await apiFetch(`/artifacts/${id}/versions/${version}/content`)).text();
}

export async function getDiff(id: string, from_version: number, to_version: number): Promise<DiffResult> {
  return (await apiFetch(`/artifacts/${id}/diff?from_version=${from_version}&to_version=${to_version}`)).json();
}

export async function editArtifact(id: string, content: string, edit_summary?: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/edit`, { method: "POST", body: JSON.stringify({ content, edit_summary }) })).json();
}

export async function aiEditArtifact(id: string, instruction: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/ai-edit`, { method: "POST", body: JSON.stringify({ instruction }) })).json();
}

export async function restoreVersion(id: string, version: number): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/restore/${version}`, { method: "POST" })).json();
}

export async function renameArtifact(id: string, title: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}`, { method: "PATCH", body: JSON.stringify({ title }) })).json();
}

export async function deleteArtifact(id: string): Promise<void> {
  await apiFetch(`/artifacts/${id}`, { method: "DELETE" });
}

export type ArtifactShareStatus = {
  published: boolean;
  token?: string;
  share_path?: string;
  expires_at?: string | null;
};

export async function getShareStatus(id: string): Promise<ArtifactShareStatus> {
  return (await apiFetch(`/artifacts/${id}/share`)).json();
}

export async function publishArtifact(id: string, expiresInHours = 168): Promise<{ token: string; share_path: string; expires_at?: string | null }> {
  return (await apiFetch(`/artifacts/${id}/publish`, {
    method: "POST",
    body: JSON.stringify({ expires_in_hours: expiresInHours }),
  })).json();
}

export async function unpublishArtifact(id: string): Promise<{ revoked: boolean }> {
  return (await apiFetch(`/artifacts/${id}/unpublish`, { method: "POST" })).json();
}

export async function duplicateArtifact(id: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/duplicate`, { method: "POST" })).json();
}
