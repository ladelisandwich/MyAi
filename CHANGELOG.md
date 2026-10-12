# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org/);
the worker protocol has its own version ([docs/PROTOCOL.md](docs/PROTOCOL.md)).

## [0.1.0] - unreleased

First working version.

### Added

* **Roles:** coordinator, worker and client from one `homemesh` program. A standalone mode runs
  coordinator and worker together over the same protocol used between computers.
* **Inference backends:** Ollama (streaming chat, tools, thinking, embeddings, model pull/delete/load/unload,
  capability and memory reporting), OpenAI-compatible servers (llama.cpp `llama-server`, vLLM) with
  declared capabilities, and a clearly labelled mock backend for tests.
* **Hardware detection:** NVIDIA via `nvidia-smi`, AMD/Intel via Linux sysfs, Windows CIM with the
  registry VRAM value, CPU/RAM via psutil, immutable-OS (Bazzite) detection. Live telemetry in heartbeats.
* **Scheduler:** pure, unit-tested placement on VRAM/RAM fit with reserves, loaded models, capabilities,
  concurrency, resource modes (performance/balanced/background/paused), GPU-busy back-off, pinned
  node/model, and per-node rejection reasons stored with every job.
* **Jobs:** durable, with ack and total timeouts, cancellation, requeue of idempotent work, clear failure
  states after worker loss or coordinator restart.
* **Pairing and security:** one-time pairing codes, local CA with fingerprint pinning, hashed node
  secrets, credential rotation and revocation, owner account (argon2id), sessions, scoped API tokens,
  CSRF protection, rate limiting, strict CSP, audit log. TLS required for non-loopback binding.
* **Chat:** streaming over SSE, runs independent of the browser connection (re-attach, cancel,
  regenerate), context assembly within a token budget, memories and documents injected as untrusted
  data with citations, metrics (TTFT, tokens/s, load time, dispatch overhead).
* **Memory:** explicit "remember that…", editable memory list, optional approval-gated suggestions,
  dedupe, hybrid retrieval, retention.
* **Knowledge:** collections on any computer, scanned locally with path safety and a sensitive-file
  deny-list, incremental re-scan, text/Markdown/code/JSON/CSV/PDF extraction, embeddings on any node
  with the embedding model, hybrid FTS + vector search with file/line/page sources.
* **Tools:** registry with schemas and side-effect classes, approvals from any device, worker-side
  re-validation, audit; built-ins: calculator, current time, system info, search memory, search
  documents, list directory, read file, write file (new files in the workspace only).
* **Web app (PWA):** Chat, Dashboard, Computers, Models, Memory, Knowledge, Jobs, Settings; light/dark;
  mobile layout; installable.
* **CLI:** setup, run/start/stop/restart, status, doctor, logs, service, node, worker, model, jobs,
  backup/restore, user, tls, config.
* **Backups:** consistent snapshots, checksums, optional AES-256-GCM encryption, non-destructive restore.
* **Deployment:** Linux user install + systemd user unit, Bazzite (native or Podman Quadlets with CDI),
  Windows per-user install with autostart entry, Containerfile and compose (Ollama on an internal network).
* **Optional LAN discovery** (off by default, untrusted).
* **Tests:** unit and integration tests (real coordinator, simulated workers over real sockets); opt-in
  smoke tests against a real Ollama; CI on Linux and Windows including a start/chat/stop lifecycle.

### Fixed during development (found by CI and real-process testing)

* Backup verification left the extracted database open, which broke verify/restore on Windows.
* CLI output crashed on Windows when redirected (legacy code page); now falls back to ASCII symbols.
* `homemesh stop` could not stop a windowless process on Windows; a stop-request file now provides
  graceful shutdown on every OS.
* Worker join on a fresh computer kept the coordinator role; it now configures a worker-only node.
* "Saved to memory" notices were lost on reload; they are now stored with the message.
* Installers treated "running but degraded" (`homemesh status` exit 2, e.g. Ollama down) as not running,
  so an upgrade would not stop/restart HomeMesh; `homemesh stop` now also exits non-zero if the process
  is still alive.
* `scripts/bench.py` exited 0 when every chat failed; it now waits for a usable model and fails honestly.
