import { useState } from "react";
import { api } from "../api/client";
import type { Collection, SearchHit } from "../api/types";
import { Badge, ConfirmButton, Empty, ErrorAlert, Modal, Spinner, useLoad, useToast } from "../components/ui";
import { useLiveRefresh } from "../lib/events";
import { bytes, relTime } from "../lib/format";

interface ListResponse {
  collections: Collection[];
  embedding_model: string | null;
  nodes: { id: string; name: string; embedded: boolean; allowed_roots: string[] }[];
}

const STATUS_KIND: Record<string, string> = { indexed: "ok", indexing: "accent", embedding: "accent", error: "danger", pending: "" };
const DOC_KIND: Record<string, string> = { indexed: "ok", needs_ocr: "warn", unsupported: "", error: "danger", too_large: "warn", empty: "" };

function AddCollection({ data, onClose, onCreated }: { data: ListResponse; onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [nodeId, setNodeId] = useState(data.nodes.find((n) => n.embedded)?.id ?? data.nodes[0]?.id ?? "");
  const [path, setPath] = useState("");
  const [globs, setGlobs] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const node = data.nodes.find((n) => n.id === nodeId);
  return (
    <Modal title="Add a document folder" onClose={onClose} wide>
      <form className="stack" onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          await api.post("/api/v1/knowledge/collections", { name, node_id: nodeId, root_path: path.trim(), include_globs: globs.split(",").map((g) => g.trim()).filter(Boolean) });
          onCreated();
        } catch (err) { setError(err); } finally { setBusy(false); }
      }}>
        <p className="small muted">HomeMesh only reads folders you add here, never a whole disk or your entire home folder. Credential stores (SSH keys, password managers, browser profiles) are always skipped. Indexing runs on the computer that holds the files.</p>
        <div className="form-grid">
          <label className="field">Name<input value={name} onChange={(e) => setName(e.target.value)} required placeholder="e.g. Project notes" /></label>
          <label className="field">Computer
            <select value={nodeId} onChange={(e) => setNodeId(e.target.value)}>{data.nodes.map((n) => <option key={n.id} value={n.id}>{n.name}{n.embedded ? " (this coordinator)" : ""}</option>)}</select>
          </label>
        </div>
        <label className="field">Folder path on that computer
          <input value={path} onChange={(e) => setPath(e.target.value)} required placeholder={node?.embedded ? "/home/you/Documents/notes" : node?.allowed_roots[0] ?? "/path/to/folder"} className="mono" />
          {node && !node.embedded && <span className="hint">{node.allowed_roots.length ? `This computer allows: ${node.allowed_roots.join(", ")}` : "This computer has not authorised any folders yet. On it, run: homemesh worker allow-path <folder>"}</span>}
        </label>
        <label className="field">Only include <span className="hint">optional comma-separated patterns, e.g. *.md, *.pdf</span>
          <input value={globs} onChange={(e) => setGlobs(e.target.value)} placeholder="all supported files" />
        </label>
        <div className="faint small">Supported: text, Markdown, PDF (text layer), code, JSON, CSV/TSV. Scanned/image-only PDFs are marked “needs OCR”.</div>
        <ErrorAlert error={error} />
        <div className="modal-actions"><button type="button" className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={busy}>{busy ? "Adding…" : "Add and index"}</button></div>
      </form>
    </Modal>
  );
}

function CollectionDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const { data, error, loading } = useLoad(() => api.get<Collection>(`/api/v1/knowledge/collections/${id}`), [id]);
  return (
    <Modal title={data?.name ?? "Collection"} onClose={onClose} wide>
      <ErrorAlert error={error} />
      {loading || !data ? <Spinner /> : (
        <div className="stack">
          <div className="row small">{Object.entries(data.status_counts ?? {}).map(([k, v]) => <Badge key={k} kind={DOC_KIND[k]}>{k}: {v}</Badge>)}</div>
          <div className="table-wrap" style={{ maxHeight: 420 }}>
            <table className="table"><thead><tr><th>File</th><th>Status</th><th>Chunks</th><th>Size</th></tr></thead><tbody>
              {(data.documents ?? []).map((d) => (
                <tr key={d.id}><td className="mono small break">{d.rel_path}{d.error && <div className="faint tiny">{d.error}</div>}</td>
                  <td><Badge kind={DOC_KIND[d.status]}>{d.status.replace("_", " ")}</Badge></td><td>{d.chunk_count}</td><td className="nowrap">{bytes(d.size)}</td></tr>
              ))}
            </tbody></table>
          </div>
        </div>
      )}
    </Modal>
  );
}

export default function KnowledgePage() {
  const { data, error, loading, reload } = useLoad(() => api.get<ListResponse>("/api/v1/knowledge/collections"));
  const [adding, setAdding] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [results, setResults] = useState<{ results: SearchHit[]; took_ms: number; mode: string | null } | null>(null);
  const [searching, setSearching] = useState(false);
  const toast = useToast();
  useLiveRefresh(["knowledge.changed", "job.updated"], reload, 700);
  const act = async (fn: () => Promise<unknown>, msg?: string) => { try { await fn(); if (msg) toast.ok(msg); void reload(); } catch (e) { toast.error(e); } };

  return (
    <div className="page">
      <div className="page-header"><h1>Knowledge</h1><span className="spacer" /><button className="btn primary" onClick={() => setAdding(true)} disabled={!data}>＋ Add folder</button></div>
      <p className="muted small">Folders you authorise are indexed so your assistant can answer from them with real source references. Files are never modified.</p>
      <ErrorAlert error={error} />
      {data && !data.embedding_model && <div className="alert warn">No embedding model is available, so search uses keywords only. Install one (e.g. <code>ollama pull nomic-embed-text</code>) and set it on the <a href="#/models">Models</a> page for semantic search.</div>}
      {data?.embedding_model && <div className="faint small" style={{ marginBottom: 8 }}>Semantic search uses <strong>{data.embedding_model}</strong>.</div>}
      {loading && !data ? <Spinner /> : data?.collections.length === 0 ? <Empty icon="📚" title="No document folders yet">Add a folder of notes, PDFs or code to make it searchable.</Empty> : (
        <div className="grid cols-2">
          {data?.collections.map((c) => (
            <div key={c.id} className="card stack">
              <div className="card-head"><h3>{c.name}</h3><Badge kind={STATUS_KIND[c.status]}>{c.status}</Badge>{c.status === "indexing" && <Spinner />}</div>
              <div className="mono small break">{c.root_path}</div>
              <div className="small muted">on {c.node_name} {c.node_online === false && <Badge>offline</Badge>} · {c.doc_count} documents · {c.chunk_count} chunks · {c.embedded_count}/{c.chunk_count} embedded</div>
              {c.chunk_count > 0 && <div className="meter"><span style={{ width: `${Math.round((c.embedded_count / c.chunk_count) * 100)}%` }} /></div>}
              {c.error && <div className="alert warn small">{c.error}</div>}
              <div className="faint small">Last indexed {relTime(c.last_indexed_at)}</div>
              <div className="row">
                <button className="btn sm" onClick={() => setDetail(c.id)}>Documents</button>
                <button className="btn sm" disabled={c.status === "indexing"} onClick={() => act(() => api.post(`/api/v1/knowledge/collections/${c.id}/scan`), "Re-scan started")}>Re-scan</button>
                {c.embedded_count < c.chunk_count && data.embedding_model && <button className="btn sm" onClick={() => act(() => api.post(`/api/v1/knowledge/collections/${c.id}/embed`), "Embedding queued")}>Embed missing</button>}
                <span className="spacer" />
                <ConfirmButton message={`Remove “${c.name}” from HomeMesh? The search index is deleted; your files are not touched.`} onConfirm={() => act(() => api.del(`/api/v1/knowledge/collections/${c.id}`))}>Remove</ConfirmButton>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="card" style={{ marginTop: 18 }}>
        <h2>Search your documents</h2>
        <form className="row" onSubmit={async (e) => {
          e.preventDefault();
          if (!q.trim()) return;
          setSearching(true);
          try { setResults(await api.post("/api/v1/knowledge/search", { query: q, limit: 8 })); } catch (err) { toast.error(err); } finally { setSearching(false); }
        }}>
          <input type="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="What are you looking for?" style={{ flex: 1 }} aria-label="Search documents" />
          <button className="btn primary" disabled={searching}>{searching ? "Searching…" : "Search"}</button>
        </form>
        {results && (
          <div style={{ marginTop: 10 }}>
            <div className="faint small">{results.results.length} result(s) in {results.took_ms} ms{results.mode ? ` · ${results.mode} retrieval` : ""}</div>
            {results.results.map((r) => (
              <div key={r.chunk_id} className="list-item stack" style={{ gap: 4 }}>
                <div className="row small"><strong className="break">{r.source}</strong><span className="spacer" /><span className="faint">score {r.score.toFixed(3)}{r.similarity != null ? ` · similarity ${r.similarity.toFixed(2)}` : ""}</span></div>
                <div className="small muted break" style={{ whiteSpace: "pre-wrap" }}>{r.content.slice(0, 500)}{r.content.length > 500 ? "…" : ""}</div>
              </div>
            ))}
          </div>
        )}
      </div>
      {adding && data && <AddCollection data={data} onClose={() => setAdding(false)} onCreated={() => { setAdding(false); void reload(); toast.ok("Folder added; indexing started"); }} />}
      {detail && <CollectionDetail id={detail} onClose={() => setDetail(null)} />}
    </div>
  );
}
