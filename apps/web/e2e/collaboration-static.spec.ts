import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve(__dirname, "..");

function source(relativePath: string): string {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("shared conversations expose real ACL controls and fail closed for viewers", () => {
  const chat = source("app/chat/page.tsx");
  const access = source("components/collaboration/ConversationCollaboration.tsx");
  const directory = source("lib/collaboration.ts");

  expect(directory).toContain('apiFetch("/settings/member-directory")');
  expect(access).toContain("/chat/conversations/${encodeURIComponent(conversationId)}/members");
  expect(access).toContain('method: "PUT"');
  expect(access).toContain('method: "DELETE"');
  expect(access).toContain('type ConversationAccessRole = "owner" | "editor" | "viewer"');
  expect(access).toContain("Only the conversation owner can change sharing");

  expect(chat).toContain("conversationAccess.status === \"ready\"");
  expect(chat).toContain('["owner", "editor"].includes(conversationAccess.role');
  expect(chat).toContain("if (!canMutateConversation)");
  expect(chat).toContain("<fieldset disabled={!canMutateConversation}");
  expect(chat).toContain("canEdit={canMutateConversation}");
  expect(chat).toContain("ownedByCurrentMember && convoMenu === c.id");
  expect(chat).not.toContain('<button className="btn btn-ghost btn-icon"><IC.More size={15}/></button>');
});

test("task responsibility controls cover assign, reassign, handoff, unassign, and history", () => {
  const tasks = source("components/collaboration/TaskAssignmentPanel.tsx");
  const chat = source("app/chat/page.tsx");

  expect(tasks).toContain("/tasks/${encodeURIComponent(task.id)}/assignment/history");
  expect(tasks).toContain("/tasks/${encodeURIComponent(task.id)}/handoff");
  expect(tasks).toContain('method: kind === "handoff" ? "POST" : "PUT"');
  expect(tasks).toContain('{ method: "DELETE" }');
  expect(tasks).toContain('event.event_type === "reassigned"');
  expect(tasks).toContain("isTaskOwner");
  expect(tasks).toContain("isAssignee");
  expect(tasks).toContain("No assignment changes yet");
  expect(chat).toContain("<TaskAssignmentPanel");
});

test("comments and mentions are wired only to supported project, task, and artifact targets", () => {
  const comments = source("components/collaboration/CommentsThread.tsx");
  const tasks = source("components/collaboration/TaskAssignmentPanel.tsx");
  const artifacts = source("components/artifacts/ArtifactsScreen.tsx");
  const chat = source("app/chat/page.tsx");

  expect(comments).toContain('type CommentTarget = "project" | "task" | "artifact"');
  expect(comments).toContain('apiFetch("/comments"');
  expect(comments).toContain("Mentions notify only teammates who already have access");
  expect(comments).toContain("mentionToken(member)");
  expect(tasks).toContain('targetType="task"');
  expect(artifacts).toContain('targetType="artifact"');
  expect(chat).toContain('targetType="project"');
  expect(comments).not.toContain('target_type: "conversation"');
});
