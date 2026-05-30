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

export async function getShareStatus(id: string): Promise<{ published: boolean; token?: string; share_path?: string }> {
  return (await apiFetch(`/artifacts/${id}/share`)).json();
}

export async function publishArtifact(id: string): Promise<{ token: string; share_path: string }> {
  return (await apiFetch(`/artifacts/${id}/publish`, { method: "POST" })).json();
}

export async function unpublishArtifact(id: string): Promise<{ revoked: boolean }> {
  return (await apiFetch(`/artifacts/${id}/unpublish`, { method: "POST" })).json();
}

export async function duplicateArtifact(id: string): Promise<Artifact> {
  return (await apiFetch(`/artifacts/${id}/duplicate`, { method: "POST" })).json();
}
