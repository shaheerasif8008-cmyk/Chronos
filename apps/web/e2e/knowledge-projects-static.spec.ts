import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const page = fs.readFileSync(
  path.resolve(__dirname, "../app/chat/page.tsx"),
  "utf8",
);

test("global search is app-wide and federates every authorized result type", () => {
  expect(page).toContain("onGlobalSearchKeyDown");
  expect(page).toContain("openGlobalSearch");
  expect(page).toContain('apiFetch(`/search?q=${q}`');
  expect(page).toContain("Search conversations, tasks, artifacts, memory, and sources");
  for (const type of ["conversations", "messages", "tasks", "artifacts", "memory", "sources"]) {
    expect(page).toContain(`${type}:`);
  }
});

test("project chat, settings, membership, and instruction history are operable", () => {
  expect(page).toContain("Start project chat");
  expect(page).toContain("project_id=${encodeURIComponent(projectId)}");
  expect(page).toContain("project_id: effectiveProjectId ?? undefined");
  expect(page).toContain('form.append("project_id", effectiveProjectId)');
  expect(page).toContain("Project context:");
  expect(page).toContain("ProjectSettingsTab");
  expect(page).toContain("/instruction-versions");
  expect(page).toContain("/members");
  expect(page).toContain("Instruction history");
  expect(page).toContain("Access and default tools");
  expect(page).toContain('value="organization"');
  expect(page).toContain("useProjectToolPolicy ? defaultTools : []");
  expect(page).toContain("PROJECT_BUILT_IN_TOOLS");
});

test("organization-visible non-members get a read-only project surface", () => {
  expect(page).toContain('access_role?: "owner" | "member" | "viewer"');
  expect(page).toContain('project?.access_role === "owner" || project?.access_role === "member"');
  expect(page).toContain("Join this project to create conversations");
  expect(page).toContain("This organization-visible project is read-only");
  expect(page).toContain("disabled={!canEdit || busyId === \"upload\"}");
});

test("project sources support files, folders, URLs, connectors, and ACL download", () => {
  expect(page).toContain("Upload files");
  expect(page).toContain("Upload folder");
  expect(page).toContain('node.setAttribute("webkitdirectory", "")');
  expect(page).toContain("/sources/url");
  expect(page).toContain("connector_id: connectorId");
  expect(page).toContain("download_url");
  expect(page).not.toContain("apiFetch(`/artifacts/${artifactId}/content`)");
  expect(page).toContain("quarantined");
});

test("autonomous memory save remains visibly undoable", () => {
  expect(page).toContain("memoryNotice");
  expect(page).toContain("Saved to memory:");
  expect(page).toContain("undoMemoryNotice");
  expect(page).toContain('apiFetch(`/memory/${entryId}/undo`');
  expect(page).toContain(">Undo</button>");
});
