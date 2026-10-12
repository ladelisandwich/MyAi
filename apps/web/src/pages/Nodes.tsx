import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { GPUInfo, NodeSummary } from "../api/types";
import { Badge, ConfirmButton, CopyButton, Empty, ErrorAlert, Meter, Modal, Spinner, StatusDot, useLoad, useToast } from "../components/ui";
import { useLiveRefresh } from "../lib/events";
import { mb, relTime } from "../lib/format";

const MODES = [
  { value: "performance", label: "Performance", hint: "Prioritise AI work" },
  { value: "balanced", label: "Balanced", hint: "Share with normal use; back off while games use the GPU" },
  { value: "background", label: "Background", hint: "Low-priority work; chat only as last resort" },
  { value: "paused", label: "Paused", hint: "No new jobs; stays connected" },
];

interface Pairing { code: string; expires_at: string; coordinator_url: string; tls_fingerprint?: string | null; join_command: string }

function PairDialog({ onClose }: { onClose: () => void }) {
  const [label, setLabel] = useState("");
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [left, setLeft] = useState(0);
  useEffect(() => {
    if (!pairing) return;
    const tick = () => setLeft(Math.max(0, Math.round((new Date(pairing.expires_at).getTime() - Date.now()) / 1000)));
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, [pairing]);
  const create = async () => {
    try {
      setPairing(await api.post<Pairing>("/api/v1/nodes/pairing", { label }));
      setError(null);
    } catch (e) { setError(e); }
  };
  return (
    <Modal title="Add a computer" onClose={onClose} wide>
      {!pairing ? (
        <div className="stack">
          <p>Install HomeMesh on the other computer (see INSTALLATION.md), then join it with a one-time pairing code. Codes expire after 10 minutes and work once.</p>
          <label className="field">Label <span className="hint">optional, e.g. “Living-room PC”</span>
            <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={100} />
          </label>
          <ErrorAlert error={error} />
          <div className="modal-actions"><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" onClick={() => void create()}>Create pairing code</button></div>
        </div>
      ) : (
        <div className="stack">
          <div className="row"><span className="pairing-code">{pairing.code}</span><CopyButton text={pairing.code} className="btn sm" /></div>
          <div className={left < 60 ? "alert warn small" : "muted small"}>{left > 0 ? `Expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "Expired - create a new code."}</div>
          <p className="small">On the new computer, run:</p>
          <div className="row nowrap"><pre className="block" style={{ flex: 1, whiteSpace: "pre-wrap" }}>{pairing.join_command}</pre><CopyButton text={pairing.join_command} className="btn sm" /></div>
          {pairing.tls_fingerprint ? (
            <div className="alert info small">The worker will only trust this coordinator if its certificate matches the fingerprint <span className="mono">{pairing.tls_fingerprint}</span>. Check it matches what this page shows - that protects the pairing against impersonation on your network.</div>
          ) : (
            <div className="alert warn small">This coordinator runs without TLS (local-only HTTP). Workers on other computers can only join after enabling TLS: run <code>homemesh tls init</code> on this computer and restart. See SECURITY.md.</div>
          )}
          <div className="modal-actions"><button className="btn primary" onClick={onClose}>Done</button></div>
        </div>
      )}
    </Modal>
  );
}

function GpuView({ g, live }: { g: GPUInfo; live?: { utilization_percent?: number | null; vram_used_mb?: number | null; vram_total_mb?: number | null; temperature_c?: number | null } }) {
  const used = live?.vram_used_mb ?? g.vram_used_mb;
  const total = live?.vram_total_mb ?? g.vram_total_mb;
  const util = live?.utilization_percent ?? g.utilization_percent;
  const temp = live?.temperature_c ?? g.temperature_c;
  return (
    <div className="gpu stack" style={{ gap: 6 }}>
      <div className="row"><strong className="small">{g.name}</strong><Badge>{g.vendor}</Badge>{g.integrated && <Badge>integrated</Badge>}{g.ignored && <Badge kind="warn">ignored for scheduling</Badge>}{(g.compute_apis ?? []).map((a) => <Badge key={a} kind="accent">{a}</Badge>)}</div>
      {total ? <Meter label="VRAM" value={used} max={total} detail={used != null ? `${mb(used)} used of ${mb(total)}` : `${mb(total)} total (usage not reported)`} />
        : <div className="small muted">VRAM: {g.unavailable?.vram_total_mb ?? "not reported"}</div>}
      <div className="row small muted">
        <span>Utilisation: {util != null ? `${Math.round(util)}%` : "not reported"}</span>
        <span>· Temperature: {temp != null ? `${Math.round(temp)}°C` : "not reported"}</span>
        {g.driver_version && <span>· Driver {g.driver_version}</span>}
        <span className="faint">· via {g.source}</span>
      </div>
      {Object.entries(g.unavailable ?? {}).filter(([k]) => k !== "vram_total_mb").map(([k, v]) => <div key={k} className="faint tiny">{k}: {v}</div>)}
    </div>
  );
}

function NodeCard({ n, reload }: { n: NodeSummary; reload: () => void }) {
  const toast = useToast();
  const [renaming, setRenaming] = useState(false);
  const hw = n.hardware;
  const t = n.telemetry;
  const patch = async (body: Record<string, unknown>) => {
    try { await api.patch(`/api/v1/nodes/${n.id}`, body); reload(); } catch (e) { toast.error(e); }
  };
  return (
    <div className="card node-card stack">
      <div className="card-head">
        <StatusDot status={n.online ? (n.mode === "paused" ? "warn" : "ok") : "off"} />
        {renaming ? (
          <input autoFocus defaultValue={n.name} style={{ maxWidth: 260 }} aria-label="Computer name"
            onBlur={(e) => { setRenaming(false); if (e.target.value.trim() && e.target.value !== n.name) void patch({ name: e.target.value.trim() }); }}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
        ) : <h2 style={{ cursor: "pointer" }} title="Click to rename" onClick={() => setRenaming(true)}>{n.name}</h2>}
        {n.embedded && <Badge kind="accent">coordinator machine</Badge>}
        <Badge kind={n.online ? "ok" : undefined}>{n.online ? "online" : "offline"}</Badge>
        <span className="spacer" />
        <span className="faint small">{n.online ? `heartbeat ${n.heartbeat_age_s ?? "?"}s ago` : `last seen ${relTime(n.last_seen_at)}`}</span>
      </div>
      {!n.online && n.last_disconnect_reason && <div className="alert warn small">Disconnected: {n.last_disconnect_reason}</div>}
      <div className="grid cols-2" style={{ gap: 12 }}>
        <dl className="kv">
          <dt>System</dt><dd>{hw?.os.distro_name ?? n.os ?? "—"} {hw?.os.immutable && <Badge>immutable</Badge>} {hw?.os.in_container && <Badge>container</Badge>}</dd>
          <dt>Architecture</dt><dd>{n.arch ?? "—"}</dd>
          <dt>CPU</dt><dd>{hw?.cpu.model ?? "—"} {hw?.cpu.logical_cores ? `(${hw.cpu.physical_cores ?? "?"} cores / ${hw.cpu.logical_cores} threads)` : ""}</dd>
          <dt>RAM</dt><dd>{mb(hw?.memory.total_mb)} total{t?.ram_available_mb != null ? ` · ${mb(t.ram_available_mb)} free` : ""}</dd>
          <dt>HomeMesh</dt><dd>{n.software_version ?? "—"} (protocol v{n.protocol_version ?? "?"})</dd>
          <dt>Jobs</dt><dd>{n.running_jobs.length} running · max {n.limits?.max_concurrent_jobs ?? "?"} · CPU inference {n.limits?.allow_cpu_inference ? "allowed" : "off"}</dd>
          <dt>Accepts</dt><dd>{n.task_types.join(", ") || "—"}</dd>
          <dt>Folders</dt><dd>{n.allowed_roots.length ? n.allowed_roots.join(", ") : <span className="faint">none authorised</span>}</dd>
          <dt>Tools</dt><dd>{n.allowed_tools.join(", ") || "—"}</dd>
        </dl>
        <div className="stack">
          {n.online && t && <>
            <Meter label="CPU" value={t.cpu_percent} />
            <Meter label="RAM in use" value={t.ram_total_mb && t.ram_available_mb != null ? t.ram_total_mb - t.ram_available_mb : null} max={t.ram_total_mb ?? 0} />
          </>}
          {(hw?.gpus ?? []).length === 0 ? <div className="small muted">No GPU detected - CPU inference, embeddings and indexing still work.</div>
            : hw!.gpus.map((g) => <GpuView key={g.index} g={g} live={t?.gpus?.find((x) => x.index === g.index)} />)}
          {t?.external_gpu_busy && <div className="alert warn small">Another application is using the GPU heavily; this computer is backing off according to its mode.</div>}
          {(hw?.warnings ?? []).filter((w) => !w.startsWith("No GPU detected")).map((w) => <div key={w} className="faint tiny">⚠ {w}</div>)}
        </div>
      </div>
      <div>
        <div className="small muted" style={{ marginBottom: 4 }}>Inference backends</div>
        <div className="row">
          {(n.backends ?? []).length === 0 ? <span className="faint small">none</span> : (n.backends ?? []).map((b) => (
            <Badge key={b.name} kind={b.ok ? "ok" : "danger"} title={b.error ?? b.url}>{b.name}{b.version ? ` ${b.version}` : ""}{b.mock ? " (mock)" : ""}{b.ok ? "" : " · unreachable"}</Badge>
          ))}
        </div>
        {(n.backends ?? []).filter((b) => !b.ok).map((b) => <div key={b.name} className="faint tiny" style={{ marginTop: 4 }}>{b.name}: {b.error}</div>)}
      </div>
      <div>
        <div className="small muted" style={{ marginBottom: 4 }}>Models ({n.models.length})</div>
        <div className="row">{n.models.map((m) => <Badge key={`${m.backend}/${m.name}`} kind={m.loaded ? "accent" : undefined} title={m.capabilities.join(", ")}>{m.mock ? "[MOCK] " : ""}{m.name}{m.loaded ? " · loaded" : ""}</Badge>)}</div>
      </div>
      <hr style={{ margin: "4px 0" }} />
      <div className="row">
        <label className="field" style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>Mode
          <select value={n.mode} onChange={(e) => void patch({ mode: e.target.value })} disabled={n.allow_remote_mode_change === false} style={{ width: "auto" }}>
            {MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
          </select>
        </label>
        <span className="faint small">{MODES.find((m) => m.value === n.mode)?.hint}</span>
        <span className="spacer" />
        {n.mode !== "paused" ? <button className="btn sm" onClick={() => void patch({ mode: "paused" })} disabled={n.allow_remote_mode_change === false}>Pause</button>
          : <button className="btn sm" onClick={() => void patch({ mode: "balanced" })} disabled={n.allow_remote_mode_change === false}>Resume</button>}
        <label className="small muted row nowrap" title="Preference when several computers could run a job">Priority
          <input type="range" min={-10} max={10} value={n.weight} onChange={(e) => void patch({ weight: Number(e.target.value) })} style={{ width: 100 }} />
          <span className="mono">{n.weight}</span>
        </label>
        {n.online && <button className="btn sm" onClick={async () => { try { await api.post(`/api/v1/nodes/${n.id}/refresh`); toast.ok("Model list refresh requested"); } catch (e) { toast.error(e); } }}>Refresh models</button>}
        {!n.embedded && n.online && <button className="btn sm" onClick={async () => { try { await api.post(`/api/v1/nodes/${n.id}/rotate-credential`); toast.ok("Credential rotation sent"); } catch (e) { toast.error(e); } }}>Rotate credential</button>}
        {!n.embedded && <ConfirmButton message={`Remove ${n.name}? Its credential is revoked immediately; to add it again you need a new pairing code.`} onConfirm={async () => { try { await api.post(`/api/v1/nodes/${n.id}/revoke`); reload(); } catch (e) { toast.error(e); } }}>Remove</ConfirmButton>}
      </div>
      {n.allow_remote_mode_change === false && <div className="faint tiny">This computer's owner disabled remote mode changes (worker.allow_remote_mode_change = false).</div>}
    </div>
  );
}

export default function NodesPage() {
  const { data, error, loading, reload } = useLoad(() => api.get<NodeSummary[]>("/api/v1/nodes"));
  const [pairOpen, setPairOpen] = useState(false);
  useLiveRefresh(["node.connected", "node.disconnected", "node.updated", "node.models", "node.enrolled", "node.revoked"], reload, 300);
  useLiveRefresh(["node.telemetry"], reload, 3000);
  return (
    <div className="page">
      <div className="page-header">
        <h1>Computers</h1><span className="spacer" />
        <button className="btn primary" onClick={() => setPairOpen(true)}>＋ Add a computer</button>
      </div>
      <p className="muted small">Every computer running a HomeMesh worker contributes what it has: a GPU for chat, a CPU for embeddings or document indexing. Workers connect out to the coordinator, so they need no open ports.</p>
      <ErrorAlert error={error} />
      {loading && !data ? <Spinner /> : data?.length === 0 ? <Empty icon="🖥️" title="No computers yet">Start HomeMesh in standalone mode or add a worker.</Empty>
        : data?.map((n) => <NodeCard key={n.id} n={n} reload={() => void reload()} />)}
      {pairOpen && <PairDialog onClose={() => { setPairOpen(false); void reload(); }} />}
    </div>
  );
}
