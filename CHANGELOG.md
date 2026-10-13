# Changelog

All notable changes are listed here. The project follows [Semantic Versioning](https://semver.org/);
the worker protocol has its own version ([docs/PROTOCOL.md](docs/PROTOCOL.md)).

## [0.2.0] - unreleased

Upgrading from 0.1.0: run the installer with `--upgrade` / `-Upgrade` as described in the guide's
"Updating later"; it stops the running 0.1.0 itself before replacing it.

### Added

* **Internet access (opt-in per computer):** `web_search` through the owner's own SearXNG,
  `fetch_url` for public pages (private/local addresses refused on every redirect, DNS-rebinding safe,
  HTTPS verified, size/time limits, page text extracted off the event loop in linear time; no approval only for links the user wrote and up to 3 search
  results per answer, one per site; links found on pages always ask), and `open_url` to open a link in
  a computer's browser (approval every time). Rootless,
  loopback-only SearXNG Quadlet and installer for Bazzite (`deploy/bazzite/install-searxng.sh`),
  `[web]` settings, `homemesh worker allow-tool/disallow-tool`, `homemesh web set-searxng/test`.
  Approval cards now name the computer and say why they ask. Guide: docs/WEB_ACCESS.md.

### Fixed

* **Upgrading a running 0.1.0 (found by upgrading real 0.1.0 installs):** on Linux, `install.sh --upgrade`
  left the old process running (the new `homemesh status` could not see a process from before the
  instance lock), and a later `homemesh start` ran a second instance next to it that migrated the live
  database. The installer now stops HomeMesh with the installed (old) version before replacing it, as the
  Windows installer already did; the new version recognises a still-running 0.1.0 and refuses to start
  next to it; both installers check that the stop worked and that HomeMesh came back up. `homemesh stop`
  waits for the process to exit (Windows keeps its files locked until then), and `start`/`restart` exit
  non-zero when HomeMesh did not come up. CI upgrades a running 0.1.0 install on Linux and Windows.
* The version is now 0.2.0, so `homemesh version` and the Computers page show which build runs.
* Windows desktop review (found by a code review of the Windows paths, each finding independently
  verified; covered by tests where a test can show it):
  * a second HomeMesh process (autostart racing `homemesh start`, or `homemesh run` next to the
    background one) ran crash recovery against the live database; an OS lock now allows one per data
    folder, taken before anything else;
  * after a logoff the PID file could point at a recycled PID, so `start` refused and `stop --force`
    could terminate an unrelated program; the PID record now includes the process start time;
  * log rotation lost records and deleted old logs while `homemesh logs -f` (or an editor) held the
    log open; `logs -f` no longer holds it, and rotation waits instead of failing;
  * errors before logging started (e.g. a broken config) vanished in windowless autostart runs; they
    now go to `logs/console.log`; background runs no longer copy the whole log into an unrotated file;
  * document scans downloaded OneDrive cloud-only files and followed NTFS junctions out of the folder;
  * waking from sleep dropped every connected computer (Windows' monotonic clock counts sleep time);
  * the GPU query could fail on non-ASCII output and keep the worker from connecting;
  * secrets folders outside the user profile (`HOMEMESH_HOME` on another drive) got no owner-only ACL;
  * `homemesh doctor` reported a busy port as free on Windows;
  * `install.ps1` garbled the Python path for non-ASCII profile names; upgrades kept the old web UI
    (both installers now rebuild it when its sources changed); piped CLI output was garbled for
    non-ASCII paths; `setup` didn't say that a running instance needs a restart.

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

* **Install tooling:** per-user, checksum-verified Ollama installer for Linux/Bazzite
  (`deploy/linux/install-ollama.sh`); `worker.ignore_gpus`; `homemesh tls init --new-ca`; a step-by-step
  guide for a Windows 11 hub with Bazzite workers; CI that runs the real installers on Windows (PowerShell
  5.1), inside the Bazzite image, and builds/runs the container image; Python 3.14 support.

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
* On mixed-GPU machines the coordinator treated the free memory of only the GPUs that report it
  (on Windows: NVIDIA) as the node's free VRAM; it is now "unknown" unless every counted GPU reports it.
* On Windows a console window flashed every few seconds: the windowless HomeMesh process started
  nvidia-smi (every heartbeat) and PowerShell without CREATE_NO_WINDOW. All helper programs now run
  through one function that hides their window; a test forbids bypassing it.
* Ollama runner failures (`error starting llama-server`, `llama-server process has terminated`) were
  reported as "model not installed" because any message containing "not found" was treated as a missing
  model; they are now `backend_runner_failed` with a diagnostic hint. "Load" now works for
  embedding-only models (Ollama refuses /api/generate for them; HomeMesh loads them via /api/embed).
* Windows PowerShell 5.1 turned harmless pip/npm stderr output into fatal errors when the installer's
  output was redirected; native calls now rely on exit codes. Python detection finds 3.14-only systems.
