# Worker ↔ coordinator protocol, version 1

Source of truth: `src/homemesh/protocol/messages.py` (envelopes) and `src/homemesh/protocol/jobs.py`
(job payloads/results). Both are pydantic models; anything that fails validation is rejected.

## Transport and authentication

* The **worker connects outbound** to `wss://<coordinator>/api/v1/worker/ws` (`ws://` only on
  loopback or with an explicit insecure opt-in). Workers listen on no port.
* Headers:
  * `Authorization: HomeMesh-Node <node_id>:<node_secret>`: the credential from enrollment. The
    coordinator stores only a hash of the secret.
  * `X-HomeMesh-Protocol: 1`: the protocol versions the worker speaks (comma-separated).
* TLS: the worker verifies the coordinator against the **pinned CA** saved at enrollment (or system
  trust with `--system-trust`). Hostname verification is on.
* Frames are JSON text, max 16 MiB. WebSocket ping/pong keeps the connection alive (20 s interval).
* Proxies from the environment are ignored (`proxy=None`).

### Enrollment (HTTP, before the first connection)

1. `GET /api/v1/tls/ca` → the CA PEM. The worker computes its SHA-256 fingerprint and compares it to
   the one the user typed; on mismatch it aborts.
2. `POST /api/v1/nodes/enroll` `{code, name}` → `{node_id, node_name, node_secret,
   protocol_versions}`. Pairing codes are single-use, expire after 10 minutes, are stored hashed and
   enrollment is rate-limited per IP. The worker stores the credential, the pinned CA and the
   coordinator URL in its data folder (owner-only permissions).

## Envelope

Every message carries:

```json
{ "v": 1, "type": "heartbeat", "id": "msg_…", "ts": "2026-10-12T21:00:00.000Z", … }
```

`type` selects the schema (a discriminated union). A message with an unknown type or invalid fields
is ignored, logged, and answered with an `error` message (`invalid_message`).

## Handshake

```
worker                                   coordinator
  │── (WebSocket upgrade + Authorization) ──►│  401-style close 4401 if the credential is wrong,
  │                                          │  4403 if the node is revoked
  │── hello ────────────────────────────────►│  protocol_versions ∩ supported = ∅ → close 4406
  │◄──────────────────────────────── welcome ─│  (an older connection of the same node is closed with 4409)
  │── heartbeat (every heartbeat_interval_s) ►│
```

**hello** (worker → coordinator): `node_id, node_name, software_version, protocol_versions[],
hardware` (CPU, RAM, GPUs with vendor/name/VRAM and the detection source, OS info), `backends[]`
(name, type, url, ok, version, error), `models[]` (name, backend, capabilities, sizes, context length,
loaded/VRAM state, remote/mock flags), `task_types[]`, `mode`, `limits` (max concurrent jobs, CPU
allowed, RAM/VRAM reserves, GPU-busy threshold), `allowed_roots[]`, `allowed_tools[]`,
`allow_remote_mode_change`, `running_jobs[]` (jobs still running from a previous connection),
`embedded`.

**welcome** (coordinator → worker): `protocol_version` (chosen), `node_id`, `node_name`,
`coordinator_version`, `heartbeat_interval_s`, `mode` (the coordinator's stored mode, which overrides the
worker default when remote changes are allowed), `server_time`, `trusted_roots` (embedded worker only).

## Messages

### Worker → coordinator

| type | fields | meaning |
|---|---|---|
| `heartbeat` | `seq, telemetry, running_jobs[], mode` | Every `heartbeat_interval_s` (10 s). Telemetry: CPU %, RAM total/available, per-GPU VRAM used/free, utilisation, temperature, and whether *other* processes keep the GPU busy. Missing heartbeats for `heartbeat_timeout_s` (35 s) → node offline. |
| `capabilities.update` | `backends[], models[]` | Model or backend changes (after pull/delete/load/unload, or periodic refresh). |
| `job.accepted` | `job_id` | Within `dispatch_ack_timeout_s` of `job.dispatch`. |
| `job.rejected` | `job_id, code, reason` | Local policy refused: `paused`, `busy`, `capacity`, `unsupported`, `invalid_payload`, `path_not_allowed`, `tool_not_allowed`. |
| `job.progress` | `job_id, progress?, message?, data?` | e.g. download percentage, scan counts. |
| `job.output` | `job_id, seq, kind, delta?, data?` | Streaming output: `content`, `thinking`, `tool_calls` (chat) or `document` (a scanned document with its chunks, possibly split into parts). `seq` increases monotonically per job. |
| `job.completed` | `job_id, result, metrics?` | `result` follows the job type's result schema. |
| `job.failed` | `job_id, code, error, retryable, partial?` | e.g. `backend_unreachable`, `backend_timeout`, `model_not_found`, `insufficient_memory` (backend OOM), `timeout`. |
| `job.cancelled` | `job_id, partial?` | Acknowledges `job.cancel`; partial output kept. |
| `credential.rotated` | `rotation_id` | The new secret has been saved; the coordinator then revokes older secrets. |
| `pong` | `ping_id` | Reply to `ping`. |
| `shutdown` | `reason` | Worker is stopping gracefully. |

### Coordinator → worker

| type | fields | meaning |
|---|---|---|
| `job.dispatch` | `job_id, job_type, priority, payload, timeout_s` | Run a job. `priority`: `interactive`, `normal`, `background`. The worker enforces `timeout_s` too. |
| `job.cancel` | `job_id, reason` | Stop the job; the worker aborts the backend request. |
| `credential.rotate` | `rotation_id, new_secret` | Save the new secret, then confirm with `credential.rotated`. |
| `node.config` | `mode?, trusted_roots?` | Change mode from the UI (ignored if the worker disallows remote mode changes). |
| `models.refresh` | | Re-list models now. |
| `ping` | | Liveness / latency check. |
| `shutdown` | `reason, reconnect_after_s` | Coordinator stopping; reconnect later. |
| `error` | `code, message, fatal` | Protocol-level error. |

**Authorisation of job messages:** the coordinator processes `job.*` messages only from the node
the job is currently assigned to. Anything else is dropped and logged.

## Job types

| `job_type` | payload | result | notes |
|---|---|---|---|
| `chat` | `model, backend, messages[] {role, content, images?, tool_calls?, tool_name?}, options {temperature, top_p, top_k, num_ctx, num_predict, seed, stop, repeat_penalty}, tools[]?, think?, keep_alive?` | `content, thinking, tool_calls[], finish_reason, model, backend, backend_type, mock, metrics` | Streams `job.output`. **Never retried automatically.** |
| `embed` | `model, backend, inputs[] (≤512), refs[]?` | `model, backend, dim, embeddings[][], refs, mock, metrics` | Idempotent → requeued on worker loss. |
| `knowledge.scan` | `collection_id, root_path, include_globs[], exclude_dirs[], known{rel_path: {size, mtime, content_hash}}, chunk_chars, chunk_overlap, max_file_mb` | `collection_id, seen[], stats, duration_ms` | Runs on the node that owns the folder; checks its own allowed roots. Streams `document` outputs. Idempotent. |
| `tool.exec` | `tool, arguments, approval_id?` | `ok, output, error` | Re-validated against the worker's `allowed_tools` and paths. |
| `model.manage` | `action (pull, delete, load, unload, refresh), backend, model?, keep_alive?` | `action, model, ok, message` | `pull` streams progress. |

`GenerationMetrics` (on chat/embed results): `ttft_ms` and `total_ms` (timed by the worker),
`load_ms`, `prompt_tokens`, `completion_tokens`, `prompt_eval_ms`, `eval_ms`, `tokens_per_second`
(from the backend's own counters where it reports them), and `dispatch_overhead_ms` (dispatch → ack,
added by the coordinator). Fields a backend doesn't report are `null`, never estimated.

## Timeouts and failure semantics

| Situation | Behaviour |
|---|---|
| No `job.accepted` within `dispatch_ack_timeout_s` (15 s) | `worker_unresponsive`: idempotent jobs are requeued away from that node; others fail. |
| `job.rejected` before the job started | Placed again on another node (safe even for chat, since nothing ran), unless the user pinned that node. |
| Sending the dispatch fails (node just went away) | `dispatch_failed`. |
| Job exceeds `timeout_s` | Worker aborts and reports `job.failed` code `timeout`; the coordinator enforces the same limit. |
| WebSocket closes / heartbeat timeout | Node offline; non-idempotent jobs fail `worker_disconnected`; idempotent ones are requeued (bounded attempts). |
| Worker reconnects with `running_jobs` | Jobs the coordinator still considers active are kept; unknown ones are cancelled. |
| Coordinator restarts | Workers reconnect with exponential back-off (max `reconnect_max_delay_s`, jitter). Interrupted chat jobs are marked `interrupted`. |

## Close codes

| code | meaning | worker reaction |
|---|---|---|
| 4401 | unauthorized (bad credential) | stop retrying; tell the user to re-join |
| 4403 | node revoked | stop; tell the user |
| 4406 | protocol incompatible | stop; tell the user to upgrade |
| 4409 | superseded by a newer connection of the same node | stop (another process uses this identity) |
| 4503 | coordinator shutting down | reconnect with back-off |

## Versioning

`PROTOCOL_VERSION = 1` (`homemesh/version.py`). Additive changes (new optional fields, new message
types a peer may ignore) keep the version. Breaking changes increment it; a coordinator lists every
version it supports in `SUPPORTED_PROTOCOL_VERSIONS`, so a new coordinator can keep serving old workers
during an upgrade. `GET /api/v1/system/health` exposes the coordinator's protocol version, so
`homemesh status` on a worker can report a mismatch before connecting.
