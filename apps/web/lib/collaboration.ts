import { apiFetch } from "./api";

export type CollaborationIdentity = {
  id: string;
  email: string;
  name?: string | null;
  role: string;
};

export type DirectoryMember = {
  id: string;
  name: string;
  email: string;
  role: string;
};

export async function loadMemberDirectory(): Promise<DirectoryMember[]> {
  const response = await apiFetch("/settings/member-directory");
  const payload = await response.json() as { members?: unknown } | unknown[];
  const rows = Array.isArray(payload) ? payload : payload.members;
  if (!Array.isArray(rows)) throw new Error("The teammate directory returned an invalid response.");
  return rows.filter((row): row is DirectoryMember => {
    if (!row || typeof row !== "object") return false;
    const candidate = row as Partial<DirectoryMember>;
    return typeof candidate.id === "string"
      && typeof candidate.name === "string"
      && typeof candidate.email === "string"
      && typeof candidate.role === "string";
  });
}

export function memberLabel(
  memberId: string | null | undefined,
  directory: DirectoryMember[],
  currentMember?: CollaborationIdentity,
): string {
  if (!memberId) return "Unassigned";
  if (currentMember && memberId === currentMember.id) {
    return currentMember.name?.trim() || currentMember.email;
  }
  const member = directory.find(candidate => candidate.id === memberId);
  return member?.name?.trim() || member?.email || `Member ${memberId.slice(0, 8)}`;
}

export function mentionToken(member: DirectoryMember): string {
  // Full email is unambiguous when an organization contains multiple domains
  // or two teammates share the same local-part alias.
  return `@${member.email}`;
}
