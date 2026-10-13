# Configuration

HomeMesh has two kinds of settings:

| | Where | Edited with | Examples |
|---|---|---|---|
| **Machine settings** (per computer) | `homemesh.toml` | `homemesh setup`, a text editor, environment variables | roles, port, TLS, backends, worker limits, allowed folders |
| **Your settings** (shared by all your devices) | coordinator database | web UI → Settings / Models / Memory, or the API | model roles, assistant instructions, memory behaviour, tool permissions |

`homemesh config path` prints the file location, `homemesh config show` prints the effective
configuration (file plus environment overrides) and `homemesh config validate` checks it. Unknown
keys are errors, so typos don't silently fall back to defaults. Restart HomeMesh after editing
(`homemesh restart`).

Default locations: Linux `~/.config/homemesh/homemesh.toml`, Windows
`%APPDATA%\HomeMesh\homemesh.toml`, macOS `~/Library/Application Support/HomeMesh/`. With
`HOMEMESH_HOME=/dir` (or `homemesh --home /dir`), config and data live together in `/dir`.

## Example: standalone (one computer)

```toml
[node]
name = "desktop"
roles = ["coordinator", "worker"]

[server]
host = "127.0.0.1"
port = 8765

[[inference.backends]]
name = "ollama"
type = "ollama"
url = "http://127.0.0.1:11434"
```

## Example: coordinator on the LAN plus a GPU worker

Coordinator (`desktop`, always on):

```toml
[node]
roles = ["coordinator", "worker"]

[server]
host = "0.0.0.0"
tls_mode = "self_signed"
tls_extra_sans = ["192.168.1.10", "desktop.local"]

[worker]
mode = "background"           # the coordinator's own worker only helps when needed
```

Gaming PC worker (written by `homemesh worker join`, then adjusted):

```toml
[node]
name = "gaming-pc"
roles = ["worker"]

[worker]
mode = "balanced"
max_concurrent_jobs = 2
vram_reserve_percent = 15
gpu_busy_threshold_percent = 60      # back off sooner while gaming
allowed_roots = ["D:/Documents/Notes"]
```

## `[node]`

| Key | Default | Meaning |
|---|---|---|
| `name` | host name | Display name of this computer. |
| `roles` | `["coordinator", "worker"]` | Any of `coordinator`, `worker`, `client`. |

## `[server]` (coordinator)

| Key | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Listen address. Anything other than loopback **requires TLS** (see `allow_insecure_lan`). |
| `port` | `8765` | Listen port. |
| `public_url` | (derived) | The URL other devices use; shown in pairing instructions. Set it when behind NAT/proxy or using a DNS name. |
| `tls_mode` | `off` | `off`, `self_signed` (local CA created by `homemesh tls init`), `custom` (your cert/key), `proxy` (TLS terminated by a reverse proxy; requires `host` loopback). |
| `tls_cert_file`, `tls_key_file` | | For `tls_mode = "custom"`. |
| `tls_extra_sans` | `[]` | Extra names/IPs for the self-signed server certificate (the host name, `localhost` and detected LAN IP are included automatically). |
| `allow_insecure_lan` | `false` | Allow plain HTTP on a non-loopback address. Only for trusted, isolated networks or containers whose published port is loopback-only. |
| `max_request_mb` | `10` | Maximum JSON request body. |
| `max_upload_mb` | `25` | Maximum attachment upload. |
| `session_ttl_hours` | `168` | Browser session lifetime. |
| `trusted_proxies` | `[]` | IPs whose `X-Forwarded-For`/`-Proto` headers are trusted (reverse proxy). |
| `discovery_enabled` | `false` | Answer LAN discovery probes (UDP) so `homemesh worker discover` can find this coordinator. Discovery is untrusted convenience only. |
| `discovery_port` | `47781` | UDP port for discovery. |
| `queue_timeout_s` | `30` | How long an interactive request waits for a free computer before failing with a clear message. |
| `heartbeat_timeout_s` | `35` | A worker with no heartbeat for this long is marked offline. |
| `dispatch_ack_timeout_s` | `15` | A worker must accept or reject a dispatched job within this time. |
| `web_dist_dir` | (bundled) | Serve the web UI from another folder (development). |
| `login_attempts_per_minute` | `10` | Per-IP rate limit for login, setup and enrollment. |
| `max_queued_jobs` | `5000` | Upper bound on queued background jobs. |

## `[worker]`

| Key | Default | Meaning |
|---|---|---|
| `coordinator_url` | `http://127.0.0.1:8765` | Set by `worker join`. |
| `mode` | `balanced` | Initial mode: `performance`, `balanced`, `background`, `paused`. The coordinator remembers changes made in the UI. |
| `max_concurrent_jobs` | `2` | Upper limit; modes cap it further (balanced 2, background 1, paused 0). |
| `allow_cpu_inference` | `true` | Allow running models (partly) on CPU when they don't fit in VRAM. |
| `ram_reserve_mb` | `2048` | RAM always left free for you (the mode's minimum applies if larger). |
| `vram_reserve_percent` | `10` | VRAM always left free (balanced ≥10%, background ≥25%). |
| `gpu_busy_threshold_percent` | `85` | GPU utilisation by *other* programs above which balanced/background workers take no new GPU work. |
| `task_types` | all | Subset of `chat`, `embed`, `knowledge.scan`, `tool.exec`, `model.manage`. |
| `allowed_roots` | `[]` | Folders this computer allows to be indexed or read by tools. Empty = none. Sensitive locations (`.ssh`, `.gnupg`, browser profiles, password stores, `.env` files, private keys) are refused even inside allowed folders. Manage with `homemesh worker allow-path` / `disallow-path`. |
| `ignore_gpus` | `[]` | GPUs HomeMesh should not count when scheduling, by part of their name, e.g. `["GTX 745"]`: for a card your model server doesn't use (hide it from Ollama too, e.g. `CUDA_VISIBLE_DEVICES=-1`). They are still shown, marked "ignored for scheduling". |
| `allowed_tools` | `["system_info", "list_directory", "read_file"]` | Tools this computer agrees to run. `write_file`, `web_search`, `fetch_url` and `open_url` must be added explicitly (`homemesh worker allow-tool <name>`); `write_file` and `open_url` still need your approval per use. See [docs/WEB_ACCESS.md](docs/WEB_ACCESS.md) for the internet tools. |
| `heartbeat_interval_s` | `10` | |
| `allow_remote_mode_change` | `true` | Let the coordinator UI change this computer's mode. |
| `tls_ca_file` | | Set by `worker join` (the pinned CA). |
| `reconnect_max_delay_s` | `60` | Reconnect back-off ceiling (exponential with jitter). |
| `reconnect_max_attempts` | `0` | 0 = retry forever. |

## `[inference]`

| Key | Default | Meaning |
|---|---|---|
| `backends` | Ollama at `127.0.0.1:11434` | List of `[[inference.backends]]` (below). |
| `mock_enabled` | `false` | Adds the **MOCK** backend for testing. Every mock response is labelled. Never enable for real use. |
| `allow_model_downloads` | `true` | Allow downloading models from the Models page / `homemesh model pull`. |
| `default_num_ctx` | `8192` | Context window requested from backends (capped by the model's own limit). |
| `max_output_tokens` | `1024` | Maximum tokens per reply. |
| `request_timeout_s` | `600` | Hard limit for one generation. |
| `keep_alive` | `10m` | How long Ollama keeps a model loaded after use. |

### `[[inference.backends]]`

| Key | Meaning |
|---|---|
| `name` | Unique name, e.g. `ollama`, `llamacpp`. |
| `type` | `ollama`, `openai_compat` (llama.cpp `llama-server`, vLLM, LM Studio, …) or `mock`. |
| `url` | Base URL, e.g. `http://127.0.0.1:11434` or `http://127.0.0.1:8080` (OpenAI-compatible servers: `/v1` is added if missing). |
| `enabled` | `true`/`false`. |
| `api_key_env` | Name of an environment variable holding an API key (for servers started with `--api-key`). The key itself is never stored in the config. |
| `declared_models` | **Required for OpenAI-compatible servers**: they don't report capabilities, so declare them: `{ "qwen2.5-7b-instruct" = ["completion", "tools"] }`. HomeMesh never guesses capabilities from a name. Valid capabilities: `completion`, `tools`, `vision`, `embedding`, `thinking`. |
| `declared_memory_mb` | Optional approximate memory need per declared model, used for scheduling. |
| `timeout_s` | Request timeout. |
| `allow_remote_models` | Ollama can proxy *cloud* models (they show a remote host). They are hidden unless this **and** `[cloud] enabled` are true. |

llama.cpp example:

```toml
[[inference.backends]]
name = "llamacpp"
type = "openai_compat"
url = "http://127.0.0.1:8080"
declared_models = { "qwen2.5-7b-instruct-q4_k_m" = ["completion", "tools"] }
declared_memory_mb = { "qwen2.5-7b-instruct-q4_k_m" = 6000 }
```

vLLM is the same with its URL (default port 8000) and served model name.

## `[knowledge]`

| Key | Default | Meaning |
|---|---|---|
| `max_file_mb` | `50` | Larger files are skipped (reported as such). |
| `chunk_chars` | `2000` | Target chunk size. |
| `chunk_overlap` | `200` | Overlap between consecutive chunks. |
| `embed_batch_size` | `32` | Chunks per embedding request. |
| `exclude_dirs` | `.git`, `node_modules`, `__pycache__`, `.venv`, … | Folder names never scanned. Hidden folders and files are always skipped. |

## `[logging]`

| Key | Default | Meaning |
|---|---|---|
| `level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `format` | `text` | `text` or `json`. |
| `to_file` | `true` | Also write `data/logs/homemesh.log` (rotated). Message contents are never logged. |

## `[web]`

Settings for the internet tools on this computer (they only run where `worker.allowed_tools` lists
them). Set-up and safety model: [docs/WEB_ACCESS.md](docs/WEB_ACCESS.md).

| Key | Default | Meaning |
|---|---|---|
| `searxng_url` | `""` | Your own SearXNG for `web_search`, e.g. `http://127.0.0.1:8888` (`homemesh web set-searxng …`). Empty = web search not set up here. |
| `safesearch` | `1` | `0` off, `1` moderate, `2` strict. |
| `search_language` | `auto` | Search language (`en`, `de`, `all`, …). |
| `max_results` | `8` | Results per search (1-20). |
| `timeout_s` | `20` | Per search request, and per page or redirect for `fetch_url` (one deadline each, 1-30). A whole search stops after 40 s, a whole `fetch_url` call after 60 s. |
| `max_download_mb` | `5` | Pages are cut off beyond this (marked `truncated`). |
| `extra_ca_file` | `""` | Extra CA certificate (PEM) to trust for `fetch_url`, e.g. antivirus HTTPS inspection. Mozilla's list (certifi) stays in use; the Windows certificate store and `SSL_CERT_FILE` are not used. |
| `max_redirects` | `5` | Each redirect is re-checked against the private-address rules. |

## `[cloud]`

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch for anything that would send prompts outside your network. Off by default; v0.1.0 ships no cloud backend. It only gates Ollama's remote models. |

## Environment variables

* `HOMEMESH_HOME`: one folder for config and data.
* `HOMEMESH__SECTION__KEY=value`: override any key, e.g. `HOMEMESH__SERVER__PORT=9000`,
  `HOMEMESH__WORKER__MODE=background`. Values are parsed as JSON when possible
  (`HOMEMESH__SERVER__TLS_EXTRA_SANS='["10.0.0.5"]'`).
* Plain-text shortcuts for containers and systemd units:
  `HOMEMESH_OLLAMA_URL=http://ollama:11434` (replaces the backend list with that Ollama),
  `HOMEMESH_TLS_SANS=192.168.1.10,server.local`, `HOMEMESH_SEARXNG_URL=http://127.0.0.1:8888`, and
  `HOMEMESH_MOCK=1` (tests only).
* `HOMEMESH_MOCK_DELAY`: seconds between mock tokens (tests).

## In-app settings (stored in the database)

Edited in the web UI (Settings, Models, Memory pages) or with `PATCH /api/v1/settings/{section}`.
Changes apply immediately on every device.

| Section | Keys (defaults) |
|---|---|
| `assistant` | `name` ("HomeMesh"), `custom_instructions`, `response_style` (`concise` / **`balanced`** / `detailed`) |
| `user_profile` | `preferred_name`, `about`, `timezone`: given to the model as trusted owner preferences |
| `model_roles` | model name per role: `chat`, `reasoning`, `coding`, `fast`, `embedding`, `vision` (`stt`, `tts` reserved); empty = automatic |
| `generation` | `temperature` (0.7), `top_p`, `max_output_tokens` (1024), `num_ctx`, `think` |
| `memory` | `enabled` (on), `retrieval_limit` (6), `detect_explicit` ("remember that…", on), `auto_extract` (suggestions needing approval, **off**), `default_retention_days` |
| `knowledge` | `enabled` (on), `retrieval_limit` (5), `min_score` |
| `tools` | `enabled` (on), `max_rounds` (5), `approval_timeout_s` (300) |
| `retention` | `jobs_days` (30), `audit_days` (180), `archived_conversations_days` |

Per-tool policies (enabled, approval) are set on Settings → Tools. API tokens (scopes `read`,
`chat`, `admin`), sessions and the password are under Settings → Security.
