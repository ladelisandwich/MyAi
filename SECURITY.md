# Security

HomeMesh holds some of the most personal data you have: conversations, memories and an index of
your documents. It also controls programs (model servers) on several computers. This document
explains what HomeMesh protects against, how, and where its limits are.

**Reporting a vulnerability:** please open a private security advisory on the GitHub repository
rather than a public issue.

## Threat model

**In scope (defended against):**

* Other devices and people on your home network (guests, smart TVs, a compromised IoT device) trying
  to read your data, use your models, impersonate a coordinator or join as a worker.
* A stolen or replayed pairing code, worker credential, session cookie or API token.
* Malicious web pages you visit trying to act on HomeMesh through your browser (CSRF, clickjacking, XSS).
* **Prompt injection** from documents, memories, web content or model output trying to make HomeMesh
  read or change files or escalate permissions.
* Accidental exposure through unsafe configuration (for example binding to the LAN without TLS).
* Data loss from crashes, failed upgrades or restores.

**Out of scope:**

* Someone with administrator/root access or your user account on the coordinator computer. They can
  read the database directly. The database is **not encrypted at rest**; use full-disk encryption
  (BitLocker, LUKS, FileVault) on computers that hold it.
* A malicious model file or model server. HomeMesh treats model output as untrusted text, but the
  backends (Ollama, llama.cpp, vLLM) themselves are trusted software.
* Exposing HomeMesh directly to the internet. Don't. Use a VPN (WireGuard, Tailscale) for remote access.
* Multiple mutually distrusting users. v0.1.0 has a single owner account; the schema carries `user_id`
  everywhere for future multi-user support, but isolation between users is not implemented.

## Network exposure

| Component | Default | Notes |
|---|---|---|
| Coordinator API/UI | `127.0.0.1:8765` | Binding to any other address **requires TLS**; the coordinator refuses to start otherwise unless you set `allow_insecure_lan = true` (warned about in logs and the UI). `tls_mode = "proxy"` additionally requires loopback binding. |
| Worker | no listening port | Workers make **outbound** WebSocket connections to the coordinator. |
| Ollama / llama.cpp / vLLM | `127.0.0.1` | HomeMesh only talks to backends on the same computer; nothing is proxied to the network. The container examples keep Ollama on an internal network or loopback. **Never** publish a backend port; they have no authentication. |
| LAN discovery | off | Optional UDP responder (`discovery_enabled`). It reveals the coordinator's name, URL, version and TLS fingerprint to the local subnet, and its answers are not trusted by workers (see below). |
| Telemetry | none | HomeMesh makes no outbound connections except: workers → coordinator, backends on localhost, and model downloads you start. FastAPI/OpenTelemetry instrumentation is disabled explicitly. |

Outgoing HTTP clients ignore proxy environment variables when talking to local backends and the
coordinator (`trust_env=False`), so a system proxy can't intercept that traffic.

## TLS and coordinator identity

* `homemesh tls init` creates a **local CA** (EC P-256) and a server certificate signed by it with
  the coordinator's host names and IPs as SANs. Private keys are stored with owner-only permissions in
  `data/tls/`.
* The pairing screen shows the CA's **SHA-256 fingerprint**. `homemesh worker join --fingerprint …`
  downloads the CA, compares fingerprints (**aborting on mismatch**, with an impersonation warning),
  stores it, and from then on uses normal certificate *and hostname* verification against that CA only.
  TLS verification is never disabled.
* Behind a reverse proxy with a publicly trusted certificate, workers use `--system-trust` instead.
* HSTS is sent when TLS is on.

## Owner account and sessions

* One owner account. First-run setup in the browser requires a **one-time setup token** printed to
  the console and saved in `data/secrets/setup-token.txt`; `homemesh setup` can create the owner from
  the terminal instead. Both stop working once an owner exists.
* Passwords: at least 10 characters, hashed with **argon2id** (argon2-cffi defaults).
* Sessions: 256-bit random tokens, stored **hashed** (SHA-256) on the server, sent as an
  `HttpOnly`, `SameSite=Strict` cookie (`Secure` under TLS), expiring after `session_ttl_hours`. You
  can see and revoke sessions in Settings → Security. Changing the password revokes other sessions.
* **CSRF:** every state-changing request authenticated by cookie must carry `X-HomeMesh-CSRF: 1`,
  which a cross-site form can't set and a cross-origin script can't send without a CORS preflight
  (no CORS is enabled).
* **API tokens** for scripts: scoped `read`, `chat` or `admin`, shown once, stored hashed, revocable.
  The CLI on the coordinator uses a local admin token in `data/secrets/cli-admin.token` (owner-only file).
* **Rate limiting:** login (per IP and per username, `login_attempts_per_minute`), first-run setup and
  worker enrollment (10/minute per IP).
* Request size limits (`max_request_mb`, `max_upload_mb`).
* Security headers on every response: a strict **Content-Security-Policy** (`script-src 'self'`, no
  inline scripts, `frame-ancestors 'none'`, `connect-src 'self'`), `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy`.
* Model output and documents are rendered as Markdown and sanitised with **DOMPurify**; raw HTML from
  models never executes.

## Worker pairing and credentials

1. The owner creates a pairing code (`homemesh node pair` or the UI): 12 characters from an
   unambiguous 32-symbol alphabet (60 bits), **stored hashed**, **single-use**, valid for **10 minutes**,
   revocable before use. Enrollment attempts are rate-limited and audited.
2. The worker enrolls over TLS (pinned as above) and receives a node id plus a random 256-bit
   **node secret**, stored owner-only on the worker; the coordinator keeps only its hash.
3. Every WebSocket connection authenticates with `Authorization: HomeMesh-Node <id>:<secret>`.
   Failures close with code 4401; a **revoked** node gets 4403 and its live connection is closed
   immediately. A second connection with the same identity supersedes the first (4409).
4. Credentials can be **rotated** from the UI while the node is online (`credential.rotate`); once
   the worker confirms it saved the new secret, all older secrets for that node are revoked.
5. The coordinator accepts job messages for a job **only from the node it dispatched that job to**.

Plain-HTTP enrollment to a non-loopback coordinator is refused unless `--allow-insecure-http` is given.

## Files, tools and prompt injection

* **Nothing is readable by default.** Each worker has its own `allowed_roots`, managed locally on that
  computer (`homemesh worker allow-path`). The coordinator cannot grant itself access to another
  computer's folders. (The coordinator's own embedded worker trusts collection roots the owner created,
  since they are the same machine and owner.)
* Path checks resolve real paths (no `..` or symlink escapes) and refuse sensitive locations even inside
  allowed folders: `.ssh`, `.gnupg`, keyrings and credential stores, browser profiles, password-manager
  databases, `.env` files and private-key files. Filesystem roots and the whole home folder can't be
  added as a collection.
* **Tools** declare a side-effect class (`none`, `read`, `write`, `destructive`). Anything that writes
  **always** requires your approval in the UI (from any of your devices), with a timeout. A tool that
  runs on another computer is re-checked there against that computer's `allowed_tools` and paths. There
  is **no shell/command tool**. `write_file` can only create *new* files in the HomeMesh workspace folder
  of that computer (it never overwrites), and it is not in a worker's `allowed_tools` by default.
* Tool arguments are validated against a schema; invalid calls are refused, not "fixed up".
* Retrieved documents, memories and tool results are put into the prompt **marked as untrusted data**,
  separate from the owner's instructions. Text inside them can't change permissions, approvals or
  settings; those are decided only by code and by you. The model is told to treat them as reference
  material, but no prompt can fully prevent a model from being *misled* by injected text. That's why
  side effects need approval.
* Every tool execution, approval decision, login, pairing, revocation, settings change and backup is
  recorded in the **audit log** (Settings → Security, `GET /api/v1/system/audit`).

## Internet access (web_search, fetch_url, open_url)

Off by default; each computer allows them itself in `worker.allowed_tools`. Details:
[docs/WEB_ACCESS.md](docs/WEB_ACCESS.md).

* `web_search` uses the owner's own SearXNG on `127.0.0.1` (installed per user with rootless Podman,
  published on loopback only, pinned image, no root). Queries go to public search engines.
* `fetch_url` connects only to public internet addresses. Private, loopback, link-local (cloud
  metadata), CGNAT/Tailscale, IPv6 local, multicast/reserved and IPv4-in-IPv6 forms are refused, and so
  is every address on the fetching computer's own networks (its global IPv6 /64 and its own public
  addresses); a router's public WAN address reached by hairpin NAT can't be detected; every
  redirect is re-checked; the connection goes to the exact address that was checked (DNS-rebinding
  safe) while TLS is verified against the real host name; only web ports (80, 443, 1024+); environment
  proxies are ignored; size, time and redirect limits apply, and compressed bodies are inflated with a
  hard limit (no decompression bombs).
* **Exfiltration guard:** `fetch_url` runs without approval only for addresses the user wrote in the
  conversation, or that a search result or an earlier fetch itself supplied (exact host, port, path and
  query; only http/https, `www.` and one trailing slash may differ), and of the latter at most 3 per
  answer and one per site. Links found on fetched pages always ask, because the page chooses them and
  could offer a set of links whose selection spells out data. The remaining channel (which of a few
  search results gets read) is a few bits per answer. Search queries are written by
  the model and are not checked this way; set `web_search` to *ask every time* if that matters to you.
* `open_url` is a `write` tool: it always needs approval, shows the exact canonical link and the
  computer, flags links into the local network, and accepts only `http(s)` URLs (never files,
  programs or other URL schemes). No shell is involved.
* Search results and pages are untrusted data in the prompt, like documents.

## Memory

* Memories are saved only when you ask ("remember that…", the Memory page, or `/remember`) or, if you
  turn on automatic suggestions, as **candidates** that you approve or reject. Each memory records its
  source conversation and message, and can be edited or deleted.
* Memories and documents are never sent anywhere except to the model serving your request (on one of
  your computers).

## Cloud

Cloud AI is **off**. `[cloud] enabled = false` is the master switch. v0.1.0 ships no cloud
backend; the switch only gates Ollama's "remote" models (models Ollama proxies to a cloud service),
which are hidden and unschedulable unless both `[cloud] enabled` and the backend's
`allow_remote_models` are true. Such models are labelled `cloud` in the UI.

## Data protection and recovery

* Database writes are transactional (SQLite WAL). Migrations run in a transaction after an automatic
  pre-migration copy; a newer database is never opened by an older version.
* **Backups** (`homemesh backup create`, Settings → Backup) contain a consistent database snapshot, the
  config, TLS material and node credentials (exclude with `--no-secrets`), attachments and a manifest of
  SHA-256 checksums. With `--encrypt` the archive is encrypted with **AES-256-GCM** in 1 MiB authenticated
  chunks, using a key derived from your passphrase with **scrypt** (N=2^15, r=8, p=1). A wrong passphrase or
  any modification is detected. **Unencrypted backups contain your secrets**; store them like the data itself.
* **Restore** verifies the archive first, refuses while HomeMesh is running, and moves the current data
  aside to `<data>-pre-restore-<timestamp>` instead of deleting it. Archive extraction uses Python's
  `data` filter (no absolute paths, links or path traversal).
* Uninstallers keep your data unless you explicitly ask and type `DELETE`.

## Respecting the computers it runs on

* No installer needs administrator/root rights. Nothing changes firewall rules, SELinux/Defender
  settings, system packages or network configuration; when such a change would help, the docs show the
  command and leave the decision to you.
* Workers never kill or throttle other programs. In balanced/background mode they *decline new work*
  when the GPU is busy with something else or memory is short.
* No `curl | sh` installation. Install scripts run from a source tree you can read first.

## Known limitations

* Database and logs are not encrypted at rest (use disk encryption).
* The single-owner model has no per-user isolation yet.
* Prompt injection can still mislead a model's *answers*; it can't bypass approvals or path rules.
  With web tools enabled, a model could be misled into putting private details into a *search query*
  (see Internet access).
* The self-signed CA has to be trusted manually on phones for a warning-free browser experience.
* LAN discovery (off by default) reveals that HomeMesh runs on the network.
