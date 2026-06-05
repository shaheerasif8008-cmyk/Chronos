"use client";

const workspaceTools = [
  { label: "Clone/import", tool: "repo.clone", state: "Workspace jailed" },
  { label: "Branch", tool: "repo.create_branch", state: "Git tracked" },
  { label: "Inspect/edit", tool: "repo.read_file / repo.write_file", state: "Path checked" },
  { label: "Tests", tool: "repo.run_tests", state: "Pytest only" },
  { label: "Diff viewer", tool: "repo.diff", state: "Git diff" },
  { label: "Review", tool: "repo.review", state: "Artifact saved" },
  { label: "Commit", tool: "repo.commit", state: "SHA recorded" },
  { label: "Approval-gated PR", tool: "repo.create_pr", state: "Approval required" },
];

const sampleEvents = [
  { type: "workspace", detail: "repo.clone imports local workspace paths or public GitHub HTTPS URLs through the broker." },
  { type: "test", detail: "repo.run_tests accepts constrained pytest commands and captures stdout, stderr, status, and return code." },
  { type: "review", detail: "repo.review writes .chronos/code_review.json with inline findings and suggested patches." },
  { type: "pr", detail: "repo.create_pr returns approval_required until an approval record is supplied." },
];

function statusColor(label: string) {
  if (label.includes("Approval")) return "var(--warn)";
  if (label.includes("saved") || label.includes("recorded")) return "var(--accent)";
  return "var(--text-faint)";
}

export default function CodingAgentScreen() {
  return (
    <div className="flex-1 min-w-0 overflow-hidden flex flex-col" data-testid="coding-agent-workspace">
      <header className="px-10 pt-9 pb-5 flex items-start justify-between gap-6 flex-shrink-0">
        <div className="min-w-0">
          <h1 className="h-page tracking-tight">Coding</h1>
          <p className="mt-1.5 text-[14px]" style={{ color: "var(--text-dim)" }}>
            Repo workspaces, test loops, reviews, commits, and approval-gated pull request requests.
          </p>
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <span className="tag">Broker routed</span>
          <span className="tag">Audit ready</span>
          <span className="tag">No shell PR writes</span>
        </div>
      </header>

      <div className="flex-1 min-h-0 px-10 pb-10 grid gap-4" style={{ gridTemplateColumns: "340px minmax(0, 1fr)" }}>
        <aside className="surface border border-soft rounded-lg min-h-0 overflow-hidden flex flex-col">
          <div className="px-3 py-2 border-b hairline flex items-center justify-between">
            <span className="text-[12.5px] font-medium">Workspace tools</span>
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{workspaceTools.length}</span>
          </div>
          <div className="overflow-y-auto p-2 space-y-2">
            {workspaceTools.map(item => (
              <div key={item.label} className="rounded-md border border-soft p-3">
                <div className="flex items-center gap-2">
                  <span className="inline-block w-2 h-2 rounded-full" style={{ background: statusColor(item.state) }} />
                  <span className="text-[13px] font-medium truncate">{item.label}</span>
                </div>
                <div className="mt-1 font-mono text-[11.5px] truncate" style={{ color: "var(--text-dim)" }}>{item.tool}</div>
                <div className="mt-2 text-[11px]" style={{ color: "var(--text-faint)" }}>{item.state}</div>
              </div>
            ))}
          </div>
        </aside>

        <section className="surface border border-soft rounded-lg min-w-0 min-h-0 overflow-hidden flex flex-col">
          <div className="px-4 py-3 border-b hairline">
            <div className="text-[14px] font-medium">Code task console</div>
            <div className="text-[12px]" style={{ color: "var(--text-dim)" }}>
              Durable repo artifacts are written inside the task workspace under .chronos.
            </div>
          </div>

          <div className="flex-1 min-h-0 grid" style={{ gridTemplateColumns: "minmax(0, 1fr) 340px" }}>
            <div className="min-w-0 min-h-0 p-4 overflow-auto">
              <div className="rounded-lg border border-soft overflow-hidden">
                <div className="px-3 py-2 border-b hairline flex items-center justify-between">
                  <span className="text-[12.5px] font-medium">Diff viewer</span>
                  <span className="tag">repo.diff</span>
                </div>
                <pre className="m-0 p-4 text-[12px] overflow-auto min-h-[220px]" style={{ color: "var(--text-muted)", background: "var(--surface-2)" }}>{`diff --git a/pkg/math.py b/pkg/math.py
-    return a - b
+    return a + b

Test results are captured by repo.run_tests and attached to the workspace state.`}</pre>
              </div>

              <div className="mt-4 rounded-lg border border-soft overflow-hidden">
                <div className="px-3 py-2 border-b hairline text-[12.5px] font-medium">Task replay</div>
                <div className="divide-y" style={{ borderColor: "var(--border-soft)" }}>
                  {sampleEvents.map(event => (
                    <div key={event.type} className="px-3 py-2.5 text-[12.5px] flex items-start gap-3">
                      <span className="mt-1 inline-block w-1.5 h-1.5 rounded-full" style={{ background: "var(--accent)" }} />
                      <div className="min-w-0">
                        <div className="font-medium capitalize">{event.type}</div>
                        <div style={{ color: "var(--text-dim)" }}>{event.detail}</div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            <aside className="border-l hairline min-h-0 overflow-y-auto p-4 space-y-4">
              <div>
                <div className="text-[13px] font-medium">Review artifact</div>
                <div className="mt-2 rounded-lg border border-soft p-3 text-[12.5px] space-y-2" style={{ color: "var(--text-dim)" }}>
                  <div>Path: <span className="font-mono">.chronos/code_review.json</span></div>
                  <div>Contents: summary, inline findings, severity, suggested patches.</div>
                </div>
              </div>

              <div>
                <div className="text-[13px] font-medium">Pull request gate</div>
                <div className="mt-2 rounded-lg border border-soft p-3 text-[12.5px] space-y-2" style={{ color: "var(--text-dim)" }}>
                  <div>Action: <span className="font-mono">repo.create_pr</span></div>
                  <div>Without approval: returns <span className="font-mono">approval_required</span>.</div>
                  <div>With approval: records <span className="font-mono">.chronos/pull_request.json</span>.</div>
                </div>
              </div>

              <div>
                <div className="text-[13px] font-medium">Governance</div>
                <div className="mt-2 rounded-lg border border-soft p-3 text-[12.5px] space-y-2" style={{ color: "var(--text-dim)" }}>
                  <div>Every action is a `repo.*` runtime tool and executes through the broker.</div>
                  <div>File paths are jailed to the task workspace before reads, writes, tests, commits, and PR artifacts.</div>
                </div>
              </div>
            </aside>
          </div>
        </section>
      </div>
    </div>
  );
}
