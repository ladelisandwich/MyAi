import { useState } from "react";
import { api } from "../api/client";
import type { ModelGroup } from "../api/types";
import { Badge, ConfirmButton, Empty, ErrorAlert, MockBadge, Spinner, useLoad, useToast } from "../components/ui";
import { useEvents, useLiveRefresh } from "../lib/events";
import { bytes } from "../lib/format";

interface ModelsResponse {
  models: ModelGroup[];
  roles: Record<string, string | null>;
  backends: { node_id: string; node_name: string; online: boolean; name: string; type: string; ok: boolean; version?: string; error?: string; supports_pull?: boolean; supports_load_unload?: boolean; mock?: boolean }[];
  role_capabilities: Record<string, string>;
  downloads_note: string;
}

const ROLE_LABELS: Record<string, string> = {
  chat: "Primary assistant", reasoning: "Reasoning", coding: "Coding", fast: "Fast responses", embedding: "Embeddings (search)",
  vision: "Vision (images)", stt: "Speech-to-text", tts: "Text-to-speech",
};
const CAP_KIND: Record<string, string> = { tools: "violet", vision: "accent", embedding: "ok", thinking: "warn" };

function PullForm({ data, onStarted }: { data: ModelsResponse; onStarted: (jobId: string) => void }) {
  const pullable = data.backends.filter((b) => b.online && b.ok && b.supports_pull);
  const [target, setTarget] = useState(pullable[0] ? `${pullable[0].node_id}|${pullable[0].name}` : "");
  const [model, setModel] = useState("");
  const toast = useToast();
  if (pullable.length === 0) return <div className="muted small">No online backend supports downloading models (Ollama does).</div>;
  return (
    <form className="row" onSubmit={async (e) => {
      e.preventDefault();
      const [node_id, backend] = target.split("|");
      try {
        const r = await api.post<{ job_id: string }>("/api/v1/models/manage", { action: "pull", node_id, backend, model: model.trim() });
        onStarted(r.job_id);
        setModel("");
        toast.info(`Download of ${model} started`);
      } catch (err) { toast.error(err); }
    }}>
      <input placeholder="Model name, e.g. llama3.2:3b or nomic-embed-text" value={model} onChange={(e) => setModel(e.target.value)} style={{ flex: 2, minWidth: 220 }} required aria-label="Model name" />
      <select value={target} onChange={(e) => setTarget(e.target.value)} style={{ flex: 1, minWidth: 160 }} aria-label="Download to">
        {pullable.map((b) => <option key={`${b.node_id}|${b.name}`} value={`${b.node_id}|${b.name}`}>{b.node_name} · {b.name}</option>)}
      </select>
      <button className="btn primary">Download</button>
    </form>
  );
}

export default function ModelsPage() {
  const { data, error, loading, reload } = useLoad(() => api.get<ModelsResponse>("/api/v1/models"));
  const [progress, setProgress] = useState<Record<string, { progress?: number; message?: string }>>({});
  const [roleDraft, setRoleDraft] = useState<Record<string, string | null> | null>(null);
  const toast = useToast();
  const { subscribe } = useEvents();
  useLiveRefresh(["node.models", "node.connected", "node.disconnected", "settings.updated"], reload, 500);

  const watchJob = (jobId: string) => {
    const unsub = subscribe(["job.progress", "job.updated"], (d) => {
      if (d.job_id !== jobId) return;
      if (d.progress !== undefined || d.message) setProgress((p) => ({ ...p, [jobId]: { progress: d.progress as number, message: d.message as string } }));
      if (d.status === "completed" || d.status === "failed" || d.status === "cancelled") {
        setProgress((p) => { const n = { ...p }; delete n[jobId]; return n; });
        if (d.status === "failed") toast.error(new Error("Model operation failed - see Jobs for details"));
        else if (d.status === "completed") toast.ok("Model operation finished");
        unsub();
        setTimeout(() => void reload(), 1500);
      }
    });
    setProgress((p) => ({ ...p, [jobId]: { message: "starting…" } }));
  };

  const manage = async (action: string, node_id: string, backend: string, model: string) => {
    try {
      const r = await api.post<{ job_id: string }>("/api/v1/models/manage", { action, node_id, backend, model });
      watchJob(r.job_id);
    } catch (e) { toast.error(e); }
  };

  if (loading && !data) return <div className="page"><Spinner /></div>;
  if (!data) return <div className="page"><ErrorAlert error={error} /></div>;
  const roles = roleDraft ?? data.roles;
  const compatible = (role: string) => {
    const cap = data.role_capabilities[role];
    return data.models.filter((m) => m.available && (!cap || m.capabilities.includes(cap)));
  };

  return (
    <div className="page">
      <div className="page-header"><h1>Models</h1><span className="spacer" /><button className="btn sm" onClick={() => void reload()}>Refresh</button></div>
      <ErrorAlert error={error} />
      <div className="grid cols-2">
        <div className="card">
          <div className="card-head"><h2>Model roles</h2><span className="spacer" />
            {roleDraft && <button className="btn primary sm" onClick={async () => {
              try {
                const r = await api.put<{ warnings: string[] }>("/api/v1/models/roles", { roles: roleDraft });
                r.warnings.forEach((w) => toast.info(w));
                setRoleDraft(null);
                toast.ok("Model roles saved");
                void reload();
              } catch (e) { toast.error(e); }
            }}>Save roles</button>}
          </div>
          <p className="small muted">Which model HomeMesh uses for each job. “Automatic” lets the scheduler pick a suitable model on the best available computer. Only models with the required capability are listed.</p>
          <div className="stack">
            {Object.keys(ROLE_LABELS).map((role) => {
              const options = compatible(role);
              const unavailable = role === "stt" || role === "tts";
              return (
                <label key={role} className="field">{ROLE_LABELS[role]}
                  <select value={roles[role] ?? ""} disabled={unavailable} onChange={(e) => setRoleDraft({ ...roles, [role]: e.target.value || null })}>
                    <option value="">{unavailable ? "Not available yet (voice is planned)" : "Automatic"}</option>
                    {options.map((m) => <option key={m.name} value={m.name}>{m.mock ? "[MOCK] " : ""}{m.name}</option>)}
                  </select>
                </label>
              );
            })}
          </div>
        </div>
        <div className="card">
          <div className="card-head"><h2>Inference backends</h2></div>
          {data.backends.length === 0 ? <div className="muted small">No backends reported.</div> : (
            <table className="table"><thead><tr><th>Computer</th><th>Backend</th><th>Status</th></tr></thead><tbody>
              {data.backends.map((b) => (
                <tr key={`${b.node_id}/${b.name}`}>
                  <td>{b.node_name}</td>
                  <td>{b.name} <span className="faint small">({b.type}{b.version && b.version !== b.type ? ` ${b.version}` : ""})</span> {b.mock && <MockBadge />}</td>
                  <td>{!b.online ? <Badge>offline</Badge> : b.ok ? <Badge kind="ok">ok</Badge> : <span><Badge kind="danger">unreachable</Badge><div className="faint tiny">{b.error}</div></span>}</td>
                </tr>
              ))}
            </tbody></table>
          )}
          <hr />
          <h3>Download a model</h3>
          <p className="faint small">{data.downloads_note}</p>
          <PullForm data={data} onStarted={watchJob} />
          {Object.entries(progress).map(([id, p]) => (
            <div key={id} className="progress-line small" style={{ marginTop: 8 }}>
              <span className="truncate" style={{ maxWidth: 260 }}>{p.message ?? "working…"}</span>
              <div className="meter"><span style={{ width: `${Math.round((p.progress ?? 0) * 100)}%` }} /></div>
              <span className="mono">{p.progress != null ? `${Math.round(p.progress * 100)}%` : ""}</span>
            </div>
          ))}
        </div>
      </div>
      <h2 style={{ marginTop: 24 }}>Installed models</h2>
      {data.models.length === 0 ? <Empty icon="🧠" title="No models found">Install a model with Ollama on any computer (e.g. <code>ollama pull llama3.2</code>) or download one above.</Empty> : (
        <div className="stack">
          {data.models.map((m) => (
            <div key={m.name} className="card">
              <div className="card-head">
                <h3>{m.name}</h3>
                {m.mock && <MockBadge />}
                {m.remote && <Badge kind="warn">cloud</Badge>}
                {!m.available && <Badge>unavailable</Badge>}
                {m.roles.map((r) => <Badge key={r} kind="accent">{ROLE_LABELS[r] ?? r}</Badge>)}
                <span className="spacer" />
                <div className="capabilities">{m.capabilities.map((c) => <Badge key={c} kind={CAP_KIND[c]}>{c}</Badge>)}</div>
              </div>
              <div className="row small muted">
                {m.family && <span>{m.family}</span>}
                {m.parameter_size && <span>· {m.parameter_size} parameters</span>}
                {m.quantization && <span>· {m.quantization}</span>}
                {m.size_bytes != null && <span>· {bytes(m.size_bytes)} on disk</span>}
                {m.context_length && <span>· context {m.context_length.toLocaleString()}</span>}
                {m.embedding_length && <span>· {m.embedding_length}-dim embeddings</span>}
                <span className="faint">· capabilities {m.capability_source === "backend" ? "reported by backend" : m.capability_source}</span>
              </div>
              {m.warnings.map((w) => <div key={w} className="alert warn small" style={{ marginTop: 8 }}>{w}</div>)}
              <table className="table" style={{ marginTop: 8 }}><thead><tr><th>Computer</th><th>Backend</th><th>State</th><th>Memory</th><th></th></tr></thead><tbody>
                {m.instances.map((i) => (
                  <tr key={`${i.node_id}/${i.backend}`}>
                    <td>{i.node_name} {!i.online && <Badge>offline</Badge>}</td>
                    <td>{i.backend}</td>
                    <td>{i.loaded ? <Badge kind="accent">loaded</Badge> : <span className="faint">not loaded</span>}</td>
                    <td className="small">{i.loaded ? `${bytes(i.loaded_size_bytes)} resident${i.loaded_vram_bytes ? ` (${bytes(i.loaded_vram_bytes)} VRAM)` : " (CPU/RAM)"}` : i.estimated_memory_bytes ? `~${bytes(i.estimated_memory_bytes)} est. when loaded` : "unknown"}</td>
                    <td className="nowrap">
                      {i.online && i.backend_type !== "openai_compat" && (i.loaded
                        ? <button className="btn sm" onClick={() => void manage("unload", i.node_id, i.backend, m.name)}>Unload</button>
                        : <button className="btn sm" onClick={() => void manage("load", i.node_id, i.backend, m.name)}>Load</button>)}
                      {i.online && i.backend_type === "ollama" && <ConfirmButton message={`Delete ${m.name} from ${i.node_name}? You can download it again later.`} onConfirm={() => manage("delete", i.node_id, i.backend, m.name)}>Delete</ConfirmButton>}
                    </td>
                  </tr>
                ))}
              </tbody></table>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
