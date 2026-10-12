import { useEffect } from "react";
import { api } from "../api/client";
import type { Job, Telemetry } from "../api/types";
import { Badge, Empty, ErrorAlert, Meter, Spinner, StatusDot, useLoad } from "../components/ui";
import { useLiveRefresh } from "../lib/events";
import { duration, mb, relTime, uptime } from "../lib/format";

interface Status {
  coordinator: { version: string; protocol_version: number; node_name: string; uptime_s: number; tls_mode: string; url: string; mock_enabled: boolean; cloud_enabled: boolean; tls_fingerprint?: string | null; database: { integrity: string; schema_version: number; latest_schema_version: number } };
  nodes: { total: number; online: number; items: { id: string; name: string; online: boolean; mode: string; telemetry?: Telemetry | null; running_jobs: string[]; embedded: boolean; heartbeat_age_s?: number | null }[] };
  models: { total: number; available: number };
  jobs: Record<string, number>;
  active_jobs: Job[];
  recent_errors: { id: string; type: string; node_id?: string; model?: string; error_code?: string; error?: string; finished_at?: string }[];
  warnings: string[];
}

export default function DashboardPage() {
  const { data, error, loading, reload } = useLoad(() => api.get<Status>("/api/v1/system/status"));
  useLiveRefresh(["node.connected", "node.disconnected", "job.updated", "job.created", "node.models"], reload, 600);
  useEffect(() => {
    const t = setInterval(() => void reload(), 10000);
    return () => clearInterval(t);
  }, [reload]);

  if (loading && !data) return <div className="page"><Spinner label="Loading status…" /></div>;
  if (!data) return <div className="page"><ErrorAlert error={error} /></div>;
  const c = data.coordinator;
  return (
    <div className="page">
      <div className="page-header"><h1>Dashboard</h1><span className="spacer" /><button className="btn sm" onClick={() => void reload()}>Refresh</button></div>
      <ErrorAlert error={error} />
      {data.warnings.map((w) => <div key={w} className="alert warn">{w}</div>)}
      <div className="grid cols-4" style={{ marginTop: data.warnings.length ? 14 : 0 }}>
        <div className="card stat"><span className="value">{data.nodes.online}/{data.nodes.total}</span><span className="label">computers online</span></div>
        <div className="card stat"><span className="value">{data.models.available}</span><span className="label">models available</span></div>
        <div className="card stat"><span className="value">{(data.jobs.running ?? 0) + (data.jobs.dispatched ?? 0)}</span><span className="label">jobs running · {data.jobs.queued ?? 0} queued</span></div>
        <div className="card stat"><span className="value">{data.jobs.failed ?? 0}</span><span className="label">failed jobs (retained)</span></div>
      </div>
      <div className="grid cols-2" style={{ marginTop: 14 }}>
        <div className="card">
          <div className="card-head"><h2><StatusDot status={c.database.integrity === "ok" ? "ok" : "danger"} /> Coordinator</h2></div>
          <dl className="kv">
            <dt>Computer</dt><dd>{c.node_name}</dd>
            <dt>Address</dt><dd className="mono">{c.url}</dd>
            <dt>Version</dt><dd>{c.version} (protocol v{c.protocol_version})</dd>
            <dt>Uptime</dt><dd>{uptime(c.uptime_s)}</dd>
            <dt>Transport</dt><dd>{c.tls_mode === "off" ? <span>HTTP (local only) <Badge>no TLS</Badge></span> : <span>TLS ({c.tls_mode})</span>}</dd>
            {c.tls_fingerprint && <><dt>CA fingerprint</dt><dd className="mono tiny">{c.tls_fingerprint}</dd></>}
            <dt>Database</dt><dd>integrity {c.database.integrity} · schema v{c.database.schema_version}</dd>
            <dt>Cloud models</dt><dd>{c.cloud_enabled ? <Badge kind="warn">enabled</Badge> : <Badge kind="ok">disabled</Badge>}</dd>
            {c.mock_enabled && <><dt>Mock backend</dt><dd><Badge kind="mock">MOCK ENABLED</Badge></dd></>}
          </dl>
        </div>
        <div className="card">
          <div className="card-head"><h2>Computers</h2><span className="spacer" /><a href="#/nodes" className="small">Manage →</a></div>
          {data.nodes.items.length === 0 ? <Empty title="No computers registered" /> : data.nodes.items.map((n) => {
            const t = n.telemetry;
            const gpu = t?.gpus?.[0];
            return (
              <div key={n.id} className="list-item stack" style={{ gap: 6 }}>
                <div className="row">
                  <StatusDot status={n.online ? (n.mode === "paused" ? "warn" : "ok") : "off"} />
                  <strong>{n.name}</strong>
                  {n.embedded && <Badge>this computer</Badge>}
                  <Badge kind={n.mode === "paused" ? "warn" : undefined}>{n.mode}</Badge>
                  <span className="spacer" />
                  <span className="faint small">{n.online ? `${n.running_jobs.length} job(s)` : "offline"}</span>
                </div>
                {n.online && t && (
                  <div className="grid cols-3" style={{ gap: 8 }}>
                    <Meter label="CPU" value={t.cpu_percent} />
                    <Meter label="RAM" value={t.ram_total_mb && t.ram_available_mb != null ? t.ram_total_mb - t.ram_available_mb : null} max={t.ram_total_mb ?? 0} detail={t.ram_available_mb != null ? `${mb(t.ram_available_mb)} free` : undefined} />
                    {gpu ? <Meter label="GPU VRAM" value={gpu.vram_used_mb} max={gpu.vram_total_mb ?? 0} detail={gpu.vram_used_mb != null ? `${mb(gpu.vram_used_mb)} / ${mb(gpu.vram_total_mb)}` : undefined} />
                      : <div className="metric"><div className="label"><span>GPU</span><span>none reported</span></div></div>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
        <div className="card">
          <div className="card-head"><h2>Active jobs</h2><span className="spacer" /><a href="#/jobs" className="small">All jobs →</a></div>
          {data.active_jobs.length === 0 ? <div className="muted small">Nothing running.</div> : (
            <table className="table"><tbody>
              {data.active_jobs.map((j) => (
                <tr key={j.id} className="clickable" onClick={() => (window.location.hash = `#/jobs/${j.id}`)}>
                  <td><Badge>{j.type}</Badge></td>
                  <td>{j.status}</td>
                  <td className="truncate">{j.model ?? "—"}</td>
                  <td className="faint">{duration(j.created_at, null)}</td>
                </tr>
              ))}
            </tbody></table>
          )}
        </div>
        <div className="card">
          <div className="card-head"><h2>Recent errors</h2></div>
          {data.recent_errors.length === 0 ? <div className="muted small">No recent failures. 🎉</div> : data.recent_errors.map((e) => (
            <div key={e.id} className="list-item small">
              <div className="row"><Badge kind="danger">{e.error_code ?? "failed"}</Badge><Badge>{e.type}</Badge><span className="faint">{relTime(e.finished_at)}</span></div>
              <div className="break muted" style={{ marginTop: 4 }}>{e.error}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
