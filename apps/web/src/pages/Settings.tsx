import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api/client";
import type { FeatureFlag, SettingsMap, ToolInfo, User } from "../api/types";
import { Badge, ConfirmButton, CopyButton, ErrorAlert, Spinner, useLoad, useToast } from "../components/ui";
import { useLiveRefresh } from "../lib/events";
import { bytes, dateTime, relTime } from "../lib/format";
import { getTheme, setTheme, type Theme } from "../lib/theme";

type Field =
  | { key: string; label: string; type: "text" | "textarea"; hint?: string; max?: number }
  | { key: string; label: string; type: "number"; hint?: string; min?: number; max?: number; step?: number; nullable?: boolean }
  | { key: string; label: string; type: "bool"; hint?: string }
  | { key: string; label: string; type: "select"; options: string[]; hint?: string };

const SECTIONS: { id: string; title: string; description: string; fields: Field[] }[] = [
  { id: "assistant", title: "Assistant", description: "Identity and style of your assistant (the same on every device).", fields: [
    { key: "name", label: "Assistant name", type: "text", max: 60 },
    { key: "response_style", label: "Response style", type: "select", options: ["concise", "balanced", "detailed"] },
    { key: "custom_instructions", label: "Custom instructions", type: "textarea", max: 4000, hint: "Always followed unless they conflict with HomeMesh's safety rules." },
  ] },
  { id: "user_profile", title: "About you", description: "Optional context the assistant may use.", fields: [
    { key: "preferred_name", label: "Preferred name", type: "text", max: 60 },
    { key: "about", label: "About you", type: "textarea", max: 2000 },
  ] },
  { id: "generation", title: "Inference", description: "Default generation parameters (a model's own limits always apply).", fields: [
    { key: "temperature", label: "Temperature", type: "number", min: 0, max: 2, step: 0.1, nullable: true, hint: "Lower is more focused, higher is more creative." },
    { key: "max_output_tokens", label: "Max response tokens", type: "number", min: 16, max: 32768 },
    { key: "num_ctx", label: "Context window (tokens)", type: "number", min: 512, max: 1048576, nullable: true, hint: "Empty = automatic (8192 or the model's maximum, whichever is smaller). Larger windows need more memory." },
  ] },
  { id: "memory", title: "Memory", description: "How long-term memory is used.", fields: [
    { key: "enabled", label: "Use memories in conversations", type: "bool" },
    { key: "retrieval_limit", label: "Memories per message", type: "number", min: 0, max: 30 },
    { key: "detect_explicit", label: "Save when I say “remember that …”", type: "bool" },
    { key: "auto_extract", label: "Suggest memories automatically (each needs approval)", type: "bool" },
    { key: "default_retention_days", label: "Expire new memories after (days)", type: "number", min: 1, nullable: true, hint: "Empty = keep until you delete them." },
  ] },
  { id: "knowledge", title: "Documents", description: "Retrieval from indexed folders.", fields: [
    { key: "enabled", label: "Search documents during chat", type: "bool" },
    { key: "retrieval_limit", label: "Excerpts per message", type: "number", min: 0, max: 20 },
  ] },
  { id: "tools", title: "Tools", description: "Tool use by tool-capable models.", fields: [
    { key: "enabled", label: "Allow tools", type: "bool" },
    { key: "max_rounds", label: "Max tool rounds per answer", type: "number", min: 1, max: 10 },
    { key: "approval_timeout_s", label: "Approval timeout (seconds)", type: "number", min: 10, max: 3600 },
  ] },
  { id: "retention", title: "Data retention", description: "Automatic clean-up of operational records (conversations and memories are kept until you delete them).", fields: [
    { key: "jobs_days", label: "Keep finished jobs (days)", type: "number", min: 1 },
    { key: "audit_days", label: "Keep audit log (days)", type: "number", min: 7 },
  ] },
];

function SectionForm({ section, values, onSaved }: { section: (typeof SECTIONS)[number]; values: Record<string, unknown>; onSaved: () => void }) {
  const [draft, setDraft] = useState<Record<string, unknown>>(values);
  const [error, setError] = useState<unknown>(null);
  const toast = useToast();
  useEffect(() => setDraft(values), [values]);
  const dirty = JSON.stringify(draft) !== JSON.stringify(values);
  const set = (k: string, v: unknown) => setDraft((d) => ({ ...d, [k]: v }));
  return (
    <div className="card">
      <div className="card-head"><h2>{section.title}</h2><span className="spacer" />
        {dirty && <><button className="btn sm" onClick={() => setDraft(values)}>Reset</button>
          <button className="btn primary sm" onClick={async () => {
            try { await api.patch(`/api/v1/settings/${section.id}`, draft); setError(null); toast.ok(`${section.title} saved`); onSaved(); } catch (e) { setError(e); }
          }}>Save</button></>}
      </div>
      <p className="small muted">{section.description}</p>
      <div className="form-grid">
        {section.fields.map((f) => {
          const v = draft[f.key];
          if (f.type === "bool") return <label key={f.key} className="check"><input type="checkbox" checked={Boolean(v)} onChange={(e) => set(f.key, e.target.checked)} />{f.label}</label>;
          if (f.type === "select") return <label key={f.key} className="field">{f.label}<select value={String(v ?? "")} onChange={(e) => set(f.key, e.target.value)}>{f.options.map((o) => <option key={o}>{o}</option>)}</select></label>;
          if (f.type === "textarea") return <label key={f.key} className="field" style={{ gridColumn: "1 / -1" }}>{f.label}<textarea rows={3} maxLength={f.max} value={String(v ?? "")} onChange={(e) => set(f.key, e.target.value)} />{f.hint && <span className="hint">{f.hint}</span>}</label>;
          if (f.type === "number") return <label key={f.key} className="field">{f.label}<input type="number" min={f.min} max={f.max} step={f.step ?? 1} value={v === null || v === undefined ? "" : String(v)} placeholder={f.nullable ? "automatic" : undefined}
            onChange={(e) => set(f.key, e.target.value === "" ? (f.nullable ? null : f.min ?? 0) : Number(e.target.value))} />{f.hint && <span className="hint">{f.hint}</span>}</label>;
          return <label key={f.key} className="field">{f.label}<input value={String(v ?? "")} maxLength={f.max} onChange={(e) => set(f.key, e.target.value)} />{f.hint && <span className="hint">{f.hint}</span>}</label>;
        })}
      </div>
      <ErrorAlert error={error} />
    </div>
  );
}

function ToolPolicies() {
  const { data, error, reload } = useLoad(() => api.get<ToolInfo[]>("/api/v1/tools"));
  const toast = useToast();
  return (
    <div className="card">
      <h2>Tool permissions</h2>
      <p className="small muted">Tools are only offered to models that support tool calling. Tools that change anything always ask for your approval. There is no unrestricted shell tool. Node tools only run on computers that allow them in their own <code>worker.allowed_tools</code>.</p>
      <ErrorAlert error={error} />
      {!data ? <Spinner /> : (
        <div className="table-wrap"><table className="table"><thead><tr><th>Tool</th><th>Effect</th><th>Runs on</th><th>Enabled</th><th>Approval</th></tr></thead><tbody>
          {data.map((t) => (
            <tr key={t.name}>
              <td><strong>{t.name}</strong><div className="faint tiny">{t.description}</div></td>
              <td><Badge kind={t.side_effect === "none" || t.side_effect === "read" ? undefined : "warn"}>{t.side_effect}</Badge></td>
              <td className="small">{t.location === "coordinator" ? "coordinator" : t.available_on.length ? t.available_on.join(", ") : <span className="faint">no computer allows it</span>}</td>
              <td><input type="checkbox" checked={t.enabled} aria-label={`Enable ${t.name}`} onChange={async (e) => { try { await api.put(`/api/v1/tools/${t.name}/policy`, { enabled: e.target.checked, approval: t.approval }); void reload(); } catch (err) { toast.error(err); } }} /></td>
              <td>{t.requires_approval ? <span className="small">always required</span> : (
                <select value={t.approval} style={{ width: "auto" }} aria-label={`Approval for ${t.name}`} onChange={async (e) => { try { await api.put(`/api/v1/tools/${t.name}/policy`, { enabled: t.enabled, approval: e.target.value }); void reload(); } catch (err) { toast.error(err); } }}>
                  <option value="default">not needed (read-only)</option><option value="always">ask every time</option>
                </select>)}</td>
            </tr>
          ))}
        </tbody></table></div>
      )}
    </div>
  );
}

function Security({ user }: { user: User }) {
  const toast = useToast();
  const [cur, setCur] = useState("");
  const [nw, setNw] = useState("");
  const [tokName, setTokName] = useState("");
  const [tokScopes, setTokScopes] = useState("chat");
  const [newToken, setNewToken] = useState<string | null>(null);
  const sessions = useLoad(() => api.get<{ id: string; created_at: string; last_seen_at: string; user_agent?: string; ip?: string; current: boolean }[]>("/api/v1/auth/sessions"));
  const tokens = useLoad(() => api.get<{ id: string; name: string; scopes: string[]; created_at: string; expires_at?: string; last_used_at?: string }[]>("/api/v1/auth/tokens"));
  return (
    <div className="card stack">
      <h2>Security</h2>
      <div className="small muted">Signed in as <strong>{user.username}</strong> ({user.role}).</div>
      <form className="row" onSubmit={async (e) => {
        e.preventDefault();
        try { await api.post("/api/v1/auth/password", { current_password: cur, new_password: nw }); setCur(""); setNw(""); toast.ok("Password changed; other sessions were signed out"); void sessions.reload(); } catch (err) { toast.error(err); }
      }}>
        <input type="password" placeholder="Current password" value={cur} onChange={(e) => setCur(e.target.value)} required autoComplete="current-password" style={{ flex: 1 }} aria-label="Current password" />
        <input type="password" placeholder="New password (10+ characters)" value={nw} onChange={(e) => setNw(e.target.value)} required minLength={10} autoComplete="new-password" style={{ flex: 1 }} aria-label="New password" />
        <button className="btn">Change password</button>
      </form>
      <h3>Signed-in devices</h3>
      {sessions.data?.map((s) => (
        <div key={s.id} className="row list-item small">
          <span className="truncate" style={{ flex: 1 }} title={s.user_agent}>{s.user_agent?.slice(0, 80) ?? "unknown device"} {s.current && <Badge kind="accent">this device</Badge>}</span>
          <span className="faint">{s.ip} · active {relTime(s.last_seen_at)}</span>
          {!s.current && <button className="btn sm" onClick={async () => { try { await api.del(`/api/v1/auth/sessions/${s.id}`); void sessions.reload(); } catch (e) { toast.error(e); } }}>Sign out</button>}
        </div>
      ))}
      <h3>API tokens</h3>
      <p className="small muted">For scripts or the CLI on another computer (<code>homemesh --url … --token …</code>). Scopes: read (view), chat (conversations & memory), admin (everything).</p>
      <form className="row" onSubmit={async (e) => {
        e.preventDefault();
        try { const r = await api.post<{ token: string }>("/api/v1/auth/tokens", { name: tokName, scopes: [tokScopes], ttl_days: 90 }); setNewToken(r.token); setTokName(""); void tokens.reload(); } catch (err) { toast.error(err); }
      }}>
        <input placeholder="Token name" value={tokName} onChange={(e) => setTokName(e.target.value)} required style={{ flex: 2 }} aria-label="Token name" />
        <select value={tokScopes} onChange={(e) => setTokScopes(e.target.value)} style={{ flex: 1 }} aria-label="Scope"><option>read</option><option>chat</option><option>admin</option></select>
        <button className="btn">Create (90 days)</button>
      </form>
      {newToken && <div className="alert ok small">Copy this token now - it will not be shown again: <span className="mono break">{newToken}</span> <CopyButton text={newToken} className="btn sm" /></div>}
      {tokens.data?.map((t) => (
        <div key={t.id} className="row list-item small">
          <strong>{t.name}</strong>{t.scopes.map((s) => <Badge key={s}>{s}</Badge>)}
          <span className="faint">created {relTime(t.created_at)}{t.expires_at ? ` · expires ${dateTime(t.expires_at)}` : ""}{t.last_used_at ? ` · used ${relTime(t.last_used_at)}` : ""}</span>
          <span className="spacer" />
          <ConfirmButton message={`Revoke token “${t.name}”?`} onConfirm={async () => { try { await api.del(`/api/v1/auth/tokens/${t.id}`); void tokens.reload(); } catch (e) { toast.error(e); } }}>Revoke</ConfirmButton>
        </div>
      ))}
    </div>
  );
}

function Backups() {
  const { data, error, reload } = useLoad(() => api.get<{ backups: { name: string; size: number; created_at: string; encrypted: boolean }[]; directory: string }>("/api/v1/backups"));
  const [pass, setPass] = useState("");
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  return (
    <div className="card stack">
      <h2>Backup &amp; restore</h2>
      <p className="small muted">Backups contain your conversations, memories, settings, node registry and document index (not model files - those can be downloaded again). A passphrase encrypts the backup with AES-256-GCM. Restoring is done on the coordinator with <code>homemesh restore &lt;file&gt;</code> while HomeMesh is stopped; your current data is kept aside, never deleted.</p>
      <ErrorAlert error={error} />
      <form className="row" onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try { const r = await api.post<{ name: string }>("/api/v1/backups", { passphrase: pass || null }); toast.ok(`Backup ${r.name} created`); setPass(""); void reload(); } catch (err) { toast.error(err); } finally { setBusy(false); }
      }}>
        <input type="password" placeholder="Passphrase (optional, recommended)" value={pass} onChange={(e) => setPass(e.target.value)} style={{ flex: 1 }} autoComplete="new-password" aria-label="Backup passphrase" />
        <button className="btn primary" disabled={busy}>{busy ? "Creating…" : "Create backup now"}</button>
      </form>
      {data && <div className="faint small">Stored in <span className="mono">{data.directory}</span></div>}
      {data?.backups.map((b) => (
        <div key={b.name} className="row list-item small">
          <span className="mono">{b.name}</span>{b.encrypted ? <Badge kind="ok">encrypted</Badge> : <Badge kind="warn">not encrypted</Badge>}
          <span className="faint">{bytes(b.size)} · {dateTime(b.created_at)}</span><span className="spacer" />
          <a className="btn sm" href={`/api/v1/backups/${encodeURIComponent(b.name)}`}>Download</a>
        </div>
      ))}
    </div>
  );
}

function Features({ features, machine }: { features: Record<string, FeatureFlag>; machine: ReactNode }) {
  return (
    <div className="card stack">
      <h2>Optional features</h2>
      <table className="table"><tbody>
        {Object.entries(features).map(([k, f]) => (
          <tr key={k}><td><strong>{k.replace(/_/g, " ")}</strong></td><td>{f.available ? <Badge kind="ok">available</Badge> : <Badge>unavailable</Badge>}</td><td className="small muted">{f.reason}</td></tr>
        ))}
      </tbody></table>
      {machine}
    </div>
  );
}

export default function SettingsPage({ user }: { user: User }) {
  const { data, error, loading, reload } = useLoad(() => api.get<{ settings: SettingsMap; features: Record<string, FeatureFlag> }>("/api/v1/settings"));
  const [theme, setThemeState] = useState<Theme>(getTheme());
  const [showMachine, setShowMachine] = useState(false);
  const machine = useLoad(async () => (showMachine ? api.get<{ config: unknown; config_file: string; data_dir: string; note: string }>("/api/v1/settings/machine/config") : null), [showMachine]);
  useLiveRefresh(["settings.updated"], reload, 300);
  if (loading && !data) return <div className="page"><Spinner /></div>;
  return (
    <div className="page">
      <div className="page-header"><h1>Settings</h1></div>
      <ErrorAlert error={error} />
      <div className="card">
        <h2>Appearance</h2>
        <div className="row">{(["system", "light", "dark"] as Theme[]).map((t) => (
          <label key={t} className="check"><input type="radio" name="theme" checked={theme === t} onChange={() => { setTheme(t); setThemeState(t); }} />{t}</label>
        ))}<span className="faint small">(stored on this device only)</span></div>
      </div>
      {data && SECTIONS.map((s) => <SectionForm key={s.id} section={s} values={data.settings[s.id] ?? {}} onSaved={() => void reload()} />)}
      <ToolPolicies />
      <Security user={user} />
      <Backups />
      <div className="card"><h2>Computers &amp; pairing</h2><p className="small muted">Add or remove computers, change resource modes and limits on the <a href="#/nodes">Computers</a> page.</p></div>
      {data && <Features features={data.features} machine={
        <div>
          <button className="btn sm" onClick={() => setShowMachine((v) => !v)}>{showMachine ? "Hide" : "Show"} this coordinator's machine configuration</button>
          {showMachine && machine.data && <div style={{ marginTop: 8 }}><p className="small muted">{machine.data.note} File: <span className="mono">{machine.data.config_file}</span></p><pre className="block">{JSON.stringify(machine.data.config, null, 2)}</pre></div>}
          <ErrorAlert error={machine.error} />
        </div>} />}
    </div>
  );
}
