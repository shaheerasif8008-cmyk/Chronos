import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("chat history search uses previous-chat scope and opens matching conversations", async () => {
  const source = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");

  expect(source).toContain("Search previous chats");
  expect(source).toContain("/search?q=${q}&types=conversations,messages");
  expect(source).toContain("onConvoSelected(conversationId)");
  expect(source).toContain("new URLSearchParams(window.location.search).get(\"c\")");
  expect(source).toContain("router.push(`/chat?c=${encodeURIComponent(id)}`)");
});
