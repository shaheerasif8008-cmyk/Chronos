"use client";

import { useEffect, useState } from "react";

import {
  cancelTask,
  pauseTask,
  resumeTask,
  retryTask,
  type TaskControlResult,
} from "../../lib/task-controls";

type TaskControlAction = "pause" | "resume" | "cancel" | "retry";

interface TaskControlsProps {
  task: { id: string; status: string };
  onTaskChanged?: (change: Pick<TaskControlResult, "task_id" | "status">) => void;
  onRefresh?: () => void | Promise<void>;
  compact?: boolean;
  showRetry?: boolean;
}

const PAUSABLE_STATUSES = new Set(["queued", "pending", "planning", "running"]);
const TERMINAL_STATUSES = new Set(["complete", "failed", "cancelled"]);
const KNOWN_STATUSES = new Set([
  ...PAUSABLE_STATUSES,
  "paused",
  "awaiting_approval",
  ...TERMINAL_STATUSES,
]);

export function TaskControls({ task, onTaskChanged, onRefresh, compact = false, showRetry = true }: TaskControlsProps) {
  const [busy, setBusy] = useState<TaskControlAction | null>(null);
  const [error, setError] = useState("");
  const status = task.status.toLowerCase();
  const awaitingApproval = status === "awaiting_approval";
  const canPause = PAUSABLE_STATUSES.has(status);
  const canResume = status === "paused";
  const canCancel = !TERMINAL_STATUSES.has(status) && KNOWN_STATUSES.has(status);
  const canRetry = showRetry && (status === "failed" || status === "cancelled");
  const knownStatus = KNOWN_STATUSES.has(status);

  useEffect(() => {
    setError("");
  }, [task.id, task.status]);

  async function run(action: TaskControlAction) {
    if (busy) return;
    if (action === "cancel" && !window.confirm("Cancel this task? Any progress already saved will remain available.")) {
      return;
    }

    setBusy(action);
    setError("");
    try {
      const result = action === "pause"
        ? await pauseTask(task.id)
        : action === "resume"
          ? await resumeTask(task.id)
          : action === "cancel"
            ? await cancelTask(task.id)
            : await retryTask(task.id);
      onTaskChanged?.({ task_id: result.task_id, status: result.status });
      await onRefresh?.();
    } catch (requestError) {
      const fallback: Record<TaskControlAction, string> = {
        pause: "The task could not be paused.",
        resume: "The task could not be resumed.",
        cancel: "The task could not be cancelled.",
        retry: "The task could not be retried.",
      };
      setError(requestError instanceof Error ? requestError.message : fallback[action]);
    } finally {
      setBusy(null);
    }
  }

  if (!knownStatus) {
    return (
      <p className="text-[12px]" style={{ color: "var(--text-dim)" }} role="status">
        Task controls are unavailable while the runtime reports “{task.status}”.
      </p>
    );
  }

  if (status === "complete") return null;
  if (!canPause && !canResume && !canCancel && !canRetry) return null;

  return (
    <div className={compact ? "space-y-1.5" : "space-y-2.5"}>
      {awaitingApproval && (
        <p className="text-[12px] leading-relaxed" style={{ color: "var(--warn)" }} role="status">
          This task is waiting for an approval decision. Approve or reject the pending request to continue; a task resume cannot bypass it.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Task controls">
        {canPause && (
          <button
            type="button"
            className="btn btn-secondary btn-sm disabled:opacity-50"
            disabled={busy !== null}
            onClick={() => void run("pause")}
          >
            {busy === "pause" ? "Pausing…" : "Pause task"}
          </button>
        )}
        {canResume && (
          <button
            type="button"
            className="btn btn-accent btn-sm disabled:opacity-50"
            disabled={busy !== null}
            onClick={() => void run("resume")}
          >
            {busy === "resume" ? "Resuming…" : "Resume task"}
          </button>
        )}
        {canCancel && (
          <button
            type="button"
            className="btn btn-danger-soft btn-sm disabled:opacity-50"
            disabled={busy !== null}
            onClick={() => void run("cancel")}
          >
            {busy === "cancel" ? "Cancelling…" : "Cancel task"}
          </button>
        )}
        {canRetry && (
          <button
            type="button"
            className="btn btn-secondary btn-sm disabled:opacity-50"
            disabled={busy !== null}
            onClick={() => void run("retry")}
          >
            {busy === "retry" ? "Retrying…" : "Retry task"}
          </button>
        )}
      </div>
      {error && (
        <p className="text-[12px]" style={{ color: "var(--danger)" }} role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
