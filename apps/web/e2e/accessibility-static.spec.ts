import fs from "node:fs";
import path from "node:path";
import { expect, test } from "@playwright/test";

const root = path.resolve(__dirname, "..");
const source = (file: string) => fs.readFileSync(path.join(root, file), "utf8");

test("modal surfaces trap focus, close with Escape, and restore the invoking focus", () => {
  const accessibility = source("lib/accessibility.ts");
  const chat = source("app/chat/page.tsx");
  const artifactPanel = source("components/artifacts/InChatArtifactPanel.tsx");
  const connectors = source("components/connectors/ConnectorsScreen.tsx");

  expect(accessibility).toContain('event.key === "Escape"');
  expect(accessibility).toContain('event.key !== "Tab"');
  expect(accessibility).toContain("previousFocus?.focus()");
  expect(chat).toContain('role="alertdialog"');
  expect(chat).toContain('aria-labelledby="task-activity-heading"');
  expect(chat).toContain('aria-labelledby="live-work-heading"');
  expect(artifactPanel).toContain('aria-labelledby="in-chat-artifact-heading"');
  expect(connectors).toContain('aria-labelledby="add-connector-title"');
});

test("menus, command search, and tabs expose keyboard-operable ARIA contracts", () => {
  const accessibility = source("lib/accessibility.ts");
  const chat = source("app/chat/page.tsx");
  const artifacts = source("components/artifacts/ArtifactsScreen.tsx");
  const connectors = source("components/connectors/ConnectorsScreen.tsx");
  const governance = source("components/connectors/ConnectorGovernanceScreen.tsx");

  expect(accessibility).toContain('"ArrowDown", "ArrowUp", "Home", "End"');
  expect(chat).toContain('role="combobox"');
  expect(chat).toContain('role="listbox"');
  expect(chat).toContain("aria-activedescendant");
  expect(artifacts).toContain('role="tablist" aria-label="Artifact views"');
  expect(artifacts).toContain("handleTabKeyDown");
  expect(connectors).toContain('role="tablist" aria-label="Connector views"');
  expect(governance).toContain('role="tablist" aria-label="Connector governance sections"');
  expect(governance).toContain("handleTabKeyDown");
});

test("dynamic feedback and icon-only controls have programmatic names", () => {
  const chat = source("app/chat/page.tsx");
  const browser = source("components/browser/BrowserOperatorScreen.tsx");
  const data = source("components/data/DataScreen.tsx");
  const connectors = source("components/connectors/ConnectorsScreen.tsx");

  expect(chat).toContain('aria-label="Send message"');
  expect(chat).toContain('aria-label="Close task activity"');
  expect(chat).toContain('aria-label="Search memories"');
  expect(browser).toContain('role="alert"');
  expect(browser).toContain('aria-label="Browser takeover hand-back summary"');
  expect(data).toContain('aria-label="Analysis code"');
  expect(data).toContain('role="alert"');
  expect(connectors).toContain("aria-label={`${app.name} ${toolActionLabel(tool.name)} permission`}");
  expect(connectors).toContain('role={kind === "error" ? "alert" : "status"}');
});
