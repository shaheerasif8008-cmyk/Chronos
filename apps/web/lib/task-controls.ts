import { apiFetch } from "./api";

export type KnownTaskStatus =
  | "queued"
  | "pending"
  | "planning"
  | "running"
  | "paused"
  | "awaiting_approval"
  | "complete"
  | "failed"
  | "cancelled";

export type TaskControlResult = {
  task_id: string;
  status: KnownTaskStatus;
  paused?: boolean;
  resumed?: boolean;
  cancelled?: boolean;
  retried?: boolean;
  reason?: string;
};

async function taskMutation(
  taskId: string,
  action: "pause" | "resume" | "cancel" | "retry",
  init: RequestInit = {},
): Promise<TaskControlResult> {
  const response = await apiFetch(`/tasks/${encodeURIComponent(taskId)}/${action}`, {
    method: "POST",
    ...init,
  });
  return response.json() as Promise<TaskControlResult>;
}

export function pauseTask(taskId: string, reason = "Paused by an operator from Chronos web"): Promise<TaskControlResult> {
  return taskMutation(taskId, "pause", { body: JSON.stringify({ reason }) });
}

export function resumeTask(taskId: string): Promise<TaskControlResult> {
  return taskMutation(taskId, "resume");
}

export function cancelTask(taskId: string): Promise<TaskControlResult> {
  return taskMutation(taskId, "cancel");
}

export function retryTask(taskId: string): Promise<TaskControlResult> {
  return taskMutation(taskId, "retry");
}
