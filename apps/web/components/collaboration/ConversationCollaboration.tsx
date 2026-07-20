"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import {
  CollaborationIdentity,
  DirectoryMember,
  loadMemberDirectory,
} from "../../lib/collaboration";

export type ConversationAccessRole = "owner" | "editor" | "viewer";
export type ConversationAccessState = {
  role: ConversationAccessRole | null;
  status: "loading" | "ready" | "error";
};

type ConversationMember = {
  member_id: string;
  role: ConversationAccessRole;
  granted_by_member_id?: string | null;
  email: string;
  name?: string | null;
  status?: string;
  created_at?: string;
  updated_at?: string;
};

type ConversationCollaborationProps = {
  conversationId: string;
  ownerMemberId?: string | null;
  currentMember: CollaborationIdentity;
  onAccessChange: (access: ConversationAccessState) => void;
};

function displayName(member: Pick<ConversationMember, "name" | "email">): string {
  return member.name?.trim() || member.email;
}

function accessLabel(role: ConversationAccessRole | null): string {
  if (role === "owner") return "Owner";
  if (role === "editor") return "Can edit";
  return "View only";
}

export function ConversationCollaboration({
  conversationId,
  ownerMemberId,
  currentMember,
  onAccessChange,
}: ConversationCollaborationProps) {
  const inferredOwner = ownerMemberId === currentMember.id;
  const [open, setOpen] = useState(false);
  const [members, setMembers] = useState<ConversationMember[]>([]);
  const [directory, setDirectory] = useState<DirectoryMember[]>([]);
  const [status, setStatus] = useState<ConversationAccessState["status"]>(
    inferredOwner ? "ready" : "loading",
  );
  const [error, setError] = useState("");
  const [directoryError, setDirectoryError] = useState("");
  const [selectedMemberId, setSelectedMemberId] = useState("");
  const [selectedRole, setSelectedRole] = useState<"editor" | "viewer">("viewer");
  const [busyMemberId, setBusyMemberId] = useState<string | null>(null);
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null);
  const dialogRef = useRef<HTMLElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const previousOpenRef = useRef(false);

  const accessRole = useMemo<ConversationAccessRole | null>(() => {
    if (inferredOwner) return "owner";
    const ownAcl = members.find(member => member.member_id === currentMember.id);
    return ownAcl?.role ?? null;
  }, [currentMember.id, inferredOwner, members]);

  const loadMembers = useCallback(async () => {
    setError("");
    if (!inferredOwner) setStatus("loading");
    try {
      const rows = await apiFetch(
        `/chat/conversations/${encodeURIComponent(conversationId)}/members`,
      ).then(response => response.json()) as ConversationMember[];
      if (!Array.isArray(rows)) throw new Error("The collaborator list returned an invalid response.");
      setMembers(rows);
      setStatus("ready");
    } catch (requestError) {
      setMembers([]);
      setStatus("error");
      setError(requestError instanceof Error ? requestError.message : "Collaborators could not be loaded.");
    }
  }, [conversationId, inferredOwner]);

  useEffect(() => { void loadMembers(); }, [loadMembers]);

  useEffect(() => {
    onAccessChange({ role: accessRole, status });
  }, [accessRole, onAccessChange, status]);

  useEffect(() => {
    if (!open || accessRole !== "owner") return;
    setDirectoryError("");
    void loadMemberDirectory()
      .then(setDirectory)
      .catch(requestError => {
        setDirectory([]);
        setDirectoryError(requestError instanceof Error ? requestError.message : "The teammate directory could not be loaded.");
      });
  }, [accessRole, open]);

  useEffect(() => {
    if (!open) return;
    function handleDialogKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), select:not([disabled]), input:not([disabled]), textarea:not([disabled]), [href]',
      )];
      if (focusable.length === 0) return;
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
    document.addEventListener("keydown", handleDialogKey);
    return () => document.removeEventListener("keydown", handleDialogKey);
  }, [open]);

  useEffect(() => {
    if (previousOpenRef.current && !open) triggerRef.current?.focus();
    previousOpenRef.current = open;
  }, [open]);

  const sharedMembers = members.filter(member => member.role !== "owner");
  const candidates = useMemo(() => directory.filter(member => (
    member.id !== currentMember.id
    && member.id !== ownerMemberId
    && !members.some(existing => existing.member_id === member.id)
  )), [currentMember.id, directory, members, ownerMemberId]);

  useEffect(() => {
    if (!candidates.some(candidate => candidate.id === selectedMemberId)) {
      setSelectedMemberId(candidates[0]?.id ?? "");
    }
  }, [candidates, selectedMemberId]);

  async function saveMember(memberId: string, role: "editor" | "viewer") {
    if (!memberId || busyMemberId) return;
    setBusyMemberId(memberId);
    setError("");
    try {
      await apiFetch(
        `/chat/conversations/${encodeURIComponent(conversationId)}/members/${encodeURIComponent(memberId)}`,
        { method: "PUT", body: JSON.stringify({ role }) },
      );
      await loadMembers();
      setSelectedRole("viewer");
      setConfirmRemoveId(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Conversation access could not be updated.");
    } finally {
      setBusyMemberId(null);
    }
  }

  async function removeMember(memberId: string) {
    if (busyMemberId) return;
    setBusyMemberId(memberId);
    setError("");
    try {
      await apiFetch(
        `/chat/conversations/${encodeURIComponent(conversationId)}/members/${encodeURIComponent(memberId)}`,
        { method: "DELETE" },
      );
      await loadMembers();
      setConfirmRemoveId(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Conversation access could not be removed.");
    } finally {
      setBusyMemberId(null);
    }
  }

  const buttonText = status === "loading"
    ? "Checking access…"
    : status === "error"
      ? "Access unavailable"
      : accessRole === "owner"
        ? sharedMembers.length > 0 ? `Shared · ${sharedMembers.length}` : "Share"
        : accessLabel(accessRole);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[12.5px] smooth hover:bg-[var(--surface-2)]"
        style={{ color: status === "error" ? "var(--danger)" : "var(--text-muted)" }}
        aria-haspopup="dialog"
        aria-label={`${buttonText}. Open conversation access`}
      >
        <span>Access</span>
        <span className="hidden max-w-[120px] truncate sm:inline">· {buttonText}</span>
      </button>

      {open && (
        <div
          className="fixed inset-0 z-[90] flex items-end justify-center bg-black/25 sm:items-center sm:p-4"
          onMouseDown={event => { if (event.currentTarget === event.target) setOpen(false); }}
        >
          <section
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="conversation-access-heading"
            className="surface flex max-h-[92dvh] w-full flex-col overflow-hidden rounded-t-2xl border border-soft shadow-2xl sm:max-w-[600px] sm:rounded-2xl"
          >
            <header className="flex items-start justify-between gap-4 border-b hairline px-4 py-4 sm:px-5">
              <div className="min-w-0">
                <h2 id="conversation-access-heading" className="text-[16px] font-semibold">Conversation access</h2>
                <p className="mt-1 text-[12.5px] leading-relaxed" style={{ color: "var(--text-dim)" }}>
                  {accessRole === "owner"
                    ? "Editors can contribute. Viewers can read the conversation without changing it."
                    : `You have ${accessLabel(accessRole).toLowerCase()} access. Only the conversation owner can change sharing.`}
                </p>
              </div>
              <button autoFocus type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(false)} aria-label="Close conversation access">Close</button>
            </header>

            <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4 sm:px-5">
              {error && <div role="alert" className="rounded-lg border px-3 py-2 text-[12.5px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div>}

              {status === "loading" ? (
                <div role="status" className="py-5 text-[13px]" style={{ color: "var(--text-dim)" }}>Loading conversation access…</div>
              ) : (
                <div className="overflow-hidden rounded-xl border border-soft">
                  {members.map(member => {
                    const isOwner = member.role === "owner";
                    const isBusy = busyMemberId === member.member_id;
                    return (
                      <div key={member.member_id} className="flex flex-col gap-3 border-b hairline px-3 py-3 last:border-b-0 sm:flex-row sm:items-center">
                        <div className="min-w-0 flex-1">
                          <div className="truncate text-[13.5px] font-medium">{displayName(member)}{member.member_id === currentMember.id ? " · you" : ""}</div>
                          <div className="truncate text-[12px]" style={{ color: "var(--text-dim)" }}>{member.email}</div>
                        </div>
                        {accessRole === "owner" && !isOwner ? (
                          <div className="flex flex-wrap items-center gap-2">
                            <select
                              aria-label={`Access for ${member.email}`}
                              value={member.role}
                              disabled={isBusy}
                              onChange={event => void saveMember(member.member_id, event.target.value as "editor" | "viewer")}
                              className="surface rounded-md border border-soft px-2 py-1.5 text-[12.5px]"
                            >
                              <option value="editor">Can edit</option>
                              <option value="viewer">View only</option>
                            </select>
                            {confirmRemoveId === member.member_id ? (
                              <>
                                <button type="button" disabled={isBusy} className="btn btn-danger-soft btn-sm" onClick={() => void removeMember(member.member_id)}>{isBusy ? "Removing…" : "Confirm remove"}</button>
                                <button type="button" disabled={isBusy} className="btn btn-ghost btn-sm" onClick={() => setConfirmRemoveId(null)}>Cancel</button>
                              </>
                            ) : (
                              <button type="button" disabled={isBusy} className="btn btn-ghost btn-sm" style={{ color: "var(--danger)" }} onClick={() => setConfirmRemoveId(member.member_id)}>Remove</button>
                            )}
                          </div>
                        ) : (
                          <span className="self-start rounded-full px-2 py-1 text-[11.5px] font-medium sm:self-auto" style={{ background: "var(--surface-2)", color: "var(--text-muted)" }}>{accessLabel(member.role)}</span>
                        )}
                      </div>
                    );
                  })}
                  {members.length === 0 && status !== "error" && <div className="px-3 py-5 text-[13px]" style={{ color: "var(--text-dim)" }}>No access records were returned.</div>}
                </div>
              )}

              {accessRole === "owner" && status === "ready" && (
                <section aria-labelledby="share-with-heading" className="rounded-xl border border-soft p-3 sm:p-4">
                  <h3 id="share-with-heading" className="text-[13.5px] font-semibold">Share with a teammate</h3>
                  <p className="mt-1 text-[12px]" style={{ color: "var(--text-dim)" }}>Only active members of this workspace can be added.</p>
                  {directoryError ? (
                    <div role="alert" className="mt-3 text-[12.5px]" style={{ color: "var(--danger)" }}>{directoryError}</div>
                  ) : candidates.length === 0 ? (
                    <div className="mt-3 text-[12.5px]" style={{ color: "var(--text-dim)" }}>{directory.length === 0 ? "Loading teammate directory…" : "Everyone eligible already has access."}</div>
                  ) : (
                    <form className="mt-3 flex flex-col gap-2 sm:flex-row" onSubmit={event => { event.preventDefault(); void saveMember(selectedMemberId, selectedRole); }}>
                      <select aria-label="Teammate" value={selectedMemberId} onChange={event => setSelectedMemberId(event.target.value)} className="surface min-w-0 flex-1 rounded-md border border-soft px-2.5 py-2 text-[13px]">
                        {candidates.map(member => <option key={member.id} value={member.id}>{member.name} · {member.email}</option>)}
                      </select>
                      <select aria-label="Conversation access" value={selectedRole} onChange={event => setSelectedRole(event.target.value as "editor" | "viewer")} className="surface rounded-md border border-soft px-2.5 py-2 text-[13px]">
                        <option value="viewer">View only</option>
                        <option value="editor">Can edit</option>
                      </select>
                      <button type="submit" disabled={!selectedMemberId || busyMemberId !== null} className="btn btn-accent btn-sm justify-center">{busyMemberId === selectedMemberId ? "Sharing…" : "Share"}</button>
                    </form>
                  )}
                </section>
              )}
            </div>
          </section>
        </div>
      )}
    </>
  );
}
