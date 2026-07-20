"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../lib/api";

type CustomAction = {
  name: string;
  description: string;
  method: "GET" | "HEAD" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  request_schema: Record<string, unknown>;
  risk_level: "read" | "write" | "destructive";
  approval_required: boolean;
};

type CustomHTTPConnector = {
  id: string;
  connector_id: string;
  name: string;
  base_url: string;
  status: "active" | "disabled";
  last_health_status?: string | null;
  last_health_at?: string | null;
  actions: CustomAction[];
};

type WebhookEndpoint = {
  id: string;
  name: string;
  event_type: string;
  trigger_source: string;
  url: string;
  secret_fingerprint: string;
  status: "active" | "disabled";
  rate_limit_per_minute: number;
  last_received_at?: string | null;
  signing_secret?: string;
};

type Workflow = { id: string; name: string; status: string };

type DraftAction = {
  id: string;
  name: string;
  description: string;
  method: CustomAction["method"];
  path: string;
  schema: string;
};

const EMPTY_ACTION: DraftAction = {
  id: "initial-action",
  name: "list_items",
  description: "List items",
  method: "GET",
  path: "/v1/items",
  schema: JSON.stringify(
    {
      type: "object",
      properties: { params: { type: "object" }, path_params: { type: "object" } },
      additionalProperties: false,
    },
    null,
    2,
  ),
};

function message(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatTime(value?: string | null): string {
  if (!value) return "Never";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "Unknown" : parsed.toLocaleString();
}

export default function CustomIntegrationsPanel() {
  const [httpConnectors, setHttpConnectors] = useState<CustomHTTPConnector[]>([]);
  const [webhooks, setWebhooks] = useState<WebhookEndpoint[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [secretReveal, setSecretReveal] = useState<WebhookEndpoint | null>(null);

  const [httpName, setHttpName] = useState("");
  const [httpBaseUrl, setHttpBaseUrl] = useState("");
  const [httpAuthHeader, setHttpAuthHeader] = useState("Authorization");
  const [httpAuthToken, setHttpAuthToken] = useState("");
  const [actions, setActions] = useState<DraftAction[]>([{ ...EMPTY_ACTION }]);

  const [webhookName, setWebhookName] = useState("");
  const [webhookEventType, setWebhookEventType] = useState("event.received");
  const [webhookRate, setWebhookRate] = useState(60);
  const [webhookWorkflowId, setWebhookWorkflowId] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [httpResponse, webhookResponse, workflowResponse] = await Promise.all([
        apiFetch("/connectors/custom-http").then(response => response.json()) as Promise<CustomHTTPConnector[]>,
        apiFetch("/connectors/webhook-endpoints").then(response => response.json()) as Promise<WebhookEndpoint[]>,
        apiFetch("/workflows/").then(response => response.json()) as Promise<Workflow[]>,
      ]);
      setHttpConnectors(httpResponse);
      setWebhooks(webhookResponse);
      setWorkflows(workflowResponse);
    } catch (caught) {
      setError(message(caught, "Could not load custom integrations."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  function updateAction(index: number, patch: Partial<DraftAction>) {
    setActions(current => current.map((action, actionIndex) => (
      actionIndex === index ? { ...action, ...patch } : action
    )));
  }

  async function createHTTPConnector() {
    setError("");
    setNotice("");
    let parsedActions: Array<Record<string, unknown>>;
    try {
      parsedActions = actions.map(action => {
        const schema = JSON.parse(action.schema) as unknown;
        if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
          throw new Error(`${action.name || "Action"}: request schema must be a JSON object.`);
        }
        return {
          name: action.name.trim(),
          description: action.description.trim(),
          method: action.method,
          path: action.path.trim(),
          request_schema: schema,
        };
      });
    } catch (caught) {
      setError(message(caught, "Action schemas must be valid JSON objects."));
      return;
    }
    setBusy("create-http");
    try {
      await apiFetch("/connectors/custom-http", {
        method: "POST",
        body: JSON.stringify({
          name: httpName.trim(),
          base_url: httpBaseUrl.trim(),
          auth_header: httpAuthHeader.trim(),
          auth_token: httpAuthToken,
          actions: parsedActions,
        }),
      });
      setHttpName("");
      setHttpBaseUrl("");
      setHttpAuthHeader("Authorization");
      setHttpAuthToken("");
      setActions([{ ...EMPTY_ACTION }]);
      setNotice("Custom HTTPS connector created. The credential is encrypted and is not shown again.");
      await load();
    } catch (caught) {
      setError(message(caught, "Could not create custom HTTPS connector."));
    } finally {
      setBusy(null);
    }
  }

  async function verifyHTTP(connector: CustomHTTPConnector) {
    setBusy(`health-${connector.connector_id}`);
    setError("");
    try {
      const result = await apiFetch(`/connectors/custom-http/${encodeURIComponent(connector.connector_id)}/health`, { method: "POST" }).then(response => response.json()) as { status: string };
      setNotice(`${connector.name}: ${result.status}.`);
      await load();
    } catch (caught) {
      setError(message(caught, `Could not verify ${connector.name}.`));
    } finally {
      setBusy(null);
    }
  }

  async function revokeHTTP(connector: CustomHTTPConnector) {
    if (!window.confirm(`Revoke ${connector.name}? Its encrypted credential is permanently deleted and all actions stop.`)) return;
    setBusy(`revoke-${connector.connector_id}`);
    setError("");
    try {
      await apiFetch(`/connectors/custom-http/${encodeURIComponent(connector.connector_id)}`, { method: "DELETE" });
      setNotice(`${connector.name} revoked.`);
      await load();
    } catch (caught) {
      setError(message(caught, `Could not revoke ${connector.name}.`));
    } finally {
      setBusy(null);
    }
  }

  async function createWebhook() {
    setBusy("create-webhook");
    setError("");
    setNotice("");
    setSecretReveal(null);
    try {
      const endpoint = await apiFetch("/connectors/webhook-endpoints", {
        method: "POST",
        body: JSON.stringify({
          name: webhookName.trim(),
          event_type: webhookEventType.trim(),
          rate_limit_per_minute: webhookRate,
          workflow_id: webhookWorkflowId || null,
        }),
      }).then(response => response.json()) as WebhookEndpoint;
      setSecretReveal(endpoint);
      setWebhookName("");
      setNotice("Webhook endpoint created. Copy the signing secret now; it will not be shown again.");
      await load();
    } catch (caught) {
      setError(message(caught, "Could not create webhook endpoint."));
    } finally {
      setBusy(null);
    }
  }

  async function rotateWebhook(endpoint: WebhookEndpoint) {
    if (!window.confirm(`Rotate the signing secret for ${endpoint.name}? The previous secret stops working immediately.`)) return;
    setBusy(`rotate-${endpoint.id}`);
    setError("");
    setSecretReveal(null);
    try {
      const updated = await apiFetch(`/connectors/webhook-endpoints/${encodeURIComponent(endpoint.id)}/rotate`, { method: "POST" }).then(response => response.json()) as WebhookEndpoint;
      setSecretReveal(updated);
      setNotice("Signing secret rotated. Copy the replacement now; it will not be shown again.");
      await load();
    } catch (caught) {
      setError(message(caught, `Could not rotate ${endpoint.name}.`));
    } finally {
      setBusy(null);
    }
  }

  async function updateWebhookStatus(endpoint: WebhookEndpoint) {
    const nextStatus = endpoint.status === "active" ? "disabled" : "active";
    if (nextStatus === "disabled" && !window.confirm(`Disable ${endpoint.name}? New deliveries will be rejected.`)) return;
    setBusy(`status-${endpoint.id}`);
    setError("");
    try {
      await apiFetch(`/connectors/webhook-endpoints/${encodeURIComponent(endpoint.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ status: nextStatus }),
      });
      setNotice(`${endpoint.name} ${nextStatus}.`);
      await load();
    } catch (caught) {
      setError(message(caught, `Could not update ${endpoint.name}.`));
    } finally {
      setBusy(null);
    }
  }

  async function testWebhook(endpoint: WebhookEndpoint) {
    setBusy(`test-${endpoint.id}`);
    setError("");
    try {
      const result = await apiFetch(`/connectors/webhook-endpoints/${encodeURIComponent(endpoint.id)}/test`, { method: "POST" }).then(response => response.json()) as { workflow_run_ids: string[] };
      setNotice(`${endpoint.name}: verified test created ${result.workflow_run_ids.length} matching workflow run${result.workflow_run_ids.length === 1 ? "" : "s"}.`);
      await load();
    } catch (caught) {
      setError(message(caught, `Could not test ${endpoint.name}.`));
    } finally {
      setBusy(null);
    }
  }

  async function copy(value: string, label: string) {
    await navigator.clipboard.writeText(value);
    setNotice(`${label} copied.`);
  }

  return (
    <section className="mb-8 space-y-5" aria-labelledby="custom-integrations-title">
      <div>
        <h2 id="custom-integrations-title" className="text-[13px] font-semibold uppercase tracking-wide" style={{ color: "var(--text-faint)" }}>Custom integrations</h2>
        <p className="mt-1 text-[12.5px]" style={{ color: "var(--text-dim)" }}>
          Admin-managed HTTPS APIs and signed inbound events. Secrets are encrypted, responses are treated as untrusted, and write actions remain approval-gated.
        </p>
      </div>

      {error && <div role="alert" className="rounded-lg border border-soft px-3 py-2 text-[12.5px]" style={{ color: "var(--danger)" }}>{error}</div>}
      {notice && <div role="status" aria-live="polite" className="rounded-lg border border-soft px-3 py-2 text-[12.5px]" style={{ color: "var(--ok)" }}>{notice}</div>}
      {loading && <div role="status" className="text-[12.5px]" style={{ color: "var(--text-dim)" }}>Loading custom integrations…</div>}

      <details className="surface border border-soft rounded-xl p-4">
        <summary className="cursor-pointer text-[14px] font-semibold">Create custom HTTPS connector</summary>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <label className="grid gap-1 text-[12px]">Name<input className="input" value={httpName} onChange={event => setHttpName(event.target.value)} /></label>
          <label className="grid gap-1 text-[12px]">Public HTTPS base URL<input className="input" type="url" placeholder="https://api.example.com" value={httpBaseUrl} onChange={event => setHttpBaseUrl(event.target.value)} /></label>
          <label className="grid gap-1 text-[12px]">Authentication header<input className="input font-mono" value={httpAuthHeader} onChange={event => setHttpAuthHeader(event.target.value)} /></label>
          <label className="grid gap-1 text-[12px]">Authentication token<input className="input font-mono" type="password" autoComplete="new-password" value={httpAuthToken} onChange={event => setHttpAuthToken(event.target.value)} /></label>
        </div>
        <div className="mt-4 space-y-3">
          {actions.map((action, index) => (
            <fieldset key={action.id} className="rounded-lg border border-soft p-3">
              <legend className="px-1 text-[12px] font-semibold">Action {index + 1}</legend>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="grid gap-1 text-[12px]">Action name<input className="input font-mono" value={action.name} onChange={event => updateAction(index, { name: event.target.value })} /></label>
                <label className="grid gap-1 text-[12px]">Description<input className="input" value={action.description} onChange={event => updateAction(index, { description: event.target.value })} /></label>
                <label className="grid gap-1 text-[12px]">Method<select className="input" value={action.method} onChange={event => updateAction(index, { method: event.target.value as DraftAction["method"] })}>{["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"].map(method => <option key={method}>{method}</option>)}</select></label>
                <label className="grid gap-1 text-[12px]">Path<input className="input font-mono" placeholder="/v1/items/{id}" value={action.path} onChange={event => updateAction(index, { path: event.target.value })} /></label>
                <label className="grid gap-1 text-[12px] md:col-span-2">Request JSON Schema<textarea className="input min-h-32 resize-y font-mono" value={action.schema} onChange={event => updateAction(index, { schema: event.target.value })} /></label>
              </div>
              {actions.length > 1 && <button className="btn btn-danger-soft btn-sm mt-3" onClick={() => setActions(current => current.filter(item => item.id !== action.id))}>Remove action</button>}
            </fieldset>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap justify-between gap-2">
          <button className="btn btn-ghost btn-sm" onClick={() => setActions(current => [...current, { ...EMPTY_ACTION, id: crypto.randomUUID(), name: `action_${current.length + 1}` }])}>Add action</button>
          <button className="btn btn-accent btn-sm" disabled={busy === "create-http" || !httpName.trim() || !httpBaseUrl.trim() || !httpAuthToken || actions.some(action => !action.name.trim() || !action.path.trim())} onClick={() => void createHTTPConnector()}>{busy === "create-http" ? "Creating…" : "Create connector"}</button>
        </div>
      </details>

      {httpConnectors.length > 0 && (
        <div className="surface border border-soft rounded-xl overflow-hidden">
          {httpConnectors.map(connector => (
            <div key={connector.id} className="border-b hairline px-4 py-3 last:border-b-0">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-[14px]">{connector.name}</span><span className={`tag ${connector.status === "active" ? "tag-ok" : "tag-warn"}`}>{connector.status}</span>{connector.last_health_status && <span className="tag">{connector.last_health_status}</span>}</div>
                  <div className="mt-1 truncate font-mono text-[11.5px]" style={{ color: "var(--text-dim)" }}>{connector.base_url}</div>
                  <div className="mt-2 flex flex-wrap gap-1.5">{connector.actions.map(action => <span key={action.name} className={`tag ${action.approval_required ? "tag-warn" : ""}`} title={action.approval_required ? "Human approval is required" : "Read-only action"}>{action.method} {action.name}</span>)}</div>
                  <div className="mt-1 text-[11px]" style={{ color: "var(--text-faint)" }}>Last health check: {formatTime(connector.last_health_at)}</div>
                </div>
                {connector.status === "active" && <div className="flex gap-2"><button className="btn btn-ghost btn-sm" disabled={busy === `health-${connector.connector_id}`} onClick={() => void verifyHTTP(connector)}>Verify</button><button className="btn btn-danger-soft btn-sm" disabled={busy === `revoke-${connector.connector_id}`} onClick={() => void revokeHTTP(connector)}>Revoke</button></div>}
              </div>
            </div>
          ))}
        </div>
      )}

      <details className="surface border border-soft rounded-xl p-4">
        <summary className="cursor-pointer text-[14px] font-semibold">Create signed webhook endpoint</summary>
        <div className="mt-4 grid gap-3 md:grid-cols-2">
          <label className="grid gap-1 text-[12px]">Name<input className="input" value={webhookName} onChange={event => setWebhookName(event.target.value)} /></label>
          <label className="grid gap-1 text-[12px]">Event type<input className="input font-mono" value={webhookEventType} onChange={event => setWebhookEventType(event.target.value)} /></label>
          <label className="grid gap-1 text-[12px]">Matching workflow<select className="input" value={webhookWorkflowId} onChange={event => setWebhookWorkflowId(event.target.value)}><option value="">No workflow yet</option>{workflows.map(workflow => <option key={workflow.id} value={workflow.id}>{workflow.name}</option>)}</select></label>
          <label className="grid gap-1 text-[12px]">Rate limit per minute<input className="input" type="number" min={1} max={600} value={webhookRate} onChange={event => setWebhookRate(Number(event.target.value))} /></label>
        </div>
        <div className="mt-3 flex justify-end"><button className="btn btn-accent btn-sm" disabled={busy === "create-webhook" || !webhookName.trim() || !webhookEventType.trim()} onClick={() => void createWebhook()}>{busy === "create-webhook" ? "Creating…" : "Create endpoint"}</button></div>
      </details>

      {secretReveal?.signing_secret && (
        <div role="status" className="surface rounded-xl border p-4" style={{ borderColor: "var(--warn)" }}>
          <div className="text-[13px] font-semibold">Copy the signing secret now</div>
          <p className="mt-1 text-[12px]" style={{ color: "var(--text-dim)" }}>Chronos stores only encrypted material and will not reveal this value again.</p>
          <div className="mt-3 grid gap-2">
            <div className="flex gap-2"><code className="min-w-0 flex-1 overflow-x-auto rounded-md bg-black/10 px-2 py-1.5 text-[11.5px]">{secretReveal.url}</code><button className="btn btn-ghost btn-sm" onClick={() => void copy(secretReveal.url, "Webhook URL")}>Copy URL</button></div>
            <div className="flex gap-2"><code className="min-w-0 flex-1 overflow-x-auto rounded-md bg-black/10 px-2 py-1.5 text-[11.5px]">{secretReveal.signing_secret}</code><button className="btn btn-ghost btn-sm" onClick={() => void copy(secretReveal.signing_secret || "", "Signing secret")}>Copy secret</button></div>
          </div>
        </div>
      )}

      {webhooks.length > 0 && (
        <div className="surface border border-soft rounded-xl overflow-hidden">
          {webhooks.map(endpoint => (
            <div key={endpoint.id} className="border-b hairline px-4 py-3 last:border-b-0">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-[14px]">{endpoint.name}</span><span className={`tag ${endpoint.status === "active" ? "tag-ok" : "tag-warn"}`}>{endpoint.status}</span><span className="tag">{endpoint.event_type}</span></div>
                  <div className="mt-1 truncate font-mono text-[11px]" style={{ color: "var(--text-dim)" }}>{endpoint.url}</div>
                  <div className="mt-1 text-[11px]" style={{ color: "var(--text-faint)" }}>HMAC SHA-256 · 5 minute timestamp window · {endpoint.rate_limit_per_minute}/min · secret {endpoint.secret_fingerprint} · last delivery {formatTime(endpoint.last_received_at)}</div>
                </div>
                <div className="flex flex-wrap gap-2"><button className="btn btn-ghost btn-sm" onClick={() => void copy(endpoint.url, "Webhook URL")}>Copy URL</button>{endpoint.status === "active" && <button className="btn btn-ghost btn-sm" disabled={busy === `test-${endpoint.id}`} onClick={() => void testWebhook(endpoint)}>Test</button>}<button className="btn btn-ghost btn-sm" disabled={busy === `rotate-${endpoint.id}`} onClick={() => void rotateWebhook(endpoint)}>Rotate secret</button><button className={endpoint.status === "active" ? "btn btn-danger-soft btn-sm" : "btn btn-ghost btn-sm"} disabled={busy === `status-${endpoint.id}`} onClick={() => void updateWebhookStatus(endpoint)}>{endpoint.status === "active" ? "Disable" : "Enable"}</button></div>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="rounded-lg border border-soft px-3 py-2 text-[11.5px]" style={{ color: "var(--text-dim)" }}>
        Sign <code>{"${timestamp}.${rawBody}"}</code> with HMAC SHA-256 and send <code>X-Chronos-Timestamp</code>, <code>X-Chronos-Signature: v1=&lt;hex&gt;</code>, and a stable <code>X-Chronos-Event-ID</code>. Payloads must be JSON objects no larger than 1 MiB.
      </div>
    </section>
  );
}
