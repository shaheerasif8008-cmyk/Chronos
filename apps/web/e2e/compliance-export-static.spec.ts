import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

import { buildComplianceExportRequest } from "../lib/compliance-export";

const root = path.resolve(__dirname, "..");

function source(relativePath: string): string {
  return fs.readFileSync(path.join(root, relativePath), "utf8");
}

test("compliance export uses an inclusive calendar range with an exclusive API boundary", () => {
  expect(buildComplianceExportRequest("2026-07-01", "2026-07-31")).toEqual({
    since: "2026-07-01T00:00:00.000Z",
    until: "2026-08-01T00:00:00.000Z",
  });
  expect(buildComplianceExportRequest("", "")).toEqual({});
  expect(() => buildComplianceExportRequest("2026-08-01", "2026-07-31")).toThrow(
    "The from date must be on or before the to date.",
  );
});

test("compliance export creates a durable artifact and downloads its authenticated content", () => {
  const api = source("lib/compliance-export.ts");

  expect(api).toContain('apiFetch("/compliance/exports"');
  expect(api).toContain('method: "POST"');
  expect(api).toContain("receipt.download_path === fallbackPath");
  expect(api).toContain("/artifacts/${encodeURIComponent(receipt.artifact_id)}/content");
  expect(api).toContain('COMPLIANCE_EXPORT_FILENAME = "chronos-compliance.json"');
  expect(api).toContain("document.body.appendChild(anchor)");
  expect(api).toContain("URL.revokeObjectURL(url)");
});

test("compliance export control is permission gated and exposes busy, error, and success states", () => {
  const control = source("components/settings/ComplianceExportControl.tsx");

  expect(control).toContain("disabled={!canExport || busy}");
  expect(control).toContain('aria-busy={busy}');
  expect(control).toContain("Only organization owners and administrators can export compliance evidence.");
  expect(control).toContain('busy ? "Creating bundle…" : "Export compliance bundle"');
  expect(control).toContain('role="alert"');
  expect(control).toContain('role="status"');
  expect(control).toContain('aria-live="polite"');
  expect(control).toContain("createComplianceExport(since, until)");
  expect(control).toContain("fetchComplianceExport(receipt)");
  expect(control).toContain("downloadComplianceExport(blob)");
});

test("audit surfaces wire the compliance control to the admin capability and active filters", () => {
  const chat = source("app/chat/page.tsx");

  expect(chat).toContain('import ComplianceExportControl from "../../components/settings/ComplianceExportControl"');
  expect(chat).toContain("<AuditScreen canExport={canAdmin} />");
  expect(chat).toContain("<AuditSettings canExport={canAdmin} />");
  expect(chat).toContain("<ComplianceExportControl since={since} until={until} canExport={canExport}/>");
});
