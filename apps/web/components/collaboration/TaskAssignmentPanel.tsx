"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";
import {
  CollaborationIdentity,
  DirectoryMember,
  loadMemberDirectory,
  memberLabel,
} from "../../lib/collaboration";
import { CommentsThread } from "./CommentsThread";

type AssignmentTask = {
  id: string;
  goal: string;
  triggered_by_member_id?: string | null;
  assignee_member_id?: string | null;
  assigned_by_member_id?: string | null;
  assigned_at?: string | null;
};

type AssignmentDetail = {
  task_id: string;
  assignee: DirectoryMember | null;
  assigned_by_member_id?: string | null;
  assigned_at?: string | null;
};

type AssignmentEvent = {
  id: string;
  task_id: string;
  from_member_id?: string | null;
  to_member_id?: string | null;
  actor_member_id: string;
  event_type: "assigned" | "reassigned" | "handoff" | "unassigned";
  note?: string | null;
  created_at?: string | null;
};

type TaskAssignmentPanelProps = {
  task: AssignmentTask;
  currentMember: CollaborationIdentity;
  onTaskChanged?: (task: AssignmentTask) => void;
};

function eventLabel(event: AssignmentEvent): string {
  if (event.event_type === "assigned") return "Assigned";
  if (event.event_type === "reassigned") return "Reassigned";
  if (event.event_type === "handoff") return "Handed off";
  return "Unassigned";
}

function eventTime(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function TaskAssignmentPanel({ task, currentMember, onTaskChanged }: TaskAssignmentPanelProps) {
  const [assignment, setAssignment] = useState<AssignmentDetail | null>(null);
  const [history, setHistory] = useState<AssignmentEvent[]>([]);
  const [directory, setDirectory] = useState<DirectoryMember[]>([]);
  const [selectedMemberId, setSelectedMemberId] = useState("");
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [directoryError, setDirectoryError] = useState("");
  const [confirmUnassign, setConfirmUnassign] = useState(false);

  const isOrgAdmin = ["admin", "owner"].includes(currentMember.role);
  const isTaskOwner = task.triggered_by_member_id === currentMember.id;
  const activeAssigneeId = assignment?.assignee?.id ?? task.assignee_member_id ?? null;
  const isAssignee = activeAssigneeId === currentMember.id;
  const canAssign = isOrgAdmin || isTaskOwner;
  const canHandoff = canAssign || isAssignee;

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    setDirectoryError("");
    try {
      const [nextAssignment, nextHistory, directoryResult] = await Promise.all([
        apiFetch(`/tasks/${encodeURIComponent(task.id)}/assignment`).then(response => response.json()) as Promise<AssignmentDetail>,
        apiFetch(`/tasks/${encodeURIComponent(task.id)}/assignment/history`).then(response => response.json()) as Promise<AssignmentEvent[]>,
        loadMemberDirectory()
          .then(rows => ({ rows, error: "" }))
          .catch(requestError => ({
            rows: [] as DirectoryMember[],
            error: requestError instanceof Error ? requestError.message : "The teammate directory could not be loaded.",
          })),
      ]);
      setAssignment(nextAssignment);
      setHistory(Array.isArray(nextHistory) ? nextHistory : []);
      setDirectory(directoryResult.rows);
      setDirectoryError(directoryResult.error);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Assignment details could not be loaded.");
    } finally {
      setLoading(false);
    }
  }, [task.id]);

  useEffect(() => { void load(); }, [load]);

  const candidates = useMemo(() => directory.filter(member => (
    member.id !== task.triggered_by_member_id
    && member.id !== activeAssigneeId
  )), [activeAssigneeId, directory, task.triggered_by_member_id]);

  useEffect(() => {
    if (!candidates.some(candidate => candidate.id === selectedMemberId)) {
      setSelectedMemberId(candidates[0]?.id ?? "");
    }
  }, [candidates, selectedMemberId]);

  const labels = useMemo(() => {
    const rows = [...directory];
    if (assignment?.assignee && !rows.some(member => member.id === assignment.assignee?.id)) {
      rows.push(assignment.assignee);
    }
    return rows;
  }, [assignment, directory]);

  async function changeAssignment(kind: "assign" | "handoff") {
    if (!selectedMemberId || busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch(
        kind === "handoff"
          ? `/tasks/${encodeURIComponent(task.id)}/handoff`
          : `/tasks/${encodeURIComponent(task.id)}/assignment`,
        {
          method: kind === "handoff" ? "POST" : "PUT",
          body: JSON.stringify({ member_id: selectedMemberId, note: note.trim() || null }),
        },
      ).then(result => result.json()) as { task?: AssignmentTask };
      if (response.task) onTaskChanged?.(response.task);
      setNote("");
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The task assignment could not be changed.");
    } finally {
      setBusy(false);
    }
  }

  async function unassign() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await apiFetch(
        `/tasks/${encodeURIComponent(task.id)}/assignment`,
        { method: "DELETE" },
      ).then(result => result.json()) as { task?: AssignmentTask };
      if (response.task) onTaskChanged?.(response.task);
      setConfirmUnassign(false);
      await load();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "The task could not be unassigned.");
    } finally {
      setBusy(false);
    }
  }

  const mutationKind: "assign" | "handoff" = activeAssigneeId && isAssignee && !canAssign ? "handoff" : "assign";
  const primaryLabel = activeAssigneeId
    ? mutationKind === "handoff" ? "Hand off task" : "Reassign task"
    : "Assign task";

  return (
    <div className="space-y-3">
      <section className="rounded-xl border border-soft" aria-labelledby={`assignment-${task.id}`}>
        <div className="flex flex-col gap-3 px-3 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-4">
          <div className="min-w-0">
            <h3 id={`assignment-${task.id}`} className="text-[13.5px] font-semibold">Responsibility</h3>
            <p className="mt-0.5 truncate text-[12.5px]" style={{ color: "var(--text-dim)" }}>
              {loading ? "Loading assignment…" : activeAssigneeId ? memberLabel(activeAssigneeId, labels, currentMember) : "No teammate is assigned"}
              {assignment?.assigned_at ? ` · since ${eventTime(assignment.assigned_at)}` : ""}
            </p>
          </div>
          {activeAssigneeId && canAssign && (
            confirmUnassign ? (
              <div className="flex flex-wrap gap-2">
                <button type="button" disabled={busy} className="btn btn-danger-soft btn-sm" onClick={() => void unassign()}>{busy ? "Unassigning…" : "Confirm unassign"}</button>
                <button type="button" disabled={busy} className="btn btn-ghost btn-sm" onClick={() => setConfirmUnassign(false)}>Cancel</button>
              </div>
            ) : (
              <button type="button" disabled={busy} className="btn btn-ghost btn-sm self-start sm:self-auto" style={{ color: "var(--danger)" }} onClick={() => setConfirmUnassign(true)}>Unassign</button>
            )
          )}
        </div>

        {error && <div role="alert" className="mx-3 mb-3 rounded-lg border px-3 py-2 text-[12.5px] sm:mx-4" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>{error}</div>}

        {canHandoff && !loading && (
          <form className="space-y-2 border-t hairline px-3 py-3 sm:px-4" onSubmit={event => { event.preventDefault(); void changeAssignment(mutationKind); }}>
            <div className="flex flex-col gap-2 sm:flex-row">
              <select aria-label="Assign task to" value={selectedMemberId} onChange={event => setSelectedMemberId(event.target.value)} disabled={busy || candidates.length === 0} className="surface min-w-0 flex-1 rounded-md border border-soft px-2.5 py-2 text-[13px]">
                {candidates.length === 0 ? <option value="">No eligible teammates</option> : candidates.map(member => <option key={member.id} value={member.id}>{member.name} · {member.email}</option>)}
              </select>
              <button type="submit" disabled={busy || !selectedMemberId} className="btn btn-accent btn-sm justify-center">{busy ? "Saving…" : primaryLabel}</button>
            </div>
            {directoryError && <p role="alert" className="text-[12px]" style={{ color: "var(--danger)" }}>{directoryError} Assignment changes are disabled until the directory is available.</p>}
            <textarea value={note} onChange={event => setNote(event.target.value)} rows={2} maxLength={2000} placeholder={mutationKind === "handoff" ? "Handoff context (optional)" : "Assignment note (optional)"} aria-label="Assignment note" className="surface w-full resize-y rounded-md border border-soft px-3 py-2 text-[12.5px] outline-none" />
          </form>
        )}

        <div className="border-t hairline px-3 py-3 sm:px-4">
          <h4 className="text-[12px] font-semibold uppercase tracking-wide" style={{ color: "var(--text-dim)" }}>History</h4>
          {loading ? (
            <p role="status" className="mt-2 text-[12.5px]" style={{ color: "var(--text-dim)" }}>Loading responsibility history…</p>
          ) : history.length === 0 ? (
            <p className="mt-2 text-[12.5px]" style={{ color: "var(--text-dim)" }}>No assignment changes yet.</p>
          ) : (
            <ol className="mt-2 space-y-2">
              {[...history].reverse().map(event => (
                <li key={event.id} className="rounded-lg px-3 py-2.5" style={{ background: "var(--surface-2)" }}>
                  <div className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-3">
                    <span className="text-[12.5px] font-medium">
                      {eventLabel(event)}
                      {event.to_member_id ? ` to ${memberLabel(event.to_member_id, labels, currentMember)}` : ""}
                    </span>
                    <time className="text-[11px]" style={{ color: "var(--text-dim)" }}>{eventTime(event.created_at)}</time>
                  </div>
                  <p className="mt-0.5 text-[11.5px]" style={{ color: "var(--text-dim)" }}>
                    By {memberLabel(event.actor_member_id, labels, currentMember)}
                    {event.from_member_id ? ` · from ${memberLabel(event.from_member_id, labels, currentMember)}` : ""}
                  </p>
                  {event.note && <p className="mt-1.5 whitespace-pre-wrap text-[12.5px] leading-relaxed">{event.note}</p>}
                </li>
              ))}
            </ol>
          )}
        </div>
      </section>

      <CommentsThread targetType="task" targetId={task.id} currentMember={currentMember} compact />
    </div>
  );
}
