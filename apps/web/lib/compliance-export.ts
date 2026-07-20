import { apiFetch } from "./api";

export const COMPLIANCE_EXPORT_FILENAME = "chronos-compliance.json";

export type ComplianceExportManifest = {
  schema_version?: string;
  generated_at?: string;
  record_count?: number;
  chain_head?: string;
  signature?: string;
};

export type ComplianceExportReceipt = {
  artifact_id: string;
  download_path?: string;
  manifest: ComplianceExportManifest;
};

function utcDay(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!match) throw new Error("Choose a valid export date.");

  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (
    date.getUTCFullYear() !== year
    || date.getUTCMonth() !== month - 1
    || date.getUTCDate() !== day
  ) {
    throw new Error("Choose a valid export date.");
  }
  return date;
}

export function buildComplianceExportRequest(
  since: string,
  until: string,
): { since?: string; until?: string } {
  if (since && until && since > until) {
    throw new Error("The from date must be on or before the to date.");
  }

  const body: { since?: string; until?: string } = {};
  if (since) body.since = utcDay(since).toISOString();
  if (until) {
    const endExclusive = utcDay(until);
    endExclusive.setUTCDate(endExclusive.getUTCDate() + 1);
    body.until = endExclusive.toISOString();
  }
  return body;
}

function isReceipt(value: unknown): value is ComplianceExportReceipt {
  if (!value || typeof value !== "object") return false;
  const receipt = value as Partial<ComplianceExportReceipt>;
  return typeof receipt.artifact_id === "string" && Boolean(receipt.artifact_id) && Boolean(receipt.manifest);
}

export async function createComplianceExport(since: string, until: string): Promise<ComplianceExportReceipt> {
  const response = await apiFetch("/compliance/exports", {
    method: "POST",
    body: JSON.stringify(buildComplianceExportRequest(since, until)),
  });
  const receipt: unknown = await response.json();
  if (!isReceipt(receipt)) throw new Error("Chronos created an invalid compliance export receipt.");
  return receipt;
}

export async function fetchComplianceExport(receipt: ComplianceExportReceipt): Promise<Blob> {
  const fallbackPath = `/artifacts/${encodeURIComponent(receipt.artifact_id)}/content`;
  const path = receipt.download_path === fallbackPath
    ? receipt.download_path
    : fallbackPath;
  return (await apiFetch(path)).blob();
}

export function downloadComplianceExport(blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = COMPLIANCE_EXPORT_FILENAME;
  anchor.hidden = true;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
