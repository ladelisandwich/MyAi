import { useState } from "react";
import { api } from "../api/client";
import type { Approval, Job } from "../api/types";
import { Badge, Empty, ErrorAlert, MockBadge, Modal, Spinner, useLoad, useToast } from "../components/ui";
import { useLiveRefresh } from "../lib/events";
import { dateTime, duration, ms, num, relTime } from "../lib/format";
import { navigate, type Route } from "../lib/router";

const STATUS_KIND: Record<string, string> = { completed: "ok", running: "accent", dispatched: "accent", queued: "", failed: "danger", cancelled: "warn", interrupted: "warn" };
const FILTERS: [string, string][] = [["active", "queued,dispatched,running"], ["completed", "completed"], ["failed", "failed,interrupted"], ["cancelled", "cancelled"], ["all", ""]];

function JobDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const { data, error, loading, reload } = useLoad(() => api.get<Job>(`/api/v1/jobs/${id}`), [id]);
  const toast = useToast();
  useLiveRefresh(["job.updated", "job.progress"], reload, 500);
  const active = data && ["queued", "dispatched", "running"].includes(data.status);
  return (
    <Modal title={`Job ${id}`} onClose={onClose} wide>
      <ErrorAlert error={error} />
      {loading || !data ? <Spinner /> : (
        <div className="stack">
          <div className="row"><Badge>{data.type}</Badge><Badge kind={STATUS_KIND[data.status]}>{data.status}</Badge><Badge>{data.priority}</Badge>{data.mock && <MockBadge />}
            <span className="spacer" />
            {active && <button className="btn danger sm" onClick={async () => { try { await api.post(`/api/v1/jobs/${id}/cancel`); toast.ok("Cancellation requested"); void reload(); } catch (e) { toast.error(e); } }}>Cancel job</button>}
          </div>
          {data.error && <div className="alert danger small"><strong>{data.error_code}</strong>: {data.error}</div>}
          {data.progress != null && active && <div className="progress-line small"><span>{data.progress_message}</span><div className="meter"><span style={{ width: `${Math.round(data.progress * 100)}%` }} /></div></div>}
          <dl className="kv">
            <dt>Computer</dt><dd>{data.node_name ?? "—"}</dd>
            <dt>Model</dt><dd>{data.model ?? "—"} {data.backend ? `(${data.backend})` : ""}</dd>
            <dt>Created</dt><dd>{dateTime(data.created_at)}</dd>
            <dt>Started</dt><dd>{dateTime(data.started_at)}</dd>
            <dt>Finished</dt><dd>{dateTime(data.finished_at)}</dd>
            <dt>Duration</dt><dd>{duration(data.started_at ?? data.created_at, data.finished_at)}</dd>
            <dt>Attempts</dt><dd>{data.attempts}/{data.max_attempts}</dd>
            {data.conversation_id && <><dt>Conversation</dt><dd><a href={`#/chat/${data.conversation_id}`} onClick={onClose}>open</a></dd></>}
          </dl>
          {data.metrics_json && (
            <div><h3>Metrics</h3><dl className="kv">
              {data.metrics_json.ttft_ms != null && <><dt>Time to first token</dt><dd>{ms(data.metrics_json.ttft_ms)}</dd></>}
              {data.metrics_json.tokens_per_second != null && <><dt>Generation speed</dt><dd>{num(data.metrics_json.tokens_per_second, 1)} tokens/s</dd></>}
              {data.metrics_json.load_ms != null && <><dt>Model load</dt><dd>{ms(data.metrics_json.load_ms)}</dd></>}
              {data.metrics_json.prompt_tokens != null && <><dt>Prompt tokens</dt><dd>{data.metrics_json.prompt_tokens}</dd></>}
              {data.metrics_json.completion_tokens != null && <><dt>Output tokens</dt><dd>{data.metrics_json.completion_tokens}</dd></>}
              {data.metrics_json.total_ms != null && <><dt>Total (worker)</dt><dd>{ms(data.metrics_json.total_ms)}</dd></>}
              {data.metrics_json.dispatch_overhead_ms != null && <><dt>Dispatch overhead</dt><dd>{ms(data.metrics_json.dispatch_overhead_ms)}</dd></>}
            </dl></div>
          )}
          {data.decision_json && (
            <div><h3>Why this computer?</h3>
              {data.decision_json.chosen && <div className="small">Chosen: <strong>{data.decision_json.chosen.node_name}</strong> · {data.decision_json.chosen.model ?? "—"} · {data.decision_json.chosen.placement} (score {data.decision_json.chosen.score}) {data.decision_json.chosen.notes.join("; ")}</div>}
              {data.decision_json.candidates.length > 1 && <details><summary>{data.decision_json.candidates.length} candidates</summary><ul className="small">{data.decision_json.candidates.map((c, i) => <li key={i}>{c.node_name} · {c.model} · {c.placement} · score {c.score}</li>)}</ul></details>}
              {data.decision_json.rejections.length > 0 && <details><summary>{data.decision_json.rejections.length} rejected</summary><ul className="small">{data.decision_json.rejections.map((r, i) => <li key={i}><strong>{r.node_name}</strong>{r.model ? ` / ${r.model}` : ""}: {r.message} <span className="faint">({r.code})</span></li>)}</ul></details>}
            </div>
          )}
          <div><h3>Execution log</h3>
            <div className="table-wrap"><table className="table"><tbody>
              {(data.events ?? []).map((e) => (
                <tr key={e.id}><td className="nowrap faint small">{new Date(e.ts).toLocaleTimeString()}</td><td><Badge kind={e.level === "error" ? "danger" : e.level === "warning" ? "warn" : undefined}>{e.kind}</Badge></td><td className="small break">{e.message}</td></tr>
              ))}
            </tbody></table></div>
          </div>
          <details><summary>Payload and result (summarised)</summary><pre className="block">{JSON.stringify({ payload: data.payload_json, result: data.result_json }, null, 2)}</pre></details>
        </div>
      )}
    </Modal>
  );
}

export default function JobsPage({ route }: { route: Route }) {
  const [filter, setFilter] = useState("active");
  const [type, setType] = useState("");
  const statuses = FILTERS.find(([k]) => k === filter)?.[1] ?? "";
  const { data, error, loading, reload } = useLoad(async () => {
    const params = new URLSearchParams();
    if (statuses) params.set("status", statuses);
    if (type) params.set("type", type);
    const [jobs, approvals, executions] = await Promise.all([
      api.get<{ items: Job[]; counts: Record<string, number> }>(`/api/v1/jobs?${params}`),
      api.get<Approval[]>("/api/v1/tools/approvals?status=pending"),
      api.get<{ id: string; tool_name: string; status: string; started_at: string; error?: string | null; side_effect: string }[]>("/api/v1/tools/executions?limit=20"),
    ]);
    return { ...jobs, approvals, executions };
  }, [statuses, type]);
  const toast = useToast();
  useLiveRefresh(["job.created", "job.updated", "approval.requested", "approval.decided", "approval.expired"], reload, 500);
  const detailId = route.rest[0];

  return (
    <div className="page">
      <div className="page-header"><h1>Jobs &amp; Activity</h1><span className="spacer" /><button className="btn sm" onClick={() => void reload()}>Refresh</button></div>
      <ErrorAlert error={error} />
      {data && data.approvals.length > 0 && (
        <div className="card">
          <h2>Waiting for your approval</h2>
          {data.approvals.map((a) => (
            <div key={a.id} className="list-item row">
              <div style={{ flex: 1 }}><strong>{a.tool_name}</strong> <Badge kind="warn">{a.side_effect}</Badge> <span className="faint small">requested {relTime(a.requested_at)}</span>
                <pre className="block" style={{ marginTop: 4 }}>{JSON.stringify(a.arguments_json, null, 2)}</pre></div>
              <button className="btn ok solid sm" onClick={async () => { try { await api.post(`/api/v1/tools/approvals/${a.id}`, { approved: true }); void reload(); } catch (e) { toast.error(e); } }}>Approve</button>
              <button className="btn danger sm" onClick={async () => { try { await api.post(`/api/v1/tools/approvals/${a.id}`, { approved: false }); void reload(); } catch (e) { toast.error(e); } }}>Deny</button>
            </div>
          ))}
        </div>
      )}
      <div className="card">
        <div className="tabs">
          {FILTERS.map(([k]) => <button key={k} className={filter === k ? "active" : ""} onClick={() => setFilter(k)}>{k[0].toUpperCase() + k.slice(1)}</button>)}
          <span className="spacer" />
          <select value={type} onChange={(e) => setType(e.target.value)} style={{ width: "auto", minHeight: 30, marginBottom: 4 }} aria-label="Job type">
            <option value="">All types</option>{["chat", "embed", "knowledge.scan", "tool.exec", "model.manage"].map((t) => <option key={t}>{t}</option>)}
          </select>
        </div>
        {data && <div className="row small faint" style={{ marginBottom: 8 }}>{Object.entries(data.counts).map(([k, v]) => <span key={k}>{k}: {v}</span>)}</div>}
        {loading && !data ? <Spinner /> : data?.items.length === 0 ? <Empty title="No jobs in this view" /> : (
          <div className="table-wrap"><table className="table">
            <thead><tr><th>Type</th><th>Status</th><th>Computer</th><th>Model</th><th>Created</th><th>Duration</th><th>Details</th></tr></thead>
            <tbody>{data?.items.map((j) => (
              <tr key={j.id} className="clickable" onClick={() => navigate(`/jobs/${j.id}`)}>
                <td><Badge>{j.type}</Badge></td>
                <td><Badge kind={STATUS_KIND[j.status]}>{j.status}</Badge>{j.mock && <> <MockBadge /></>}</td>
                <td>{j.node_name ?? "—"}</td>
                <td className="truncate" style={{ maxWidth: 180 }}>{j.model ?? "—"}</td>
                <td className="nowrap faint">{relTime(j.created_at)}</td>
                <td className="nowrap">{duration(j.started_at ?? j.created_at, j.finished_at)}</td>
                <td className="small break">{j.error ? <span className="faint">{j.error_code}: {j.error.slice(0, 80)}</span> : j.progress_message ?? (j.metrics_json?.tokens_per_second ? `${num(j.metrics_json.tokens_per_second, 1)} tok/s` : "")}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
      {data && data.executions.length > 0 && (
        <div className="card">
          <h2>Recent tool executions</h2>
          <table className="table"><tbody>{data.executions.map((x) => (
            <tr key={x.id}><td>{x.tool_name}</td><td><Badge kind={x.status === "succeeded" ? "ok" : x.status === "running" ? "accent" : "danger"}>{x.status}</Badge></td><td><Badge>{x.side_effect}</Badge></td><td className="faint small">{relTime(x.started_at)}</td><td className="small faint break">{x.error}</td></tr>
          ))}</tbody></table>
        </div>
      )}
      {detailId && <JobDetail id={detailId} onClose={() => navigate("/jobs")} />}
    </div>
  );
}
