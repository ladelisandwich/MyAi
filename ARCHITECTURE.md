# Architecture

This document describes how HomeMesh v0.1.0 is built and why. Wire-level details are in
[docs/PROTOCOL.md](docs/PROTOCOL.md) and [docs/API.md](docs/API.md).

## Overview

```
 Browser / PWA ──HTTPS (cookie + CSRF header)──┐        CLI ──HTTPS (admin token)──┐
                                               ▼                                  ▼
 ┌──────────────────────────────── coordinator process ───────────────────────────────────┐
 │ FastAPI app ─ middleware (security headers, size limits, auth, rate limits)             │
 │   ├─ api/*            REST + SSE endpoints                                             │
 │   ├─ AuthService      owner, sessions, API tokens                                      │
 │   ├─ ChatService      runs (independent of HTTP), context assembly, tool loop          │
 │   ├─ MemoryService    explicit + candidate memories, dedupe, hybrid retrieval          │
 │   ├─ KnowledgeService collections, scan/embed orchestration, hybrid retrieval          │
 │   ├─ ModelRegistry    models per node/backend, roles, capability checks                │
 │   ├─ ToolService      registry, policies, approvals, execution, audit                  │
 │   ├─ JobService       durable jobs, queue, timers, retries, dispatch ◄── scheduler()   │
 │   ├─ NodeManager      worker sessions, heartbeats, pairing, credentials                │
 │   ├─ EventBus         in-process pub/sub → /api/v1/events (SSE) for live UI            │
 │   └─ Database         SQLite (WAL) on one dedicated thread + repositories              │
 │                                                                                         │
 │  embedded worker (standalone mode) ──── same WebSocket protocol over loopback ──┐       │
 └──────────────────────────────────────────────────────────────────────────────────┼──────┘
                         ▲ WSS /api/v1/worker/ws (outbound from workers)            │
             ┌───────────┴───────────┐                                             │
     ┌───────┴────────┐     ┌────────┴───────┐                                     │
     │ WorkerAgent    │     │ WorkerAgent    │  ◄──────────────────────────────────┘
     │  Executor      │     │  Executor      │   chat · embed · knowledge.scan · tool.exec · model.manage
     │  LocalPolicy   │     │  LocalPolicy   │   allowed roots/tools, mode re-check
     │  Telemetry     │     │  Telemetry     │   CPU/RAM/GPU sampling (nvidia-smi, sysfs, CIM)
     │  Backends ─────┼─► Ollama / llama.cpp / vLLM (localhost only)
     └────────────────┘     └────────────────┘
```

## Code layout and layering

A single Python distribution (`src/homemesh`) gives every computer one `pip install`, one CLI and
one service. The subpackages correspond to the modules suggested in the original brief:

| Package | Contents | May import |
|---|---|---|
| `common` | config, paths, logging, ids, security primitives, errors, time | stdlib, pydantic |
| `protocol` | wire messages, job payload/result schemas, hardware and model types | `common` |
| `hardware` | CPU/RAM/GPU detection and live telemetry per OS | `common`, `protocol` |
| `inference` | backend adapters (Ollama, OpenAI-compatible, mock), capability + memory estimates | `common`, `protocol` |
| `scheduler` | **pure** placement policy and resource profiles | `common`, `protocol` |
| `memory`, `knowledge`, `tools` | pure domain logic (explicit-memory detection, extraction, chunking, path safety, scanner, vector index, tool registry and built-ins) | the above |
| `coordinator` | FastAPI app, services, DB | anything except `worker` |
| `worker` | agent, executor, local policy, enrollment | anything except `coordinator` |
| `cli` | `homemesh` command | anything |

`tests/unit/test_import_rules.py` enforces these boundaries and keeps OS-specific code
(`winreg`, `schtasks`, etc.) inside `hardware/` and `cli/service.py`.

The web client (`apps/web`) is a React 19 + TypeScript + Vite app with no router or UI kit. It's
built into `src/homemesh/web_dist/` and served by the coordinator, so there's nothing extra to host.

## One process, composable roles

`homemesh run` starts the roles listed in `node.roles` (`runtime.py`):

* **coordinator**: uvicorn serving the FastAPI app.
* **worker** alongside a coordinator: the **embedded worker**. It's provisioned with a local
  credential and connects to its own coordinator over loopback using **exactly the same WebSocket
  protocol** as a remote worker. Single-computer and multi-computer setups therefore share one
  code path, and everything tested with one also holds for the other.
* **worker** alone: connects to a remote coordinator using the credential from `worker join`.

Shutdown is graceful on SIGINT/SIGTERM or when `homemesh stop` drops a stop-request file (needed on
Windows, where a windowless process gets no console signal). Workers report `shutdown`, running jobs
are cancelled cooperatively, and the DB is closed last.

## Persistence

SQLite in WAL mode with foreign keys on. A `Database` class runs every statement on **one dedicated
thread** (an executor of size 1), so the asyncio loop never blocks and writes are serialised. All
SQL lives in `coordinator/db/repositories/*`, which would make a PostgreSQL backend a contained change.

Tables (migration `0001_initial.sql`): `users, sessions, api_tokens, nodes, node_credentials,
pairing_tokens, hardware_reports, models, projects, conversations, messages, memories, collections,
documents, chunks, embeddings, jobs, job_events, tool_policies, tool_approvals, tool_executions,
settings, audit_events, attachments`, plus FTS5 tables `messages_fts`, `memories_fts`, `chunks_fts`
kept in sync by triggers. Personal tables carry `user_id` for future multi-user support.

**Migrations** are numbered `.sql` files with recorded checksums. On start the runner refuses a
database newer than the code, warns if an already-applied migration file was edited, copies a
database that holds data to `backups/pre-migration-…sqlite3` before changing anything, and applies
each pending migration in **its own transaction**. A failing migration is rolled back completely and
startup stops with recovery instructions.

**Vectors** are float32 BLOBs in `embeddings` (owner type + id, model, dimension). Search uses an
exact cosine index in numpy, built lazily per embedding model and updated incrementally. That's
fine up to roughly 100k chunks on ordinary hardware. `sqlite-vec` or an ANN index is the planned
accelerator beyond that ([ROADMAP](ROADMAP.md)). Vectors from different models are never mixed.

## Jobs

Everything a worker does is a **job** with a durable row written *before* dispatch (`jobs`,
`job_events`). Types: `chat`, `embed`, `knowledge.scan`, `tool.exec`, `model.manage`.

```
queued ──dispatch──► dispatched ──job.accepted──► running ──► completed | failed | cancelled
   ▲                     │ ack timeout / job.rejected            │ worker lost
   └──── requeue (idempotent types only: embed, knowledge.scan) ◄─┘
```

* **Interactive** jobs (chat) are placed immediately. If no computer can take one now, it waits up
  to `queue_timeout_s` for capacity (a node freeing up or coming online) and then fails with a
  specific reason ("all computers are busy", "model X needs ~9.1 GB, the largest free GPU has 6.0 GB", …).
* **Background** jobs (embedding, scanning) stay queued, and a dispatch loop places them whenever a
  node or job changes state. The queue is bounded (`max_queued_jobs`).
* Timers: an ack timeout per dispatch and a total timeout per job type. The worker enforces the
  timeout as well.
* **Failure handling.** A worker disconnect fails its non-idempotent jobs with `worker_disconnected`
  (a chat reply is never silently retried elsewhere, because that could duplicate side effects) and
  requeues idempotent ones with a bounded attempt count. After a coordinator restart, `running` chat
  jobs become `interrupted`, assistant messages left `streaming` are marked interrupted (partial
  text kept), and idempotent jobs are requeued.
* Only the node a job was dispatched to may report on it. Reports from any other node are ignored
  and logged.
* Every job stores its **scheduling decision** (`decision_json`): the chosen node and model, the
  candidates considered and why each other node was rejected. The Jobs page shows it.

## Scheduler

`scheduler/policy.py` is a pure function, `schedule(request, node_snapshots) → Decision`, unit
tested without I/O. For each node it checks, in order, and records a reason code for every rejection:

1. Online and heartbeat fresh (`offline`, `stale_heartbeat`).
2. Mode: `paused` rejects everything; `background` accepts interactive work only as a **last resort**
   when no other node qualifies (`paused`, `mode_excludes_priority`).
3. Task type supported (`task_type_unsupported`).
4. Concurrency under the mode's cap (`at_capacity`).
5. GPU busy with other programs above the node's threshold in balanced/background mode
   (`gpu_busy_external`).
6. A model with the required capabilities: `completion`, `tools`, `vision`, `embedding`
   (`model_not_on_node`, `no_suitable_model`; near misses such as `missing_capability`,
   `context_too_small`, `remote_model_disallowed`, `mock_disallowed` are reported too).
7. **Memory fit.** A model that is already loaded needs no new memory. Otherwise the requirement is
   the backend-reported resident size if known, else a conservative `file size × 1.25 + 512 MiB`.
   It must fit in **free VRAM minus the mode's reserve** (placement `gpu`). If CPU inference is
   allowed, it may instead fit in VRAM + free RAM minus reserve (`partial`) or RAM only (`cpu`);
   both are penalised. If a node reports no VRAM telemetry, only RAM is checked and the decision
   notes that (`unknown`). Otherwise the rejection is `insufficient_memory` or `insufficient_vram`.

Candidates are scored on queue load, model-load cost, CPU/partial penalties, mode, and the owner's
node weight/preference. Ties go to better placement (loaded > gpu > partial > unknown > cpu).
**Pinned** requests (the user chose a node or model) are honoured or fail with the reason; they are
never silently re-routed. The automatic model choice uses the role default from settings
(`model_roles`) if it's available, otherwise a loaded, GPU-fitting, capable model.

Workers re-check every job against their own mode, limits, allowed paths and tools
(`worker/policy.py`) and reject with a code if the coordinator's view was stale (defence in depth).
Nothing in HomeMesh kills or throttles other processes; it only declines its own work.

## Chat engine

`POST /api/v1/conversations/{id}/messages` creates the user message and an assistant message in
state `streaming`, then starts a **run** that's independent of the HTTP request. The response is a
server-sent event stream:

`meta → route → context → (delta | thinking | tool_call | approval_required | tool_result)* → memory_saved? → done | error | cancelled`

If the browser disconnects the run continues. Partial text is persisted about once a second, and
any device can re-attach with `GET /api/v1/chat/runs/{message_id}/events`. Cancel with
`POST …/cancel`; the worker aborts the backend stream and the partial reply is kept as `cancelled`.

**Context assembly** (`coordinator/context.py`) builds, within a token budget derived from the
model's context window minus the output reserve:

1. a system message: HomeMesh instructions, the owner's custom instructions and profile (trusted);
2. a `<memories>` block and a `<documents>` block (retrieved excerpts with `[S1]`… source ids), both
   **explicitly marked as untrusted reference data**;
3. as much recent history as fits (newest first), then the new message.

Each section has its own budget; the real `prompt_eval_count` from the backend is recorded with the
reply. The stored message records model, backend, node, job, metrics (TTFT, tokens/s, load time,
dispatch overhead), sources and memory ids used, and whether it came from the mock backend.

**Tool loop.** Tools are offered only to models with the `tools` capability (up to `max_rounds`).
Internet tools (`web_search`, `fetch_url`, `open_url`) are ordinary node tools: SSRF-safe fetching lives
in `tools/webfetch.py` and `tools/urlsafety.py`; the coordinator lets `fetch_url` run without approval
only for links the user wrote and a few search results per answer (`ToolService._unasked_fetch_refusal`,
docs/WEB_ACCESS.md).
Each round is its own job. Tool calls are validated, checked against policy, and either executed
(on the coordinator, or as a `tool.exec` job on the node that owns the data) or held for **approval**,
which is shown in the UI of every connected device. Results go back to the model as `role: tool` data.

## Memory

`memory/text.py` detects explicit requests ("remember that…", "/remember …"). These are saved as
`user_explicit` with provenance (conversation, message). Optional automatic extraction (off by
default) runs after a reply and creates **candidates** (`model_inferred`) that the owner approves or
rejects. Deduplication uses a normalised-text hash plus a near-duplicate check (token Jaccard ≥ 0.85
against the closest keyword matches). Retrieval is hybrid: FTS5 BM25 and vector similarity fused with
**reciprocal-rank fusion**, scoped to global + current project. It updates `last_used_at`.

## Knowledge (RAG)

A **collection** is a folder on a specific node. Scanning runs *on that node* (`knowledge.scan` job),
so files never leave the computer except as extracted text chunks:

1. The node validates the root against its own `allowed_roots` and the sensitive-path deny-list.
2. It walks the tree (skipping excluded and hidden folders, size caps, no symlink escapes) and compares
   size and mtime, then SHA-256, with the known documents the coordinator sent, so it only extracts
   what changed.
3. Extraction: text, Markdown, code, JSON, CSV and PDF (pypdf). PDFs without extractable text are
   marked `needs_ocr` rather than silently indexed empty. Unsupported types are listed as such.
4. Heading- and paragraph-aware chunking with line ranges (and page numbers for PDFs). Chunks are
   streamed back as `document` outputs and stored with FTS5.
5. `embed` jobs, batched, go to **any** node that has the configured embedding model, often a
   different computer.

Search fuses FTS5 and cosine results with RRF and returns path, line range or page and a snippet.
Without an embedding model, search is keyword-only and the UI says so.

## Real-time UI

`EventBus` publishes events (node connected, telemetry, job updates, memory/knowledge changes,
approvals, settings). `GET /api/v1/events` streams them to every signed-in client as SSE, so a
conversation started on the phone appears on the desktop, and an approval request can be answered
from either.

## Hardware detection

* **NVIDIA:** `nvidia-smi --query-gpu=…` CSV (Linux and Windows): name, total/used/free VRAM,
  utilisation, temperature, driver. Total VRAM is what the driver reports, never assumed from the model
  name.
* **AMD/Intel on Linux:** sysfs (`mem_info_vram_total`/`used`, `gpu_busy_percent`).
* **Windows fallback:** CIM `Win32_VideoController` plus the driver's `HardwareInformation.qwMemorySize`
  registry value, because `AdapterRAM` is a 32-bit field that caps at 4 GB.
* CPU/RAM via psutil. Bazzite and other immutable OSes are detected from `/etc/os-release` and
  `/run/ostree-booted`.
* Fields that can't be measured are reported as unavailable with the reason, not filled with guesses.

## Key decisions and deviations

| Decision | Why |
|---|---|
| One Python package instead of separate `packages/*` distributions | One install, one CLI, one service per computer; boundaries are enforced by tests instead of packaging. |
| Workers connect outbound over WebSocket | No inbound ports on workers; works through NAT and VPNs; one authenticated, multiplexed channel for jobs, telemetry and control. |
| SQLite rather than PostgreSQL | Zero-administration for a home setup, transactional, FTS5 built in, and backed up with the online backup API. The repository layer keeps a migration path open. |
| Exact numpy vector search | Correct and dependency-free at home scale; there's a documented upgrade path. |
| Mock backend kept in the product | Lets the full stack be tested without GPUs, but it's opt-in, labelled everywhere, and stored as `mock=true` on every message it produces. |
| No shell tool | Its risk and approval fatigue outweigh its value at this stage. |
| Splitting one model across computers is not implemented | Ollama doesn't support it; llama.cpp RPC is experimental, LAN bandwidth makes it slow, and it's insecure without extra work. See [docs/DISTRIBUTED_INFERENCE.md](docs/DISTRIBUTED_INFERENCE.md). |
