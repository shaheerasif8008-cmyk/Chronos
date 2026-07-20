import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

test("research lives in Activity and Projects, not a top-level route", async () => {
  const pageSrc = fs.readFileSync(path.join(process.cwd(), "app/chat/page.tsx"), "utf8");
  // Research renders as a tab inside the Activity surface (and as a Projects
  // tab), driven by local tab state rather than a top-level route.
  expect(pageSrc).toContain("<ResearchScreen");
  expect(pageSrc).not.toContain('route === "research"');
  // Projects keeps a Research tab.
  expect(pageSrc).toContain('{ id: "research", label: "Research" }');

  // The standalone /research route deep-links the matching Activity tab.
  const routeSrc = fs.readFileSync(path.join(process.cwd(), "app/research/page.tsx"), "utf8");
  expect(routeSrc).toContain('redirect("/activity?tab=research")');

  const researchSrc = fs.readFileSync(path.join(process.cwd(), "components/research/ResearchScreen.tsx"), "utf8");
  expect(researchSrc).toContain("allowed_domains: parseDomains(allowedDomains)");
  expect(researchSrc).toContain("disallowed_domains: parseDomains(disallowedDomains)");
  expect(researchSrc).toContain("citation_policy: citationPolicy");
  expect(researchSrc).toContain('kind="markdown" mimeType="text/markdown"');
  expect(researchSrc).toContain('["upload", "Uploaded files"');
  expect(researchSrc).toContain('["mcp", "Read-only MCP tool"');
  expect(researchSrc).toContain("/research/mcp-tools?server_id=");
  expect(researchSrc).toContain("mcp_tools: mcpScope");
  expect(researchSrc).toContain('data-testid="research-export-docx"');
  expect(researchSrc).toContain('data-testid="research-export-pdf"');
  expect(researchSrc).toContain('apiFetch(`/research/${runId}/export`');
  expect(researchSrc).toContain("getContentBlob(artifact.id)");
  expect(researchSrc).toContain("Export saved to Artifacts and downloaded.");
});
