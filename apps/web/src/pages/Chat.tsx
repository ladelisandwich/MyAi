import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api, readSSE, streamGet, streamPost } from "../api/client";
import type { Conversation, Message, ModelGroup, NodeSummary, Project, Source } from "../api/types";
import { Badge, CopyButton, Empty, ErrorAlert, MockBadge, Spinner, useToast } from "../components/ui";
import { useLiveRefresh } from "../lib/events";
import { ms, num, relTime } from "../lib/format";
import { renderMarkdown } from "../lib/markdown";
import { navigate, type Route } from "../lib/router";

interface RouteInfo {
  model: string;
  backend: string;
  backend_type: string;
  node_id: string;
  node_name: string;
  placement: string;
  mock: boolean;
  manual_model?: boolean;
  manual_node?: boolean;
  rerouted?: boolean;
}

interface ToolEvent { name: string; arguments: Record<string, unknown>; ok?: boolean; result?: unknown; running: boolean }
interface ApprovalEvent { approval_id: string; tool: string; arguments: Record<string, unknown>; side_effect: string; description?: string; decided?: "approved" | "denied" }

interface Live {
  streaming: boolean;
  route?: RouteInfo;
  memories?: { id: string; content: string; source: string }[];
  notes?: string[];
  tools: ToolEvent[];
  approvals: ApprovalEvent[];
  notices: string[];
  errorHint?: string;
}

type UIMessage = Message & { live?: Live };

function Markdown({ text, streaming }: { text: string; streaming?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const html = useMemo(() => renderMarkdown(text), [text]);
  useEffect(() => {
    if (!ref.current) return;
    ref.current.querySelectorAll("pre").forEach((pre) => {
      if (pre.querySelector(".copy-code")) return;
      const btn = document.createElement("button");
      btn.className = "btn sm copy-code";
      btn.type = "button";
      btn.textContent = "Copy";
      btn.onclick = async () => {
        const code = pre.querySelector("code")?.textContent ?? pre.textContent ?? "";
        try { await navigator.clipboard.writeText(code); btn.textContent = "Copied"; } catch { btn.textContent = "Copy failed"; }
        setTimeout(() => (btn.textContent = "Copy"), 1200);
      };
      pre.appendChild(btn);
    });
  }, [html]);
  return <div ref={ref} className={`md ${streaming ? "typing" : ""}`} dangerouslySetInnerHTML={{ __html: html }} />;
}

function Sources({ sources }: { sources: Source[] }) {
  if (!sources?.length) return null;
  return (
    <details>
      <summary>{sources.length} source{sources.length > 1 ? "s" : ""} from your documents</summary>
      <div className="sources" style={{ marginTop: 6 }}>
        {sources.map((s) => (
          <div key={s.id} className="src">
            <span className="id">[{s.id}]</span>
            <span className="break">{s.source}</span>
            {s.retrieval_mode && <span className="faint tiny">{s.retrieval_mode}</span>}
          </div>
        ))}
      </div>
    </details>
  );
}

function MetaLine({ m }: { m: UIMessage }) {
  const r = m.live?.route;
  const model = r?.model ?? m.model;
  const node = r?.node_name;
  const mock = r?.mock ?? m.mock;
  const mt = m.metrics_json;
  if (!model) return null;
  return (
    <div className="meta">
      {mock && <MockBadge />}
      <span title="Model that produced this response">{model}</span>
      {node && <span>· on {node}</span>}
      {r?.placement && <span className="faint">({r.backend}, {r.placement})</span>}
      {!r && m.backend && <span className="faint">({m.backend})</span>}
      {r?.rerouted && <Badge kind="warn">rerouted</Badge>}
      {r?.manual_model && <Badge>manual model</Badge>}
      {mt?.tokens_per_second != null && <span>· {num(mt.tokens_per_second, 1)} tok/s</span>}
      {mt?.ttft_ms != null && <span>· first token {ms(mt.ttft_ms)}</span>}
      {mt?.completion_tokens != null && <span>· {mt.completion_tokens} tokens</span>}
      {mt?.load_ms != null && mt.load_ms > 500 && <span className="faint">· model load {ms(mt.load_ms)}</span>}
    </div>
  );
}

function AssistantMessage({ m, isLast, onRegenerate, onApprove }: {
  m: UIMessage; isLast: boolean; onRegenerate: () => void; onApprove: (id: string, ok: boolean) => void;
}) {
  const live = m.live;
  const streaming = live?.streaming ?? m.status === "streaming";
  const tools = live?.tools ?? (m.tool_calls_json ?? []).map((t) => ({ name: t.name, arguments: t.arguments, ok: t.result?.ok, result: t.result, running: false }));
  const memories = live?.memories ?? m.context_json?.memories ?? [];
  const notes = live?.notes ?? m.context_json?.notes ?? [];
  return (
    <div className="msg assistant">
      {!live && m.context_json?.memory_saved && (
        <div className="notice">📌 {m.context_json.memory_saved.outcome === "created" ? "Saved to memory" : "Already in memory"}: “{m.context_json.memory_saved.content}” <a href="#/memory">manage</a></div>
      )}
      {live?.notices.map((n, i) => <div key={i} className="notice">{n}</div>)}
      {tools.map((t, i) => (
        <div key={i} className="tool">
          <div className="row"><strong>🔧 {t.name}</strong>
            {t.running ? <Spinner /> : t.ok ? <Badge kind="ok">done</Badge> : <Badge kind="danger">failed</Badge>}
          </div>
          <details><summary>arguments and result</summary>
            <pre className="block">{JSON.stringify({ arguments: t.arguments, result: t.result }, null, 2)}</pre>
          </details>
        </div>
      ))}
      {live?.approvals.map((a) => (
        <div key={a.approval_id} className="approval">
          <div><strong>Approval needed:</strong> the assistant wants to run <code>{a.tool}</code> ({a.side_effect}).</div>
          {a.description && <div className="small muted">{a.description}</div>}
          <pre className="block" style={{ margin: "6px 0" }}>{JSON.stringify(a.arguments, null, 2)}</pre>
          {a.decided ? <Badge kind={a.decided === "approved" ? "ok" : "danger"}>{a.decided}</Badge> : (
            <div className="row">
              <button className="btn ok solid sm" onClick={() => onApprove(a.approval_id, true)}>Approve</button>
              <button className="btn danger sm" onClick={() => onApprove(a.approval_id, false)}>Deny</button>
            </div>
          )}
        </div>
      ))}
      {m.thinking && (
        <details><summary>Reasoning</summary><div className="small muted" style={{ whiteSpace: "pre-wrap" }}>{m.thinking}</div></details>
      )}
      <div className="bubble">
        {m.content ? <Markdown text={m.content} streaming={streaming} /> : streaming ? <Spinner label={live?.route ? `${live.route.model} on ${live.route.node_name} is thinking…` : "Choosing a computer…"} /> : null}
      </div>
      {(m.status === "failed" || m.status === "interrupted") && (
        <div className="alert danger small">{m.error ?? "The response failed."}{live?.errorHint && <span className="hint">{live.errorHint}</span>}</div>
      )}
      {m.status === "cancelled" && <div className="faint small">Stopped.</div>}
      <Sources sources={m.sources_json ?? []} />
      {memories.length > 0 && (
        <details><summary>Used {memories.length} saved memor{memories.length > 1 ? "ies" : "y"}</summary>
          <ul className="small" style={{ margin: "4px 0", paddingLeft: 18 }}>{memories.map((x) => <li key={x.id}>{x.content}</li>)}</ul>
        </details>
      )}
      {notes.length > 0 && <div className="faint tiny">{notes.join(" ")}</div>}
      <div className="row">
        <MetaLine m={m} />
        <span className="spacer" />
        {!streaming && (
          <div className="actions">
            {m.content && <CopyButton text={m.content} />}
            {isLast && <button className="btn ghost sm" onClick={onRegenerate}>Regenerate</button>}
          </div>
        )}
      </div>
    </div>
  );
}

function ConversationList({ activeId, open, onClose }: { activeId?: string; open: boolean; onClose: () => void }) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [project, setProject] = useState("");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ conversation_id: string; title: string; snippet: string }[] | null>(null);
  const toast = useToast();
  const load = useCallback(async () => {
    try {
      const [c, p] = await Promise.all([
        api.get<Conversation[]>(`/api/v1/conversations${project ? `?project_id=${project}` : ""}`),
        api.get<Project[]>("/api/v1/projects"),
      ]);
      setItems(c);
      setProjects(p);
    } catch (e) { toast.error(e); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project]);
  useEffect(() => { void load(); }, [load]);
  useLiveRefresh(["conversation.created", "conversation.updated", "conversation.deleted", "project.changed"], load, 500);
  useEffect(() => {
    if (!q.trim()) { setHits(null); return; }
    const t = setTimeout(async () => {
      try { setHits(await api.get(`/api/v1/conversations/search?q=${encodeURIComponent(q)}`)); } catch { setHits([]); }
    }, 300);
    return () => clearTimeout(t);
  }, [q]);
  const create = async () => {
    try {
      const c = await api.post<Conversation>("/api/v1/conversations", { project_id: project || null });
      navigate(`/chat/${c.id}`);
      onClose();
    } catch (e) { toast.error(e); }
  };
  return (
    <aside className={`conv-list ${open ? "open" : ""}`} aria-label="Conversations">
      <div className="head">
        <button className="btn primary" onClick={create}>＋ New conversation</button>
        <input type="search" placeholder="Search all conversations" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search conversations" />
        {projects.length > 0 && (
          <select value={project} onChange={(e) => setProject(e.target.value)} aria-label="Filter by project">
            <option value="">All projects</option>
            {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        )}
      </div>
      <div className="conv-items">
        {hits !== null ? (
          hits.length === 0 ? <div className="faint small" style={{ padding: 10 }}>No matches.</div> :
            hits.map((h, i) => (
              <button key={i} className="conv-item" onClick={() => { navigate(`/chat/${h.conversation_id}`); onClose(); }}>
                <div className="t truncate">{h.title}</div>
                <div className="m break">{h.snippet}</div>
              </button>
            ))
        ) : items.length === 0 ? <div className="faint small" style={{ padding: 10 }}>No conversations yet.</div> :
          items.map((c) => (
            <button key={c.id} className={`conv-item ${c.id === activeId ? "active" : ""}`} onClick={() => { navigate(`/chat/${c.id}`); onClose(); }}>
              <div className="t truncate">{c.pinned ? "📌 " : ""}{c.title}</div>
              <div className="m"><span>{relTime(c.updated_at)}</span>{c.message_count ? <span>· {c.message_count} msgs</span> : null}</div>
            </button>
          ))}
      </div>
    </aside>
  );
}

export default function ChatPage({ route }: { route: Route }) {
  const convId = route.rest[0];
  const [listOpen, setListOpen] = useState(false);
  const toast = useToast();

  useEffect(() => {
    if (convId) return;
    // Open the most recent conversation, or create the first one.
    (async () => {
      try {
        const list = await api.get<Conversation[]>("/api/v1/conversations?limit=1");
        if (list.length) navigate(`/chat/${list[0].id}`);
        else {
          const c = await api.post<Conversation>("/api/v1/conversations", {});
          navigate(`/chat/${c.id}`);
        }
      } catch (e) { toast.error(e); }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [convId]);

  return (
    <div className="chat">
      {listOpen && <div className="scrim" onClick={() => setListOpen(false)} />}
      <ConversationList activeId={convId} open={listOpen} onClose={() => setListOpen(false)} />
      {convId ? <ChatView key={convId} convId={convId} openList={() => setListOpen(true)} /> : <div className="chat-main"><Empty title="Loading…" /></div>}
    </div>
  );
}

function ChatView({ convId, openList }: { convId: string; openList: () => void }) {
  const [conv, setConv] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [models, setModels] = useState<ModelGroup[]>([]);
  const [nodes, setNodes] = useState<NodeSummary[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [useMemory, setUseMemory] = useState(true);
  const [useKnowledge, setUseKnowledge] = useState(true);
  const [useTools, setUseTools] = useState(true);
  const [attachments, setAttachments] = useState<{ id: string; filename: string }[]>([]);
  const [editingTitle, setEditingTitle] = useState(false);
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const abortRef = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const load = useCallback(async () => {
    try {
      const c = await api.get<Conversation>(`/api/v1/conversations/${convId}`);
      setConv(c);
      setMessages((prev) => {
        // Keep live state for a message that is still streaming in this tab.
        const liveById = new Map(prev.filter((m) => m.live?.streaming).map((m) => [m.id, m]));
        return (c.messages ?? []).map((m) => liveById.get(m.id) ?? m);
      });
      setError(null);
      return c;
    } catch (e) {
      setError(e);
      return null;
    }
  }, [convId]);

  const loadMeta = useCallback(async () => {
    try {
      const [m, n] = await Promise.all([api.get<{ models: ModelGroup[] }>("/api/v1/models"), api.get<NodeSummary[]>("/api/v1/nodes")]);
      setModels(m.models);
      setNodes(n);
    } catch { /* non-fatal */ }
  }, []);

  useEffect(() => {
    void loadMeta();
    void load().then((c) => { if (c?.active_run) void attach(c.active_run); });
    return () => abortRef.current?.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [convId]);

  useLiveRefresh(["node.connected", "node.disconnected", "node.models"], loadMeta, 800);
  useLiveRefresh(["conversation.updated"], () => { if (!busy) void load(); }, 400);

  useEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [messages]);

  const patchAssistant = (id: string, fn: (m: UIMessage) => UIMessage) =>
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));

  const consume = async (res: Response, assistantTempId: string, signal: AbortSignal) => {
    let aid = assistantTempId;
    for await (const ev of readSSE(res, signal)) {
      const d = ev.data as Record<string, unknown>;
      switch (ev.event) {
        case "meta": {
          const realA = d.assistant_message_id as string;
          const realU = d.user_message_id as string;
          setMessages((ms) => ms.map((m) => (m.id === aid ? { ...m, id: realA } : m.id === "tmp-user" ? { ...m, id: realU } : m)));
          aid = realA;
          break;
        }
        case "memory_saved":
          patchAssistant(aid, (m) => ({ ...m, live: { ...m.live!, notices: [...m.live!.notices, `📌 ${d.outcome === "created" ? "Saved to memory" : "Already in memory"}: “${d.content}”`] } }));
          break;
        case "route":
          patchAssistant(aid, (m) => ({ ...m, model: d.model as string, mock: d.mock as boolean, live: { ...m.live!, route: d as unknown as RouteInfo } }));
          break;
        case "context":
          patchAssistant(aid, (m) => ({ ...m, sources_json: (d.sources as Source[]) ?? [], live: { ...m.live!, memories: d.memories as Live["memories"], notes: d.notes as string[] } }));
          break;
        case "delta":
          patchAssistant(aid, (m) => ({ ...m, content: m.content + (d.text as string) }));
          break;
        case "thinking":
          patchAssistant(aid, (m) => ({ ...m, thinking: (m.thinking ?? "") + (d.text as string) }));
          break;
        case "status":
          patchAssistant(aid, (m) => ({ ...m, live: { ...m.live!, notices: [...m.live!.notices, `↪ ${d.message}`] } }));
          break;
        case "tool_call":
          patchAssistant(aid, (m) => ({ ...m, live: { ...m.live!, tools: [...m.live!.tools, { name: d.name as string, arguments: d.arguments as Record<string, unknown>, running: true }] } }));
          break;
        case "approval_required":
          patchAssistant(aid, (m) => ({ ...m, live: { ...m.live!, approvals: [...m.live!.approvals, d as unknown as ApprovalEvent] } }));
          break;
        case "tool_result":
          patchAssistant(aid, (m) => {
            const tools = [...m.live!.tools];
            const idx = tools.map((t) => t.name).lastIndexOf(d.name as string);
            if (idx >= 0) tools[idx] = { ...tools[idx], running: false, ok: d.ok as boolean, result: d.result };
            return { ...m, live: { ...m.live!, tools } };
          });
          break;
        case "done":
          patchAssistant(aid, (m) => ({ ...m, status: "complete", metrics_json: d.metrics as Message["metrics_json"], live: { ...m.live!, streaming: false } }));
          break;
        case "cancelled":
          patchAssistant(aid, (m) => ({ ...m, status: "cancelled", live: { ...m.live!, streaming: false } }));
          break;
        case "error":
          patchAssistant(aid, (m) => ({ ...m, status: "failed", error: d.message as string, live: { ...m.live!, streaming: false, errorHint: d.hint as string | undefined } }));
          break;
      }
    }
    return aid;
  };

  const run = async (body: Record<string, unknown>, userText: string | null) => {
    setBusy(true);
    stickToBottom.current = true;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const now = new Date().toISOString();
    const assistant: UIMessage = { id: "tmp-assistant", conversation_id: convId, role: "assistant", content: "", status: "streaming", created_at: now, live: { streaming: true, tools: [], approvals: [], notices: [] } };
    setMessages((ms) => [
      ...ms,
      ...(userText !== null ? [{ id: "tmp-user", conversation_id: convId, role: "user" as const, content: userText, status: "complete" as const, created_at: now, attachments_json: attachments.map((a) => ({ ...a, mime: "", kind: "image", size: 0 })) }] : []),
      assistant,
    ]);
    try {
      const res = await streamPost(`/api/v1/conversations/${convId}/messages`, body, ctrl.signal);
      await consume(res, "tmp-assistant", ctrl.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        setMessages((ms) => ms.filter((m) => !m.id.startsWith("tmp-")));
        toast.error(e);
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
      void load();
    }
  };

  const attach = async (messageId: string) => {
    // Re-attach to a response that is still being generated (e.g. started on another device).
    setBusy(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setMessages((ms) => ms.map((m) => (m.id === messageId ? { ...m, content: "", live: { streaming: true, tools: [], approvals: [], notices: [] } } : m)));
    try {
      const res = await streamGet(`/api/v1/chat/runs/${messageId}/events`, ctrl.signal);
      await consume(res, messageId, ctrl.signal);
    } catch { /* run finished meanwhile */ } finally {
      setBusy(false);
      void load();
    }
  };

  const send = async () => {
    const text = input.trim();
    if ((!text && attachments.length === 0) || busy) return;
    setInput("");
    const ids = attachments.map((a) => a.id);
    setAttachments([]);
    await run({ content: text, use_memory: useMemory, use_knowledge: useKnowledge, use_tools: useTools, attachment_ids: ids }, text);
  };

  const stop = async () => {
    const streamingMsg = messages.find((m) => m.live?.streaming || m.status === "streaming");
    if (streamingMsg && !streamingMsg.id.startsWith("tmp-")) {
      try { await api.post(`/api/v1/chat/runs/${streamingMsg.id}/cancel`); } catch (e) { toast.error(e); }
    }
  };

  const regenerate = async (m: UIMessage) => {
    setMessages((ms) => ms.slice(0, ms.findIndex((x) => x.id === m.id)));
    await run({ regenerate_of: m.id, use_memory: useMemory, use_knowledge: useKnowledge, use_tools: useTools }, null);
  };

  const approve = async (approvalId: string, ok: boolean) => {
    try {
      await api.post(`/api/v1/tools/approvals/${approvalId}`, { approved: ok });
      setMessages((ms) => ms.map((m) => (m.live ? { ...m, live: { ...m.live, approvals: m.live.approvals.map((a) => (a.approval_id === approvalId ? { ...a, decided: ok ? "approved" : "denied" } : a)) } } : m)));
    } catch (e) { toast.error(e); }
  };

  const updateConv = async (patch: Record<string, unknown>) => {
    try {
      setConv(await api.patch<Conversation>(`/api/v1/conversations/${convId}`, patch));
    } catch (e) { toast.error(e); }
  };

  const upload = async (files: FileList | null) => {
    if (!files) return;
    for (const f of Array.from(files)) {
      try {
        const a = await api.upload<{ id: string; filename: string }>(`/api/v1/attachments?filename=${encodeURIComponent(f.name)}&conversation_id=${convId}`, f);
        setAttachments((x) => [...x, a]);
      } catch (e) { toast.error(e); }
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  const chatModels = models.filter((m) => m.capabilities.includes("completion") && m.available);
  const onlineNodes = nodes.filter((n) => n.online);
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const noModels = models.length > 0 && chatModels.length === 0;

  if (error && !conv) return <div className="chat-main"><div className="page"><ErrorAlert error={error} /></div></div>;

  return (
    <div className="chat-main">
      <div className="chat-head">
        <button className="btn sm only-mobile" aria-label="Show conversations" onClick={openList}>💬 Chats</button>
        {editingTitle ? (
          <input autoFocus defaultValue={conv?.title} style={{ flex: 1, minWidth: 120 }} aria-label="Conversation title"
            onBlur={(e) => { setEditingTitle(false); if (e.target.value.trim() && e.target.value !== conv?.title) void updateConv({ title: e.target.value.trim() }); }}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setEditingTitle(false); }} />
        ) : (
          <span className="title truncate" title="Click to rename" onClick={() => setEditingTitle(true)}>{conv?.title ?? "…"}</span>
        )}
        <select value={conv?.settings_json?.model ?? ""} aria-label="Model" title="Model (Auto lets HomeMesh choose)"
          onChange={(e) => void updateConv(e.target.value ? { model: e.target.value } : { clear_model: true })}>
          <option value="">Model: Auto</option>
          {chatModels.map((m) => <option key={m.name} value={m.name}>{m.mock ? "[MOCK] " : ""}{m.name}{m.parameter_size ? ` (${m.parameter_size})` : ""}</option>)}
        </select>
        <select value={conv?.settings_json?.node_id ?? ""} aria-label="Computer" title="Computer (Auto picks the best available)"
          onChange={(e) => void updateConv(e.target.value ? { node_id: e.target.value } : { clear_node: true })}>
          <option value="">Computer: Auto</option>
          {onlineNodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
        </select>
        <a className="btn ghost sm" href={`/api/v1/conversations/${convId}/export`} title="Export as JSON">Export</a>
        <button className="btn ghost sm danger" onClick={async () => {
          if (!confirm("Delete this conversation permanently?")) return;
          try { await api.del(`/api/v1/conversations/${convId}`); navigate("/chat"); } catch (e) { toast.error(e); }
        }}>Delete</button>
      </div>
      <div className="messages" ref={scroller} onScroll={(e) => {
        const el = e.currentTarget;
        stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}>
        <div className="messages-inner">
          {noModels && (
            <div className="alert warn">No chat model is available on any connected computer. Install one with Ollama (for example <code>ollama pull llama3.2</code>) or download one on the <a href="#/models">Models</a> page.</div>
          )}
          {onlineNodes.length === 0 && nodes.length > 0 && <div className="alert warn">No computer is online right now, so nothing can answer. Check the <a href="#/nodes">Computers</a> page.</div>}
          {messages.length === 0 && <Empty icon="👋" title="Start a conversation">Your assistant remembers what you ask it to (“remember that…”), can search your indexed documents, and runs on your own computers.</Empty>}
          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.id} className="msg user">
                <div className="bubble">{m.content}</div>
                {(m.attachments_json ?? []).length > 0 && <div className="faint tiny">📎 {(m.attachments_json ?? []).map((a) => a.filename).join(", ")}</div>}
              </div>
            ) : m.role === "assistant" ? (
              <AssistantMessage key={m.id} m={m} isLast={m.id === lastAssistant?.id} onRegenerate={() => void regenerate(m)} onApprove={approve} />
            ) : null,
          )}
        </div>
      </div>
      <div className="composer">
        <div className="composer-inner">
          {attachments.length > 0 && (
            <div className="attach-preview">
              {attachments.map((a) => (
                <span key={a.id} className="chip">🖼 {a.filename}
                  <button className="btn ghost sm icon-only" aria-label={`Remove ${a.filename}`} onClick={() => setAttachments((x) => x.filter((y) => y.id !== a.id))}>✕</button>
                </span>
              ))}
              <span className="faint tiny">Images are only sent to models that support vision.</span>
            </div>
          )}
          <div className="row nowrap" style={{ alignItems: "flex-end" }}>
            <button className="btn icon-only" title="Attach an image" aria-label="Attach an image" onClick={() => fileRef.current?.click()} disabled={busy}>📎</button>
            <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden onChange={(e) => void upload(e.target.files)} />
            <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} rows={1}
              placeholder="Message your assistant…" aria-label="Message" />
            {busy ? <button className="btn danger" onClick={() => void stop()}>■ Stop</button>
              : <button className="btn primary" onClick={() => void send()} disabled={!input.trim() && attachments.length === 0}>Send</button>}
          </div>
          <div className="opts">
            <label className="check"><input type="checkbox" checked={useMemory} onChange={(e) => setUseMemory(e.target.checked)} />Use memories</label>
            <label className="check"><input type="checkbox" checked={useKnowledge} onChange={(e) => setUseKnowledge(e.target.checked)} />Search my documents</label>
            <label className="check"><input type="checkbox" checked={useTools} onChange={(e) => setUseTools(e.target.checked)} />Allow tools</label>
            <span className="spacer" />
            <span className="faint desktop-only">Enter to send · Shift+Enter for a new line · say “remember that …” to save a memory</span>
          </div>
        </div>
      </div>
    </div>
  );
}
