"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";
import {
  CollaborationIdentity,
  DirectoryMember,
  loadMemberDirectory,
  memberLabel,
  mentionToken,
} from "../../lib/collaboration";

type CommentTarget = "project" | "task" | "artifact";

type CommentRow = {
  id: string;
  target_type: CommentTarget;
  target_id: string;
  author_member_id: string;
  body: string;
  mentions: string[];
  created_at?: string | null;
};

type CommentsThreadProps = {
  targetType: CommentTarget;
  targetId: string;
  currentMember: CollaborationIdentity;
  compact?: boolean;
};

function formatCommentTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function CommentsThread({ targetType, targetId, currentMember, compact = false }: CommentsThreadProps) {
  const [comments, setComments] = useState<CommentRow[]>([]);
  const [directory, setDirectory] = useState<DirectoryMember[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(!compact);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const loadComments = useCallback(async () => {
    setLoading(true);
    setError("");
    const params = new URLSearchParams({ target_type: targetType, target_id: targetId });
    try {
      const rows = await apiFetch(`/comments?${params.toString()}`).then(response => response.json()) as CommentRow[];
      if (!Array.isArray(rows)) throw new Error("The comment thread returned an invalid response.");
      setComments(rows);
    } catch (requestError) {
      setComments([]);
      setError(requestError instanceof Error ? requestError.message : "Comments could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [targetId, targetType]);

  useEffect(() => { void loadComments(); }, [loadComments]);
  useEffect(() => {
    void loadMemberDirectory().then(setDirectory).catch(() => setDirectory([]));
  }, []);

  const mentionCandidates = useMemo(
    () => directory.filter(member => member.id !== currentMember.id).slice(0, 12),
    [currentMember.id, directory],
  );

  async function addComment() {
    const body = draft.trim();
    if (!body || busy) return;
    setBusy(true);
    setError("");
    try {
      await apiFetch("/comments", {
        method: "POST",
        body: JSON.stringify({ target_type: targetType, target_id: targetId, body }),
      });
      setDraft("");
      await loadComments();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The comment could not be posted.");
    } finally {
      setBusy(false);
    }
  }

  async function deleteComment(commentId: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await apiFetch(`/comments/${encodeURIComponent(commentId)}`, { method: "DELETE" });
      setComments(previous => previous.filter(comment => comment.id !== commentId));
      setConfirmDeleteId(null);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The comment could not be deleted.");
    } finally {
      setBusy(false);
    }
  }

  function insertMention(member: DirectoryMember) {
    const token = mentionToken(member);
    setDraft(previous => `${previous}${previous && !previous.endsWith(" ") ? " " : ""}${token} `);
  }

  return (
    <section className="rounded-xl border border-soft" aria-labelledby={`comments-${targetType}-${targetId}`}>
      <button
        type="button"
        onClick={() => setExpanded(value => !value)}
        className="flex w-full items-center justify-between gap-3 px-3 py-3 text-left sm:px-4"
        aria-expanded={expanded}
      >
        <span id={`comments-${targetType}-${targetId}`} className="text-[13.5px] font-semibold">Comments</span>
        <span className="flex items-center gap-2 text-[12px]" style={{ color: "var(--text-dim)" }}>
          {loading ? "Loading…" : `${comments.length} ${comments.length === 1 ? "comment" : "comments"}`}
          <span>{expanded ? "Hide" : "Show"}</span>
        </span>
      </button>

      {expanded && (
        <div className="space-y-3 border-t hairline px-3 py-3 sm:px-4">
          {error && <div role="alert" className="rounded-lg border px-3 py-2 text-[12.5px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div>}
          {!loading && comments.length === 0 && !error && <p className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>No comments yet. Start the review thread below.</p>}
          {comments.length > 0 && (
            <div className="space-y-2.5">
              {comments.map(comment => {
                const canDelete = comment.author_member_id === currentMember.id || ["admin", "owner"].includes(currentMember.role);
                return (
                  <article key={comment.id} className="rounded-lg p-3" style={{ background: "var(--surface-2)" }}>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-[12.5px] font-semibold">{memberLabel(comment.author_member_id, directory, currentMember)}</div>
                        <time className="text-[11px]" style={{ color: "var(--text-dim)" }}>{formatCommentTime(comment.created_at)}</time>
                      </div>
                      {canDelete && (
                        confirmDeleteId === comment.id ? (
                          <div className="flex flex-wrap justify-end gap-1.5">
                            <button type="button" disabled={busy} className="btn btn-danger-soft btn-sm" onClick={() => void deleteComment(comment.id)}>Confirm</button>
                            <button type="button" disabled={busy} className="btn btn-ghost btn-sm" onClick={() => setConfirmDeleteId(null)}>Cancel</button>
                          </div>
                        ) : (
                          <button type="button" disabled={busy} className="btn btn-ghost btn-sm" style={{ color: "var(--text-dim)" }} onClick={() => setConfirmDeleteId(comment.id)} aria-label="Delete comment">Delete</button>
                        )
                      )}
                    </div>
                    <p className="mt-2 whitespace-pre-wrap break-words text-[13px] leading-relaxed">{comment.body}</p>
                  </article>
                );
              })}
            </div>
          )}

          <form onSubmit={event => { event.preventDefault(); void addComment(); }}>
            <label htmlFor={`comment-draft-${targetType}-${targetId}`} className="text-[12px] font-medium">Add a comment</label>
            <textarea
              id={`comment-draft-${targetType}-${targetId}`}
              value={draft}
              onChange={event => setDraft(event.target.value)}
              rows={3}
              maxLength={5000}
              placeholder="Share context or mention a teammate with @name"
              className="surface mt-1.5 w-full resize-y rounded-lg border border-soft px-3 py-2 text-[13px] outline-none"
            />
            {mentionCandidates.length > 0 && (
              <div className="mt-2 flex flex-wrap items-center gap-1.5" aria-label="Mention teammates">
                <span className="mr-1 text-[11.5px]" style={{ color: "var(--text-dim)" }}>Mention:</span>
                {mentionCandidates.map(member => (
                  <button key={member.id} type="button" onClick={() => insertMention(member)} className="rounded-full border border-soft px-2 py-1 text-[11.5px] smooth hover:bg-[var(--surface-2)]">{member.name}</button>
                ))}
              </div>
            )}
            <div className="mt-2 flex flex-col-reverse items-start justify-between gap-2 sm:flex-row sm:items-center">
              <p className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>Mentions notify only teammates who already have access to this {targetType}.</p>
              <button type="submit" disabled={busy || !draft.trim()} className="btn btn-accent btn-sm self-end sm:self-auto">{busy ? "Posting…" : "Post comment"}</button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}
