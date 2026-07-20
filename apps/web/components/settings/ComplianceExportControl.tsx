"use client";

import { useState } from "react";

import {
  createComplianceExport,
  downloadComplianceExport,
  fetchComplianceExport,
} from "../../lib/compliance-export";

type ComplianceExportControlProps = {
  since: string;
  until: string;
  canExport: boolean;
};

type ExportStatus =
  | { kind: "idle" }
  | { kind: "busy" }
  | { kind: "error"; message: string }
  | { kind: "success"; recordCount: number };

export default function ComplianceExportControl({
  since,
  until,
  canExport,
}: ComplianceExportControlProps) {
  const [status, setStatus] = useState<ExportStatus>({ kind: "idle" });
  const busy = status.kind === "busy";

  async function exportBundle() {
    if (!canExport || busy) return;
    setStatus({ kind: "busy" });
    try {
      const receipt = await createComplianceExport(since, until);
      const blob = await fetchComplianceExport(receipt);
      downloadComplianceExport(blob);
      setStatus({
        kind: "success",
        recordCount: Number(receipt.manifest.record_count || 0),
      });
    } catch (error) {
      setStatus({
        kind: "error",
        message: error instanceof Error
          ? error.message
          : "The compliance bundle could not be created.",
      });
    }
  }

  const permissionMessage = "Only organization owners and administrators can export compliance evidence.";

  return (
    <div className="flex min-w-[220px] flex-col gap-1.5" data-testid="compliance-export-control">
      <button
        type="button"
        className="btn btn-accent btn-sm justify-center"
        disabled={!canExport || busy}
        aria-busy={busy}
        aria-describedby="compliance-export-help"
        title={canExport ? "Download a tamper-evident compliance bundle" : permissionMessage}
        onClick={() => void exportBundle()}
      >
        {busy ? "Creating bundle…" : "Export compliance bundle"}
      </button>
      <p id="compliance-export-help" className="text-[11.5px] leading-4" style={{ color: "var(--text-dim)" }}>
        {canExport
          ? "Includes the selected date range, integrity chain, and signed manifest."
          : permissionMessage}
      </p>
      {status.kind === "error" && (
        <p className="text-[12px] leading-4" role="alert" style={{ color: "var(--danger)" }}>
          {status.message}
        </p>
      )}
      {status.kind === "success" && (
        <p className="text-[12px] leading-4" role="status" aria-live="polite" style={{ color: "var(--ok)" }}>
          Downloaded {status.recordCount.toLocaleString()} evidence {status.recordCount === 1 ? "record" : "records"}.
        </p>
      )}
    </div>
  );
}
