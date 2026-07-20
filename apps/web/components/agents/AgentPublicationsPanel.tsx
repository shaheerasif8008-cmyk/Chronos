"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../lib/api";

type AgentSummary = { id: string; name: string };
type Binding = {
  id: string;
  provider: "slack" | "teams";
  connector_id?: string | null;
  external_tenant_id?: string | null;
  external_channel_id: string;
  display_name?: string | null;
  status: string;
  provider_status: string;
};
type Publication = {
  id: string;
  target: string;
  display_name: string;
  external_channel_id?: string | null;
  binding_id?: string | null;
  status: string;
  provider_status: string;
  last_error_code?: string | null;
  last_inbound_at?: string | null;
  last_outbound_at?: string | null;
};
type Connector = { id: string; name: string; connected?: boolean; connector_id?: string | null };

const TARGETS = ["slack", "teams", "email", "web", "api"] as const;

function friendly(value: string) {
  return value.replaceAll("_", " ");
}

function endpoint(publication: Publication) {
  const base = `/agents/publications/${publication.id}`;
  if (publication.target === "slack") return `${base}/slack/events`;
  if (publication.target === "teams") return `${base}/teams/events`;
  if (publication.target === "email") return `${base}/email/events`;
  if (publication.target === "web") return `${base}/embed/messages`;
  return `${base}/inbound`;
}

export default function AgentPublicationsPanel({ agent, canPublish }: { agent: AgentSummary; canPublish: boolean }) {
  const [target, setTarget] = useState<(typeof TARGETS)[number]>("slack");
  const [publications, setPublications] = useState<Publication[]>([]);
  const [bindings, setBindings] = useState<Binding[]>([]);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [bindingId, setBindingId] = useState("");
  const [tenantId, setTenantId] = useState("");
  const [destination, setDestination] = useState("");
  const [origin, setOrigin] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [oneTimeSecret, setOneTimeSecret] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!canPublish) return;
    try {
      const [publicationRows, bindingRows, connectorRows] = await Promise.all([
        apiFetch(`/agents/${agent.id}/publications`).then(response => response.json()) as Promise<Publication[]>,
        apiFetch("/agents/publication-bindings").then(response => response.json()) as Promise<Binding[]>,
        apiFetch("/connectors").then(response => response.json()) as Promise<Connector[]>,
      ]);
      setPublications(publicationRows);
      setBindings(bindingRows);
      setConnectors(connectorRows);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load publication status");
    }
  }, [agent.id, canPublish]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    setBindingId("");
    setDestination("");
    setTenantId("");
    setOrigin("");
    setOneTimeSecret(null);
  }, [target, agent.id]);

  const providerBindings = useMemo(
    () => bindings.filter(binding => binding.provider === target && binding.status === "active"),
    [bindings, target],
  );
  const connector = connectors.find(item => item.id === target);

  async function connectProvider() {
    setBusy("connect"); setError(null); setNotice(null);
    try {
      const data = await apiFetch(`/connectors/${target}/oauth-start`, { method: "POST" }).then(response => response.json()) as { url: string };
      window.location.href = data.url;
    } catch (err) {
      setError(err instanceof Error ? err.message : `Unable to connect ${target}`);
      setBusy(null);
    }
  }

  async function bindChannel() {
    if (!(target === "slack" || target === "teams") || !connector?.connector_id || !tenantId.trim() || !destination.trim()) {
      setError("Connect the provider, then enter its workspace or tenant ID and channel ID.");
      return;
    }
    setBusy("bind"); setError(null); setNotice(null);
    try {
      const binding = await apiFetch("/agents/publication-bindings", {
        method: "POST",
        body: JSON.stringify({
          provider: target,
          connector_id: connector.connector_id,
          external_tenant_id: tenantId.trim(),
          external_channel_id: destination.trim(),
          display_name: destination.trim(),
        }),
      }).then(response => response.json()) as Binding;
      await load();
      setBindingId(binding.id);
      setNotice(`${target === "slack" ? "Slack" : "Teams"} channel bound to this organization.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to bind channel");
    } finally {
      setBusy(null);
    }
  }

  async function revokeBinding(binding: Binding) {
    if (!window.confirm(`Revoke the ${binding.provider} binding for ${binding.display_name || binding.external_channel_id}? Active publications on it will be unpublished.`)) return;
    setBusy(binding.id); setError(null); setNotice(null);
    try {
      await apiFetch(`/agents/publication-bindings/${binding.id}`, { method: "DELETE" });
      if (bindingId === binding.id) setBindingId("");
      await load();
      setNotice("Channel binding revoked and its active publications unpublished.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to revoke channel binding");
    } finally {
      setBusy(null);
    }
  }

  async function publish() {
    if ((target === "slack" || target === "teams") && !bindingId) {
      setError("Select an authorized channel binding first.");
      return;
    }
    if (target === "email" && !destination.trim()) {
      setError("Enter the recipient email address.");
      return;
    }
    if (target === "web" && !origin.trim()) {
      setError("Enter the HTTPS origin that may host this embed.");
      return;
    }
    setBusy("publish"); setError(null); setNotice(null); setOneTimeSecret(null);
    try {
      const publication = await apiFetch(`/agents/${agent.id}/publications`, {
        method: "POST",
        body: JSON.stringify({
          target,
          display_name: agent.name,
          binding_id: bindingId || undefined,
          external_channel_id: target === "email" ? destination.trim() : undefined,
          config: {
            reply_mode: "threaded",
            ...(target === "web" ? { allowed_origins: [origin.trim()] } : {}),
          },
        }),
      }).then(response => response.json()) as Publication & { plaintext_secret?: string };
      if (publication.plaintext_secret) setOneTimeSecret(publication.plaintext_secret);
      await load();
      setNotice(publication.provider_status === "ready" ? "Publication is active." : `Published with a configuration warning: ${friendly(publication.last_error_code || "provider degraded")}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to publish agent");
    } finally {
      setBusy(null);
    }
  }

  async function lifecycle(publication: Publication, action: "unpublish" | "rotate" | "revoke") {
    if (action === "revoke" && !window.confirm(`Permanently revoke this ${publication.target} publication?`)) return;
    setBusy(publication.id); setError(null); setNotice(null); setOneTimeSecret(null);
    try {
      const updated = await apiFetch(`/agents/publications/${publication.id}/lifecycle`, {
        method: "POST",
        body: JSON.stringify({ action }),
      }).then(response => response.json()) as Publication & { plaintext_secret?: string };
      if (updated.plaintext_secret) setOneTimeSecret(updated.plaintext_secret);
      await load();
      setNotice(`${friendly(action)} complete.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Unable to ${action} publication`);
    } finally {
      setBusy(null);
    }
  }

  if (!canPublish) {
    return <p className="mt-2 text-[12px]" style={{ color: "var(--text-dim)" }}>External publication is restricted to administrators and owners.</p>;
  }

  return (
    <div className="mt-2 space-y-3" data-testid="agent-publication-admin">
      {(error || notice) && <div role={error ? "alert" : "status"} className="rounded-md border border-soft px-2.5 py-2 text-[11.5px]" style={{ color: error ? "var(--danger)" : "var(--text-dim)" }}>{error || notice}</div>}
      <label className="block text-[11.5px]" style={{ color: "var(--text-dim)" }}>
        Destination
        <select className="input mt-1" value={target} onChange={event => setTarget(event.target.value as (typeof TARGETS)[number])}>
          {TARGETS.map(item => <option value={item} key={item}>{item === "api" ? "Scoped API" : item[0].toUpperCase() + item.slice(1)}</option>)}
        </select>
      </label>

      {(target === "slack" || target === "teams") && (
        <div className="rounded-md border border-soft p-2.5 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <span className="text-[11.5px]" style={{ color: "var(--text-dim)" }}>{connector?.connected ? "Authorized account connected" : "Provider authorization required"}</span>
            {!connector?.connected && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void connectProvider()} disabled={busy !== null}>Connect</button>}
          </div>
          {providerBindings.length > 0 && <select aria-label={`Authorized ${target} channel`} className="input" value={bindingId} onChange={event => setBindingId(event.target.value)}><option value="">Select channel</option>{providerBindings.map(binding => <option value={binding.id} key={binding.id}>{binding.display_name || binding.external_channel_id} · {binding.external_tenant_id}</option>)}</select>}
          {providerBindings.map(binding => <div key={`binding-${binding.id}`} className="flex items-center gap-2 rounded-md px-2 py-1.5" style={{ background: "var(--surface-2)" }}><span className="min-w-0 flex-1 truncate text-[11px]" style={{ color: "var(--text-dim)" }}>{binding.display_name || binding.external_channel_id} · {binding.external_tenant_id}</span><button type="button" className="btn btn-ghost btn-sm" style={{ color: "var(--danger)" }} onClick={() => void revokeBinding(binding)} disabled={busy !== null}>Revoke</button></div>)}
          {connector?.connected && (
            <>
              <input aria-label={`${target} workspace or tenant ID`} className="input" value={tenantId} onChange={event => setTenantId(event.target.value)} placeholder={target === "slack" ? "Workspace ID (T…)" : "Team ID"} />
              <input aria-label={`${target} channel ID`} className="input" value={destination} onChange={event => setDestination(event.target.value)} placeholder="Channel ID" />
              <button type="button" className="btn btn-secondary btn-sm w-full" onClick={() => void bindChannel()} disabled={busy !== null}>Authorize channel</button>
            </>
          )}
        </div>
      )}
      {target === "email" && <><input aria-label="Inbound agent email address" type="email" className="input" value={destination} onChange={event => setDestination(event.target.value)} placeholder="support-agent@inbound.company.com" /><p className="text-[11px] leading-5" style={{ color: "var(--text-dim)" }}>Use the exact address configured in SendGrid Inbound Parse. Replies are sent to the verified sender after policy approval.</p></>}
      {target === "web" && <input aria-label="Allowed embed origin" type="url" className="input" value={origin} onChange={event => setOrigin(event.target.value)} placeholder="https://support.company.com" />}
      {target === "api" && <p className="text-[11.5px] leading-5" style={{ color: "var(--text-dim)" }}>Calls require a non-expired organization API key with write scope. Keys are managed in Settings.</p>}
      <button type="button" className="btn btn-accent btn-sm w-full" onClick={() => void publish()} disabled={busy !== null}>{busy === "publish" ? "Publishing…" : "Publish agent"}</button>

      {oneTimeSecret && <div role="status" className="rounded-md border px-2.5 py-2" style={{ borderColor: "var(--warn)", background: "var(--warn-soft)" }}><div className="text-[11.5px] font-medium">Copy this embed secret now</div><code className="mt-1 block overflow-x-auto text-[11px]">{oneTimeSecret}</code><button type="button" className="btn btn-secondary btn-sm mt-2" onClick={() => void navigator.clipboard.writeText(oneTimeSecret)}>Copy secret</button></div>}

      {publications.length > 0 && <div className="space-y-2"><div className="text-[11.5px] font-medium">Active and previous publications</div>{publications.map(publication => <div key={publication.id} className="rounded-md border border-soft p-2.5"><div className="flex items-center gap-2"><span className="text-[12px] font-medium capitalize">{publication.target}</span><span className="rounded-full px-2 py-0.5 text-[10.5px]" style={{ background: publication.provider_status === "ready" ? "var(--ok-soft)" : "var(--warn-soft)", color: publication.provider_status === "ready" ? "var(--ok)" : "var(--warn)" }}>{friendly(publication.provider_status)}</span><span className="ml-auto text-[10.5px]" style={{ color: "var(--text-faint)" }}>{friendly(publication.status)}</span></div>{publication.last_error_code && <p className="mt-1 text-[11px]" style={{ color: "var(--warn)" }}>{friendly(publication.last_error_code)}</p>}<div className="mt-2 flex items-center gap-2 rounded-md px-2 py-1.5" style={{ background: "var(--surface-2)" }}><code className="min-w-0 flex-1 truncate text-[10.5px]">{endpoint(publication)}</code><button type="button" className="btn btn-ghost btn-sm" onClick={() => void navigator.clipboard.writeText(endpoint(publication))}>Copy</button></div>{(publication.last_inbound_at || publication.last_outbound_at) && <p className="mt-1.5 text-[10.5px]" style={{ color: "var(--text-faint)" }}>{publication.last_inbound_at ? `Inbound ${new Date(publication.last_inbound_at).toLocaleString()}` : ""}{publication.last_inbound_at && publication.last_outbound_at ? " · " : ""}{publication.last_outbound_at ? `Outbound ${new Date(publication.last_outbound_at).toLocaleString()}` : ""}</p>}<div className="mt-2 flex flex-wrap gap-1.5"><button type="button" className="btn btn-secondary btn-sm" onClick={() => void lifecycle(publication, "unpublish")} disabled={busy !== null || publication.status !== "active"}>Unpublish</button><button type="button" className="btn btn-secondary btn-sm" onClick={() => void lifecycle(publication, "rotate")} disabled={busy !== null || publication.status === "revoked"}>Rotate</button><button type="button" className="btn btn-ghost btn-sm" style={{ color: "var(--danger)" }} onClick={() => void lifecycle(publication, "revoke")} disabled={busy !== null || publication.status === "revoked"}>Revoke</button></div></div>)}</div>}
    </div>
  );
}
