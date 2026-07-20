"use client";

import { type ReactNode, useCallback, useEffect, useState } from "react";

import { apiFetch } from "../../lib/api";


type Toast = { kind: "ok" | "danger"; text: string };
type Confirmation = { title: string; text: string; required?: string; action: (typed: string) => Promise<void> };
type Member = { id: string; name: string; email: string; role: string; status: string; is_self?: boolean };
type Group = { id: string; name: string; description?: string | null; members: Member[] };
type WorkspaceMember = Pick<Member, "id" | "name" | "email" | "status"> & { role: "owner" | "editor" | "viewer" };
type Workspace = {
  id: string;
  name: string;
  description?: string | null;
  status: "active" | "archived" | "deletion_pending" | "deleted";
  deletion_execute_after?: string | null;
  members: WorkspaceMember[];
};
type ApiKey = {
  id: string;
  name: string;
  key_prefix: string;
  scopes: string[];
  status: string;
  rate_limit_per_minute: number;
  expires_at?: string | null;
  last_used_at?: string | null;
  plaintext_key?: string;
};


function Panel({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return <section className="mb-8"><h2 className="text-[16px] font-semibold mb-1">{title}</h2><p className="text-[13px] mb-3" style={{ color: "var(--text-dim)" }}>{note}</p><div className="surface border border-soft rounded-xl overflow-hidden">{children}</div></section>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div className="px-4 py-4 border-b hairline last:border-b-0 sm:px-5"><div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between"><div className="min-w-0"><div className="text-[14px] font-medium">{label}</div>{hint && <div className="text-[12px] mt-0.5" style={{ color: "var(--text-dim)" }}>{hint}</div>}</div><div className="min-w-0 sm:max-w-[620px]">{children}</div></div></div>;
}

function Input({ label, value, setValue, type = "text" }: { label: string; value: string; setValue: (value: string) => void; type?: string }) {
  return <input aria-label={label} type={type} value={value} onChange={event => setValue(event.target.value)} className="surface border border-soft rounded-lg px-3 py-2 text-[14px] outline-none w-full" style={{ color: "var(--text)" }}/>;
}

function MemberSelect({ label, members, value, setValue }: { label: string; members: Member[]; value: string; setValue: (value: string) => void }) {
  return <select aria-label={label} value={value} onChange={event => setValue(event.target.value)} className="surface border border-soft rounded-lg px-3 py-2 text-[13px] outline-none"><option value="">Select member</option>{members.filter(member => member.status === "active").map(member => <option key={member.id} value={member.id}>{member.name || member.email} · {member.role}</option>)}</select>;
}

export function AdminDirectorySettings({ members, setToast, setConfirm }: { members: Member[]; setToast: (toast: Toast) => void; setConfirm: (confirmation: Confirmation | null) => void }) {
  const [groups, setGroups] = useState<Group[]>([]);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [groupName, setGroupName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [selectedMembers, setSelectedMembers] = useState<Record<string, string>>({});
  const [workspaceRoles, setWorkspaceRoles] = useState<Record<string, WorkspaceMember["role"]>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [groupRows, workspaceRows] = await Promise.all([
        apiFetch("/settings/admin-lifecycle/groups").then(response => response.json()) as Promise<Group[]>,
        apiFetch("/settings/admin-lifecycle/workspaces").then(response => response.json()) as Promise<Workspace[]>,
      ]);
      setGroups(groupRows);
      setWorkspaces(workspaceRows);
    } catch (error) {
      setToast({ kind: "danger", text: error instanceof Error ? error.message : "Organization directory could not be loaded." });
    } finally {
      setLoading(false);
    }
  }, [setToast]);

  useEffect(() => { void load(); }, [load]);

  async function mutate(key: string, path: string, init: RequestInit, success: string) {
    setBusy(key);
    try {
      await apiFetch(path, init);
      await load();
      setToast({ kind: "ok", text: success });
    } catch (error) {
      setToast({ kind: "danger", text: error instanceof Error ? error.message : "The change could not be saved." });
      throw error;
    } finally {
      setBusy("");
    }
  }

  async function createGroup() {
    await mutate("group-create", "/settings/admin-lifecycle/groups", { method: "POST", body: JSON.stringify({ name: groupName }) }, "Group created.");
    setGroupName("");
  }

  async function createWorkspace() {
    await mutate("workspace-create", "/settings/admin-lifecycle/workspaces", { method: "POST", body: JSON.stringify({ name: workspaceName }) }, "Workspace created.");
    setWorkspaceName("");
  }

  return <>
    <Panel title="Native groups" note="Group membership is tenant-scoped and independent of SCIM-managed identity groups.">
      <Field label="Create group"><div className="flex flex-col gap-2 sm:flex-row"><Input label="New group name" value={groupName} setValue={setGroupName}/><button className="btn btn-accent btn-sm" disabled={!groupName.trim() || Boolean(busy)} onClick={() => void createGroup()}>Create</button></div></Field>
      {loading && <div className="px-5 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>Loading groups…</div>}
      {!loading && groups.length === 0 && <div className="px-5 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No native groups yet.</div>}
      {groups.map(group => <Field key={group.id} label={group.name} hint={group.members.length ? `${group.members.length} active membership${group.members.length === 1 ? "" : "s"}` : "No members"}><div className="space-y-3">
        <div className="flex flex-wrap justify-end gap-2"><MemberSelect label={`Member for ${group.name}`} members={members.filter(member => !group.members.some(current => current.id === member.id))} value={selectedMembers[group.id] || ""} setValue={value => setSelectedMembers(current => ({ ...current, [group.id]: value }))}/><button className="btn btn-secondary btn-sm" disabled={!selectedMembers[group.id] || Boolean(busy)} onClick={() => void mutate(`group-add-${group.id}`, `/settings/admin-lifecycle/groups/${group.id}/members`, { method: "PUT", body: JSON.stringify({ member_id: selectedMembers[group.id] }) }, "Group membership updated.")}>Add</button><button className="btn btn-danger-soft btn-sm" disabled={Boolean(busy)} onClick={() => setConfirm({ title: `Delete ${group.name}?`, text: "This removes the native group and its memberships. It does not deactivate users.", required: group.name, action: () => mutate(`group-delete-${group.id}`, `/settings/admin-lifecycle/groups/${group.id}`, { method: "DELETE" }, "Group deleted.") })}>Delete</button></div>
        {group.members.map(member => <div key={member.id} className="flex items-center justify-between gap-3 text-[12px]"><span>{member.name || member.email} <span style={{ color: "var(--text-dim)" }}>· {member.email}</span></span><button className="btn btn-secondary btn-sm" disabled={Boolean(busy)} onClick={() => void mutate(`group-remove-${member.id}`, `/settings/admin-lifecycle/groups/${group.id}/members/${member.id}`, { method: "DELETE" }, "Group membership removed.")}>Remove</button></div>)}
      </div></Field>)}
    </Panel>

    <Panel title="Workspaces" note="Archive immediately, or schedule a retention-delayed tombstone. Active legal holds always block deletion.">
      <Field label="Create workspace"><div className="flex flex-col gap-2 sm:flex-row"><Input label="New workspace name" value={workspaceName} setValue={setWorkspaceName}/><button className="btn btn-accent btn-sm" disabled={!workspaceName.trim() || Boolean(busy)} onClick={() => void createWorkspace()}>Create</button></div></Field>
      {loading && <div className="px-5 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>Loading workspaces…</div>}
      {!loading && workspaces.length === 0 && <div className="px-5 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No native workspaces yet.</div>}
      {workspaces.map(workspace => <Field key={workspace.id} label={workspace.name} hint={`${workspace.status.replaceAll("_", " ")}${workspace.deletion_execute_after ? ` · executes after ${new Date(workspace.deletion_execute_after).toLocaleString()}` : ""}`}><div className="space-y-3">
        {workspace.status !== "deleted" && workspace.status !== "deletion_pending" && <div className="flex flex-wrap justify-end gap-2"><MemberSelect label={`Member for ${workspace.name}`} members={members} value={selectedMembers[`workspace-${workspace.id}`] || ""} setValue={value => setSelectedMembers(current => ({ ...current, [`workspace-${workspace.id}`]: value }))}/><select aria-label={`Role for ${workspace.name}`} value={workspaceRoles[workspace.id] || "viewer"} onChange={event => setWorkspaceRoles(current => ({ ...current, [workspace.id]: event.target.value as WorkspaceMember["role"] }))} className="surface border border-soft rounded-lg px-3 py-2 text-[13px]"><option value="viewer">viewer</option><option value="editor">editor</option><option value="owner">owner</option></select><button className="btn btn-secondary btn-sm" disabled={!selectedMembers[`workspace-${workspace.id}`] || Boolean(busy)} onClick={() => void mutate(`workspace-member-${workspace.id}`, `/settings/admin-lifecycle/workspaces/${workspace.id}/members`, { method: "PUT", body: JSON.stringify({ member_id: selectedMembers[`workspace-${workspace.id}`], role: workspaceRoles[workspace.id] || "viewer" }) }, "Workspace membership updated.")}>Set member</button></div>}
        <div className="flex flex-wrap justify-end gap-2">{workspace.status === "active" && <button className="btn btn-secondary btn-sm" disabled={Boolean(busy)} onClick={() => void mutate(`archive-${workspace.id}`, `/settings/admin-lifecycle/workspaces/${workspace.id}/archive`, { method: "POST" }, "Workspace archived.")}>Archive</button>}{workspace.status === "archived" && <button className="btn btn-secondary btn-sm" disabled={Boolean(busy)} onClick={() => void mutate(`restore-${workspace.id}`, `/settings/admin-lifecycle/workspaces/${workspace.id}/restore`, { method: "POST" }, "Workspace restored.")}>Restore</button>}{workspace.status === "deletion_pending" && <button className="btn btn-secondary btn-sm" disabled={Boolean(busy)} onClick={() => void mutate(`cancel-delete-${workspace.id}`, `/settings/admin-lifecycle/workspaces/${workspace.id}/deletion`, { method: "DELETE" }, "Workspace deletion cancelled; it remains archived.")}>Cancel deletion</button>}{["active", "archived"].includes(workspace.status) && <button className="btn btn-danger-soft btn-sm" disabled={Boolean(busy)} onClick={() => setConfirm({ title: `Schedule deletion of ${workspace.name}?`, text: "Access will be removed only after the configured retention delay. Evidence remains retained, and a legal hold blocks execution.", required: `DELETE ${workspace.name}`, action: typed => mutate(`delete-${workspace.id}`, `/settings/admin-lifecycle/workspaces/${workspace.id}/deletion`, { method: "POST", body: JSON.stringify({ confirmation: typed }) }, "Workspace deletion scheduled.") })}>Schedule deletion</button>}</div>
        {workspace.members.map(member => <div key={member.id} className="flex items-center justify-between gap-3 text-[12px]"><span>{member.name || member.email} <span style={{ color: "var(--text-dim)" }}>· {member.role}</span></span>{workspace.status !== "deleted" && <button className="btn btn-secondary btn-sm" disabled={Boolean(busy)} onClick={() => void mutate(`workspace-remove-${member.id}`, `/settings/admin-lifecycle/workspaces/${workspace.id}/members/${member.id}`, { method: "DELETE" }, "Workspace membership removed.")}>Remove</button>}</div>)}
      </div></Field>)}
    </Panel>
  </>;
}


export function OrganizationApiKeysSettings({ setToast, setConfirm }: { setToast: (toast: Toast) => void; setConfirm: (confirmation: Confirmation | null) => void }) {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"read" | "write" | "admin">("read");
  const [expiresAt, setExpiresAt] = useState("");
  const [rateLimit, setRateLimit] = useState("60");
  const [plaintext, setPlaintext] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    try {
      setKeys(await apiFetch("/settings/admin-lifecycle/api-keys").then(response => response.json()) as ApiKey[]);
    } catch (error) {
      setToast({ kind: "danger", text: error instanceof Error ? error.message : "API keys could not be loaded." });
    }
  }, [setToast]);
  useEffect(() => { void load(); }, [load]);

  async function create() {
    setBusy("create");
    try {
      const created = await apiFetch("/settings/admin-lifecycle/api-keys", { method: "POST", body: JSON.stringify({ name, scopes: [scope], rate_limit_per_minute: Number(rateLimit), expires_at: expiresAt ? new Date(expiresAt).toISOString() : null }) }).then(response => response.json()) as ApiKey;
      setPlaintext(created.plaintext_key || "");
      setName("");
      await load();
      setToast({ kind: "ok", text: "API key created. Copy it before dismissing the one-time value." });
    } catch (error) {
      setToast({ kind: "danger", text: error instanceof Error ? error.message : "API key could not be created." });
    } finally {
      setBusy("");
    }
  }

  async function rotate(key: ApiKey) {
    setBusy(key.id);
    try {
      const created = await apiFetch(`/settings/admin-lifecycle/api-keys/${key.id}/rotate`, { method: "POST" }).then(response => response.json()) as ApiKey;
      setPlaintext(created.plaintext_key || "");
      await load();
      setToast({ kind: "ok", text: "Old key revoked. Copy the replacement before dismissing it." });
    } finally {
      setBusy("");
    }
  }

  return <Panel title="Organization API keys" note="Keys inherit the active creator's tenant and role. Plaintext is shown once; only a peppered digest is stored.">
    {plaintext && <div role="status" className="m-4 rounded-xl border p-4" style={{ borderColor: "var(--warning)" }}><div className="text-[13px] font-semibold">Copy this key now</div><code className="block mt-2 break-all select-all text-[12px]">{plaintext}</code><div className="mt-3 flex gap-2 justify-end"><button className="btn btn-secondary btn-sm" onClick={() => void navigator.clipboard.writeText(plaintext)}>Copy</button><button className="btn btn-secondary btn-sm" onClick={() => setPlaintext("")}>I saved it</button></div></div>}
    <Field label="Create key" hint="Admin scope can only be issued by an organization owner."><div className="grid gap-2 sm:grid-cols-2"><Input label="API key name" value={name} setValue={setName}/><select aria-label="API key scope" value={scope} onChange={event => setScope(event.target.value as typeof scope)} className="surface border border-soft rounded-lg px-3 py-2 text-[13px]"><option value="read">read</option><option value="write">write</option><option value="admin">admin</option></select><Input label="API key expiry" type="datetime-local" value={expiresAt} setValue={setExpiresAt}/><Input label="API key requests per minute" type="number" value={rateLimit} setValue={setRateLimit}/><button className="btn btn-accent btn-sm sm:col-span-2" disabled={!name.trim() || busy === "create"} onClick={() => void create()}>{busy === "create" ? "Creating…" : "Create key"}</button></div></Field>
    {keys.length === 0 && <div className="px-5 py-6 text-[13px]" style={{ color: "var(--text-dim)" }}>No organization API keys.</div>}
    {keys.map(key => <Field key={key.id} label={key.name} hint={`${key.key_prefix} · ${key.scopes.join(", ")} · ${key.status}`}><div className="text-right"><div className="text-[12px] mb-2" style={{ color: "var(--text-dim)" }}>{key.last_used_at ? `Last used ${new Date(key.last_used_at).toLocaleString()}` : "Never used"} · {key.rate_limit_per_minute}/minute</div>{key.status === "active" && <div className="flex justify-end gap-2"><button className="btn btn-secondary btn-sm" disabled={Boolean(busy)} onClick={() => setConfirm({ title: `Rotate ${key.name}?`, text: "The current key is revoked as soon as the replacement is issued.", action: () => rotate(key) })}>Rotate</button><button className="btn btn-danger-soft btn-sm" disabled={Boolean(busy)} onClick={() => setConfirm({ title: `Revoke ${key.name}?`, text: "Requests using this key will fail immediately.", required: key.name, action: async () => { setBusy(key.id); try { await apiFetch(`/settings/admin-lifecycle/api-keys/${key.id}`, { method: "DELETE" }); await load(); setToast({ kind: "ok", text: "API key revoked." }); } finally { setBusy(""); } } })}>Revoke</button></div>}</div></Field>)}
  </Panel>;
}


export function OrganizationDangerSettings({ organizationName, memberId, role, members, signOut, setToast, setConfirm }: { organizationName: string; memberId: string; role: string; members: Member[]; signOut: () => void; setToast: (toast: Toast) => void; setConfirm: (confirmation: Confirmation | null) => void }) {
  const [targetMember, setTargetMember] = useState("");
  const transferRequired = `TRANSFER ${organizationName}`;
  const leaveRequired = `LEAVE ${organizationName}`;
  return <Panel title="Danger zone" note="Ownership and membership changes are transactional, tenant-scoped, and confirmation protected.">
    <Field label="Transfer ownership" hint={role === "owner" ? "The recipient becomes owner and your role becomes admin." : "Only the current organization owner can transfer ownership."}><div className="flex flex-col gap-2 sm:flex-row"><MemberSelect label="New organization owner" members={members.filter(member => member.id !== memberId)} value={targetMember} setValue={setTargetMember}/><button className="btn btn-danger-soft btn-sm" disabled={role !== "owner" || !targetMember} onClick={() => setConfirm({ title: "Transfer organization ownership?", text: `Type ${transferRequired}. This changes authority immediately.`, required: transferRequired, action: async typed => { await apiFetch("/settings/admin-lifecycle/ownership-transfer", { method: "POST", body: JSON.stringify({ target_member_id: targetMember, confirmation: typed }) }); setToast({ kind: "ok", text: "Ownership transferred. Reloading your new role…" }); window.location.reload(); } })}>Transfer</button></div></Field>
    <Field label="Leave organization" hint="Your account is deactivated and API keys you created are revoked. The last owner cannot leave."><button className="btn btn-danger-soft btn-sm" onClick={() => setConfirm({ title: `Leave ${organizationName}?`, text: `Type ${leaveRequired}. You will be signed out after the membership is deactivated.`, required: leaveRequired, action: async typed => { await apiFetch("/settings/admin-lifecycle/leave", { method: "POST", body: JSON.stringify({ confirmation: typed }) }); signOut(); } })}>Leave organization</button></Field>
  </Panel>;
}
