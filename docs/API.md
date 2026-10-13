# HTTP API (v1)

All endpoints are under `/api/v1`. The coordinator serves interactive OpenAPI documentation at
`/api/docs` (schema: `/api/openapi.json`; it describes endpoints only, no data). That's the
authoritative reference for request and response bodies; this page explains the conventions and the
streaming endpoints.

## Authentication

| Client | How |
|---|---|
| Browser | `POST /auth/login {username, password}` sets the `hm_session` cookie (HttpOnly, SameSite=Strict). Every non-GET request must also send `X-HomeMesh-CSRF: 1`. |
| Scripts | `Authorization: Bearer hm_…` with an API token from Settings → Security (`POST /auth/tokens`). Tokens are shown once. |
| CLI on the coordinator | The local admin token in `data/secrets/cli-admin.token` (used automatically). |
| Workers | Not this API: they use the WebSocket protocol ([PROTOCOL.md](PROTOCOL.md)). |

**Scopes:** `read` (view status, nodes, models, jobs, settings), `chat` (conversations, messages, memory,
knowledge search, attachments, approvals for your own runs), `admin` (everything). Some actions are
**owner-only** (owner session or an `admin` token): pairing/revoking computers, changing settings and
model roles, tool policies, knowledge collections, backups, the audit log.

**Errors** use one JSON shape with a stable `code`:

```json
{ "error": { "code": "insufficient_memory", "message": "…", "hint": "…", "details": { … } } }
```

`401` not signed in / bad token, `403` missing scope or owner-only, `404`, `409` conflict, `413` body too
large, `422` validation (`validation_failed`), `429` rate-limited (with `Retry-After`), `503` no capacity
or web UI not built.

## Endpoints

| Method & path | Scope | Purpose |
|---|---|---|
| `GET /system/health` | none | Liveness: version, protocol version, DB status, whether setup is needed, TLS mode. No personal data. |
| `GET /system/status` | read | Dashboard: coordinator info, nodes, models, job counts, warnings. |
| `GET /system/metrics` | read | Latency and throughput statistics (TTFT, tokens/s, load, dispatch overhead, retrieval). |
| `GET /system/audit` | owner | Audit log (`?action=`, `?limit=`). |
| `GET /tls/ca` | none | The local CA certificate (PEM) used for pinning. |
| `GET /auth/status` · `POST /auth/setup` · `POST /auth/login` · `POST /auth/logout` · `GET /auth/me` | — | First-run setup (needs the setup token), sign-in and out. |
| `POST /auth/password` · `GET/DELETE /auth/sessions[/{id}]` · `GET/POST/DELETE /auth/tokens[/{id}]` | owner | Password, sessions, API tokens. |
| `GET /nodes` · `GET /nodes/{id}` | read | Computers with hardware, live telemetry, models, mode, running jobs. |
| `PATCH /nodes/{id}` | owner | Rename, mode, scheduling weight. |
| `POST /nodes/pairing` · `GET /nodes/pairing/list` · `DELETE /nodes/pairing/{id}` | owner | One-time pairing codes. |
| `POST /nodes/enroll` | pairing code | Used by `homemesh worker join` (rate-limited). |
| `POST /nodes/{id}/revoke` · `/rotate-credential` · `/refresh` | owner/admin | Revoke, rotate a node's secret, re-list its models. |
| `GET /models` | read | Models grouped by name with instances per node/backend, capabilities, roles, backends. |
| `PUT /models/roles` | owner | Set the model per role (returns compatibility warnings). |
| `POST /models/manage` | owner | `{action: pull/delete/load/unload, node_id, backend, model}` → a job id. |
| `GET/POST /conversations` · `GET/PATCH/DELETE /conversations/{id}` · `GET /conversations/search?q=` · `GET /conversations/{id}/export` | chat | Conversations; export downloads the conversation with all messages as JSON. |
| `POST /conversations/{id}/messages` | chat | **Send a message → SSE stream** (below). |
| `GET /chat/runs/{message_id}/events` | chat | Re-attach to a running (or just finished) reply. |
| `POST /chat/runs/{message_id}/cancel` | chat | Stop a reply; the partial text is kept. |
| `GET/POST /projects` · `PATCH/DELETE /projects/{id}` | chat | Projects scope conversations and memories. |
| `GET/POST /memories` · `GET/PATCH/DELETE /memories/{id}` · `POST /memories/{id}/approve` · `/reject` | chat | Memory management and candidate review. |
| `GET /knowledge/collections` · `GET /knowledge/collections/{id}` | chat | Collections with counts and status. |
| `POST /knowledge/collections` · `DELETE …/{id}` · `POST …/{id}/scan` · `POST …/{id}/embed` | owner | Create (folder on a node), delete, rescan, embed missing chunks. |
| `POST /knowledge/search` | chat | `{query, limit, collection_ids?}` → results with path, lines/page, snippet, score, `mode` (`hybrid` or `keyword`). |
| `GET /jobs` · `GET /jobs/{id}` | read | Jobs with status, node, model, metrics, events and the scheduling decision. |
| `POST /jobs/{id}/cancel` | chat | Cancel a job. |
| `GET /tools` · `GET /tools/executions` · `GET /tools/approvals` | read | Tools, policies, history, pending approvals. |
| `PUT /tools/{name}/policy` | owner | `{enabled, approval: "default"\|"always"}`. |
| `POST /tools/approvals/{id}` | chat | `{approved: true\|false, reason?}`. |
| `GET /settings` · `PATCH /settings/{section}` · `GET /settings/machine/config` | read / owner | In-app settings ([CONFIGURATION.md](../CONFIGURATION.md)); the coordinator's `homemesh.toml` values (read-only; the file holds no secrets, API keys are referenced by environment-variable name). |
| `GET /events` | read | **SSE stream** of live events for the UI. |
| `POST /attachments` · `GET /attachments/{id}` | chat | Upload and download images for vision models (PNG, JPEG, GIF, WebP, recognised by content, not file name; max `max_upload_mb`). |
| `GET/POST /backups` · `GET /backups/{name}` | owner | List, create and download backups. |

## Sending a message (server-sent events)

```bash
curl -N -H "Authorization: Bearer $HM_TOKEN" -H "Content-Type: application/json" \
     -d '{"content": "What did I say my wifi is called?"}' \
     https://desktop.local:8765/api/v1/conversations/$CONV/messages --cacert ca.pem
```

Request body: `content`, optional `model` and `node_id` (pin; never silently re-routed),
`regenerate_of` (message id), `attachment_ids[]`, `use_memory`, `use_knowledge`, `collection_ids[]`,
`use_tools`, `temperature`, `max_output_tokens`, `think`.

The response is `text/event-stream`. Each event is `event: <name>` plus `data: <json>`:

| event | data |
|---|---|
| `meta` | `conversation_id, user_message_id, message_id` |
| `route` | `model, node_id, node_name, backend, backend_type, placement` (`loaded`/`gpu`/`partial`/`cpu`/`unknown`), `mock`, `manual_model`, `manual_node`, `decision` (candidates and per-node rejection reasons). Sent again with `rerouted: true` if a worker rejected the job before starting and another took it. |
| `context` | memories and sources used, history messages included, tools offered, token budget |
| `delta` | `{text}` (append to the reply) |
| `thinking` | `{text}` (reasoning models, if shown) |
| `tool_call` | `{name, arguments, round}` |
| `approval_required` | `{approval_id, tool, description, arguments, side_effect, node_id, node_name, expires_at, conversation_id}`, plus when present `site` (destination host of a link), `reason` (why a read tool asks, e.g. `fetch_url` for a link found on a page) and `note` (e.g. `open_url` to a local-network address). `arguments` are the validated, normalised values that will run. The same details are stored with the approval (`details_json` in `GET /tools/approvals`). Answer with `POST /tools/approvals/{id}` from any device. |
| `tool_result` | `{name, ok, result: {ok, output, error}}` |
| `memory_saved` | `{memory_id, content, outcome}` when "remember that…" was detected |
| `done` | `{message_id, status, metrics, model, node_name, mock}` |
| `error` | `{code, message, hint?, details?, message_id}`, e.g. `no_eligible_worker` with per-node reasons in `details` |
| `cancelled` | `{message_id}` |

The run continues if the client disconnects. Re-attach with `GET /chat/runs/{message_id}/events`,
which replays what has happened so far and then streams the rest.

## Live events

`GET /events` streams `node.connected`, `node.disconnected`, `node.enrolled`, `node.revoked`,
`node.updated`, `node.telemetry`, `node.models`, `job.created`, `job.updated`, `job.progress`,
`conversation.created`, `conversation.updated`, `conversation.deleted`, `project.changed`,
`memory.changed`, `knowledge.changed`, `approval.requested`, `approval.decided`, `approval.expired`
and `settings.updated`. Events carry ids; clients re-fetch what they display. A `: keep-alive` comment
is sent periodically so proxies don't drop idle streams.

## Versioning

The API is versioned in the path (`/api/v1`). Within v1, fields may be added but not removed or
changed in meaning.
