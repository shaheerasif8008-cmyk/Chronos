import { test, expect } from "@playwright/test";
import fs from "fs";
import path from "path";

test("chat waits for attachment upload ids before sending", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain("function attachmentsBusy()");
  expect(source).toContain('a.state === "uploading" || a.id.startsWith("local-")');
  expect(source).toContain('setUploadError("Wait for the file upload to finish before sending.")');
  expect(source).toContain("disabled={!draft.trim() || attachmentsBusy()}");
});

test("chat sends and renders uploaded attachment refs with the user message", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain("function readyAttachmentRefs(): ArtifactRef[]");
  expect(source).toContain("const pendingAttachmentRefs = readyAttachmentRefs();");
  expect(source).toContain("const pendingAttachmentIds = pendingAttachmentRefs.map(a => a.id);");
  expect(source).toContain('artifacts: pendingAttachmentRefs.length ? pendingAttachmentRefs : undefined');
  expect(source).toContain("attachment_ids: pendingAttachmentIds");
});
