import { useState } from "react";
import { api } from "../api/client";
import type { Memory, Project } from "../api/types";
import { Badge, ConfirmButton, Empty, ErrorAlert, Modal, Spinner, useLoad, useToast } from "../components/ui";
import { useLiveRefresh } from "../lib/events";
import { relTime } from "../lib/format";

const SOURCE: Record<string, { label: string; kind?: string; title: string }> = {
  user_explicit: { label: "You said", kind: "ok", title: "You explicitly asked HomeMesh to remember this" },
  user_edited: { label: "Edited by you", kind: "ok", title: "You corrected this memory" },
  model_inferred: { label: "Suggested", kind: "violet", title: "Proposed automatically from a conversation - only used after you approve it" },
  imported: { label: "Imported", title: "Imported from elsewhere" },
};
const KINDS = ["fact", "preference", "project", "note"];

function MemoryItem({ m, projects, reload }: { m: Memory; projects: Project[]; reload: () => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(m.content);
  const toast = useToast();
  const src = SOURCE[m.source] ?? { label: m.source, title: "" };
  const act = async (fn: () => Promise<unknown>) => { try { await fn(); reload(); } catch (e) { toast.error(e); } };
  return (
    <div className="list-item stack" style={{ gap: 6 }}>
      {editing ? (
        <div className="stack">
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={3} maxLength={2000} aria-label="Memory text" />
          <div className="row"><button className="btn primary sm" onClick={() => act(async () => { await api.patch(`/api/v1/memories/${m.id}`, { content: text }); setEditing(false); })}>Save correction</button>
            <button className="btn sm" onClick={() => { setText(m.content); setEditing(false); }}>Cancel</button></div>
        </div>
      ) : <div className="break">{m.pinned && "📌 "}{m.content}</div>}
      <div className="row small">
        <Badge kind={src.kind} title={src.title}>{src.label}</Badge>
        <Badge>{m.kind}</Badge>
        {m.project_id && <Badge kind="accent">{projects.find((p) => p.id === m.project_id)?.name ?? "project"}</Badge>}
        <span className="faint">saved {relTime(m.created_at)}{m.use_count ? ` · used ${m.use_count}×` : ""}{m.expires_at ? ` · expires ${new Date(m.expires_at).toLocaleDateString()}` : ""}</span>
        {m.conversation_id && <a href={`#/chat/${m.conversation_id}`} className="tiny">source conversation</a>}
        <span className="spacer" />
        {m.status === "candidate" ? <>
          <button className="btn ok solid sm" onClick={() => act(() => api.post(`/api/v1/memories/${m.id}/approve`))}>Approve</button>
          <button className="btn sm" onClick={() => act(() => api.post(`/api/v1/memories/${m.id}/reject`))}>Reject</button>
        </> : <>
          <button className="btn ghost sm" onClick={() => setEditing(true)}>Edit</button>
          <button className="btn ghost sm" onClick={() => act(() => api.patch(`/api/v1/memories/${m.id}`, { pinned: !m.pinned }))}>{m.pinned ? "Unpin" : "Pin"}</button>
          {m.status === "active" ? <button className="btn ghost sm" onClick={() => act(() => api.patch(`/api/v1/memories/${m.id}`, { status: "archived" }))}>Archive</button>
            : <button className="btn ghost sm" onClick={() => act(() => api.patch(`/api/v1/memories/${m.id}`, { status: "active" }))}>Restore</button>}
        </>}
        <ConfirmButton className="btn ghost sm danger" message="Delete this memory permanently?" onConfirm={() => act(() => api.del(`/api/v1/memories/${m.id}`))}>Delete</ConfirmButton>
      </div>
    </div>
  );
}

function ProjectsDialog({ projects, onClose, reload }: { projects: Project[]; onClose: () => void; reload: () => void }) {
  const [name, setName] = useState("");
  const [category, setCategory] = useState("software");
  const toast = useToast();
  return (
    <Modal title="Projects" onClose={onClose}>
      <p className="small muted">Projects group conversations and memories (software, hardware, home infrastructure, research…). Project memories are only used in that project's conversations.</p>
      <form className="row" onSubmit={async (e) => {
        e.preventDefault();
        try { await api.post("/api/v1/projects", { name, category }); setName(""); reload(); } catch (err) { toast.error(err); }
      }}>
        <input placeholder="New project name" value={name} onChange={(e) => setName(e.target.value)} required style={{ flex: 2 }} aria-label="Project name" />
        <select value={category} onChange={(e) => setCategory(e.target.value)} style={{ flex: 1 }} aria-label="Category">
          {["software", "hardware", "home", "research", "personal", "general"].map((c) => <option key={c}>{c}</option>)}
        </select>
        <button className="btn primary">Add</button>
      </form>
      <div style={{ marginTop: 12 }}>
        {projects.length === 0 ? <div className="muted small">No projects yet.</div> : projects.map((p) => (
          <div key={p.id} className="row list-item"><strong>{p.name}</strong><Badge>{p.category}</Badge><span className="spacer" />
            <ConfirmButton className="btn ghost sm danger" message={`Delete project “${p.name}” and its project memories? Conversations are kept.`} onConfirm={async () => { try { await api.del(`/api/v1/projects/${p.id}`); reload(); } catch (e) { toast.error(e); } }}>Delete</ConfirmButton>
          </div>
        ))}
      </div>
    </Modal>
  );
}

export default function MemoryPage() {
  const [tab, setTab] = useState<"active" | "candidate" | "archived">("active");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [newText, setNewText] = useState("");
  const [newKind, setNewKind] = useState("");
  const [newProject, setNewProject] = useState("");
  const [projectsOpen, setProjectsOpen] = useState(false);
  const toast = useToast();
  const status = tab === "archived" ? "archived,rejected" : tab;
  const { data, error, loading, reload } = useLoad(async () => {
    const params = new URLSearchParams({ status });
    if (query) params.set("q", query);
    if (projectFilter) params.set("project_id", projectFilter);
    const [mem, projects, settings] = await Promise.all([
      api.get<{ items: Memory[]; counts: Record<string, number> }>(`/api/v1/memories?${params}`),
      api.get<Project[]>("/api/v1/projects"),
      api.get<{ settings: { memory: Record<string, unknown> } }>("/api/v1/settings"),
    ]);
    return { ...mem, projects, memSettings: settings.settings.memory };
  }, [status, query, projectFilter]);
  useLiveRefresh(["memory.changed", "project.changed", "settings.updated"], reload, 400);

  const setMemSetting = async (patch: Record<string, unknown>) => {
    try { await api.patch("/api/v1/settings/memory", patch); void reload(); } catch (e) { toast.error(e); }
  };

  return (
    <div className="page">
      <div className="page-header"><h1>Memory</h1><span className="spacer" /><button className="btn sm" onClick={() => setProjectsOpen(true)}>Projects</button></div>
      <p className="muted small">What your assistant remembers across conversations and devices. It never invents memories: everything here was either said by you or approved by you, and you can correct or delete anything.</p>
      <ErrorAlert error={error} />
      {data && (
        <div className="card">
          <div className="row">
            <label className="check"><input type="checkbox" checked={Boolean(data.memSettings.enabled)} onChange={(e) => void setMemSetting({ enabled: e.target.checked })} />Use memories in conversations</label>
            <label className="check"><input type="checkbox" checked={Boolean(data.memSettings.detect_explicit)} onChange={(e) => void setMemSetting({ detect_explicit: e.target.checked })} />Save when I say “remember that …”</label>
            <label className="check" title="After each message a model proposes memories; they wait here for your approval"><input type="checkbox" checked={Boolean(data.memSettings.auto_extract)} onChange={(e) => void setMemSetting({ auto_extract: e.target.checked })} />Suggest memories automatically (needs approval)</label>
          </div>
        </div>
      )}
      <div className="card">
        <h3>Add a memory</h3>
        <form className="stack" onSubmit={async (e) => {
          e.preventDefault();
          try {
            const r = await api.post<{ outcome: string }>("/api/v1/memories", { content: newText, kind: newKind || null, project_id: newProject || null });
            toast[r.outcome === "created" ? "ok" : "info"](r.outcome === "created" ? "Memory saved" : "A matching memory already exists");
            setNewText("");
            void reload();
          } catch (err) { toast.error(err); }
        }}>
          <textarea placeholder="e.g. My home server's IP address is 192.168.1.20" value={newText} onChange={(e) => setNewText(e.target.value)} rows={2} required maxLength={2000} aria-label="New memory" />
          <div className="row">
            <select value={newKind} onChange={(e) => setNewKind(e.target.value)} style={{ width: "auto" }} aria-label="Kind"><option value="">Kind: automatic</option>{KINDS.map((k) => <option key={k} value={k}>{k}</option>)}</select>
            <select value={newProject} onChange={(e) => setNewProject(e.target.value)} style={{ width: "auto" }} aria-label="Project"><option value="">Global (all conversations)</option>{data?.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select>
            <span className="spacer" /><button className="btn primary">Save</button>
          </div>
        </form>
      </div>
      <div className="card">
        <div className="tabs">
          <button className={tab === "active" ? "active" : ""} onClick={() => setTab("active")}>Active {data?.counts.active ? `(${data.counts.active})` : ""}</button>
          <button className={tab === "candidate" ? "active" : ""} onClick={() => setTab("candidate")}>Needs review {data?.counts.candidate ? `(${data.counts.candidate})` : ""}</button>
          <button className={tab === "archived" ? "active" : ""} onClick={() => setTab("archived")}>Archived</button>
        </div>
        <form className="row" onSubmit={(e) => { e.preventDefault(); setQuery(q.trim()); }}>
          <input type="search" placeholder="Search memories (keyword + meaning)" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 2 }} aria-label="Search memories" />
          <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)} style={{ flex: 1 }} aria-label="Project filter">
            <option value="">All scopes</option>{data?.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <button className="btn">Search</button>
          {query && <button type="button" className="btn ghost" onClick={() => { setQ(""); setQuery(""); }}>Clear</button>}
        </form>
        <div style={{ marginTop: 8 }}>
          {loading && !data ? <Spinner /> : data?.items.length === 0 ? <Empty icon="📌" title={tab === "candidate" ? "Nothing to review" : "No memories here"}>{tab === "active" ? "Tell your assistant “remember that …” or add one above." : null}</Empty>
            : data?.items.map((m) => <MemoryItem key={m.id} m={m} projects={data.projects} reload={() => void reload()} />)}
        </div>
      </div>
      {projectsOpen && data && <ProjectsDialog projects={data.projects} onClose={() => setProjectsOpen(false)} reload={() => void reload()} />}
    </div>
  );
}
