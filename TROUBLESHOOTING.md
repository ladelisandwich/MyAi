# Troubleshooting

Start with:

```bash
homemesh status        # running? which computers are online? warnings
homemesh doctor        # OS, Python, GPU, backends, coordinator link, TLS, database, ports, service
homemesh logs -f       # live log (data/logs/homemesh.log)
```

Errors in the UI and API have a short **code** (shown below in `monospace`) and a hint. The
**Jobs** page shows, for every request, which computer was chosen and why the others were not.

## Installation and start-up

**`homemesh: command not found`**
Linux: add `~/.local/bin` to `PATH` (`export PATH="$HOME/.local/bin:$PATH"` in `~/.bashrc`). Windows:
open a *new* terminal after installing; if it's still missing, the installer was run with `-NoPath`.
Use `%LOCALAPPDATA%\Programs\HomeMesh\bin\homemesh.cmd` directly.

**"Python is missing venv or SQLite FTS5 support"**
Ubuntu/Debian: `sudo apt install python3-venv`. Use a python.org or distribution Python. Some
minimal builds lack FTS5.

**"Refusing to start: server.host=… exposes HomeMesh to the network without TLS"**
This is deliberate. Run `homemesh tls init` (sets `tls_mode = "self_signed"`), use a reverse proxy
(`tls_mode = "proxy"` with `host = "127.0.0.1"`), or, only on a network you fully trust, set
`allow_insecure_lan = true`.

**"HomeMesh is already running for …"**
Only one HomeMesh runs per data folder (an OS lock guarantees it), so a second `homemesh run` or a
`homemesh start` racing the login autostart exits before touching the database. Use `homemesh status`,
or `homemesh stop` first. `homemesh setup` on a computer where autostart already started HomeMesh tells
you to `homemesh restart` to apply the new settings.

**HomeMesh doesn't start at login, and `homemesh logs` shows nothing new**
Errors that happen before logging starts (for example a mistake in `homemesh.toml`) are written to
`console.log` next to `homemesh.log` (`homemesh config path` shows the folder; on Windows
`%LOCALAPPDATA%\HomeMesh\logs\console.log`). `homemesh config validate` checks the file.

**Port already in use**
`homemesh doctor` shows it. Another HomeMesh may already be running (`homemesh status`), or change
`server.port`.

**Windows: Node.js installer fails (exit code 1603)**
Node is only needed once to build the web app. Use the portable ZIP from nodejs.org and check it
against `SHASUMS256.txt`, as in the Windows hub guide (step 1.1). Then run the HomeMesh installer from
the same PowerShell window.

**Browser shows `web_ui_missing`: "The web interface has not been built"**
The Python package has no `web_dist`. Build it with `scripts/build_web.sh` (Node.js 20.19+) and
reinstall (`deploy/linux/install.sh --upgrade`), or use a release that includes it. The API and CLI
work without it.

**First-run setup asks for a setup token**
It's printed in the console/log on first start and saved in `data/secrets/setup-token.txt`. Or create
the owner from a terminal: `homemesh setup` or `homemesh user create-owner`.

**Forgot the password**
On the coordinator computer: `homemesh user reset-password` (this revokes all sessions).

## Models and inference

**`backend_unreachable`: "Ollama at http://127.0.0.1:11434 is not reachable"**
Start Ollama (`ollama serve`, or its desktop app/system service). If Ollama runs elsewhere, set
`inference.backends[].url`. In containers, use `HOMEMESH_OLLAMA_URL=http://ollama:11434`.

**"No chat model is available"**
`ollama pull llama3.2:3b` on any worker, or Models → Download. The model list refreshes automatically
(or press Refresh).

**`backend_runner_failed`: "Ollama could not start its model runner"** (`error starting llama-server`, `llama-server process has terminated`)
Ollama is installed and answers, but cannot start the process that runs models, so *every* model fails,
even tiny ones. This is an Ollama/driver problem on that computer, not a missing download. Check
Ollama's own log: Windows `%LOCALAPPDATA%\Ollama\server.log`, Linux `journalctl --user -u ollama`.
Test outside HomeMesh with `ollama run llama3.2:3b "hello"`. Then narrow it down:
1. Run Ollama without GPUs: set `CUDA_VISIBLE_DEVICES=-1` and `OLLAMA_VULKAN=0` for your user, quit
   and restart Ollama, and try again. If it works now, a GPU backend is the problem: update the driver
   (NVIDIA Maxwell/Pascal cards need driver 570+), or keep the card that fails hidden from Ollama.
2. If it still fails on CPU, look in Windows Security → Protection history for blocked or quarantined
   Ollama files (`llama-server.exe`, `ggml-*.dll`). Smart App Control or antivirus can block them.

**`model_not_found` / `model_incompatible`**
The selected model isn't on any online computer, or lacks a needed capability (e.g. tools, vision,
embedding). Models → *Model roles* lists only compatible models. Capabilities come from Ollama; for
llama.cpp/vLLM they must be declared in `declared_models` (HomeMesh never guesses).

**`insufficient_memory`: "model X needs ~N GB; the most free GPU memory is M GB"**
HomeMesh doesn't load a model that won't fit in **free** VRAM minus the mode's reserve. Options:
choose a smaller or more quantised model, unload other models (Models → Unload), close GPU-heavy apps,
switch the node to *performance* mode (smaller reserve), or allow CPU inference
(`worker.allow_cpu_inference = true`, slower).

**`no_eligible_worker`: every computer was rejected**
The error lists the reason per computer: `paused`, `at_capacity`, `gpu_busy_external` (a game is using
the GPU in balanced/background mode), `stale_heartbeat`, `offline`, … Wait, change the node's mode, or
pick another model.

**`timeout` / "all computers are busy"**
Interactive requests wait up to `server.queue_timeout_s` (30 s) for capacity. Raise
`worker.max_concurrent_jobs` (if the hardware can take it) or add a computer.

**Slow first reply**
The first request loads the model into memory (`load_ms` in the message details). Ollama keeps it
loaded for `inference.keep_alive` (10 minutes). Use Models → Load to preload.

**Replies say `MOCK`**
`inference.mock_enabled` or `HOMEMESH_MOCK=1` is on. That's for testing only. Turn it off and restart.

**Ollama "cloud" models are missing**
Models that Ollama proxies to a remote service are hidden unless `[cloud] enabled = true` and the
backend has `allow_remote_models = true`. That's intentional.

## Computers (workers)

**Join fails: "fingerprint mismatch … possible impersonation"**
The certificate presented doesn't belong to the coordinator whose fingerprint you entered. Check the
URL and copy the fingerprint again from the coordinator (`homemesh tls show` or the pairing dialog).
If the coordinator's TLS was re-initialised, use the new fingerprint. **Don't** work around this.

**Join fails: `pairing_invalid`**
Codes expire after 10 minutes and work once. Create a new one. Look-alike characters (O/0, I/L/1),
spaces and missing dashes are tolerated.

**Join refuses `http://` to another computer**
Workers won't send credentials over plain HTTP across the network. Enable TLS on the coordinator, or
pass `--allow-insecure-http` if you accept that risk (e.g. inside a VPN).

**Worker shows offline**
On the worker: `homemesh status` (is it running? can it reach the coordinator?) and `homemesh logs`.
Check that the coordinator URL resolves from the worker and the coordinator's firewall allows port 8765
from the LAN. Workers reconnect automatically with back-off. A worker whose clock is far off still
works (heartbeats are timed by the coordinator).

**Worker log: "credential rejected (4401)" / "node revoked (4403)"**
The node was revoked or its credential replaced. Re-join with a new pairing code
(`homemesh worker leave`, then `homemesh worker join …`).

**Worker log: "protocol incompatible (4406)"**
Coordinator and worker versions are too far apart. Upgrade both (coordinator first).

**"superseded (4409)"**
Two processes are running with the same worker identity (for example, a copied data folder). Stop one;
each computer needs its own `worker join`.

**GPU not detected / VRAM "unknown"**
NVIDIA: `nvidia-smi` must work in the same environment HomeMesh runs in (inside a container the GPU must
be passed through; see deploy/bazzite/README.md). AMD/Intel on Linux: the amdgpu/i915/xe driver must
expose sysfs memory info. Without VRAM telemetry the scheduler checks RAM only and says so in its decisions.

**The game stutters while HomeMesh runs**
Put that computer in **balanced** (pauses new GPU work above `gpu_busy_threshold_percent`) or
**background** mode, lower the threshold, or pause it. A model that is already loaded stays in VRAM until
Ollama's keep-alive expires; unload it from the Models page to free VRAM immediately.

## Documents (knowledge)

**"Folder is not allowed on this computer" / `path_not_allowed`**
Each computer decides which of its folders may be read: `homemesh worker allow-path <folder>` on *that*
computer, then retry the scan.

**"Refusing to index your whole home folder" / "an entire filesystem"**
Choose a specific sub-folder. Sensitive locations (`.ssh`, browser profiles, password stores, `.env`) are
always refused.

**Search is "keyword only"**
No embedding model is configured or online. `ollama pull nomic-embed-text`, then set Models → Model roles
→ Embeddings. Existing chunks are embedded automatically (Knowledge → Embed missing).

**OneDrive files show "cloud-only file … skipped"**
With OneDrive Files On-Demand (or another sync client), files that are only in the cloud are not
read, because reading them would make Windows download the whole folder. Right-click the folder →
**Always keep on this device**, then rescan. Junctions and symbolic links inside an indexed folder
are never followed out of it.

**PDF shows `needs_ocr`**
The PDF contains scanned images without a text layer. OCR isn't included in v0.1.0. Run it through an OCR
tool (e.g. `ocrmypdf`) and rescan.

**Answers don't cite my documents**
Check that the collection is `indexed` and search finds the passage (Knowledge → Search). Small models
often ignore context; try a larger chat model. Knowledge retrieval can be turned off per message or in
Settings.

## Internet access (web search, pages, links)

Start with `homemesh web test` (a real search from this computer) or
`homemesh web test --fetch https://example.com/`. Set-up: [docs/WEB_ACCESS.md](docs/WEB_ACCESS.md).

**The assistant never searches / opens links**
The tool must be allowed on an online computer (`homemesh worker allow-tool web_search`, then
`homemesh restart`; Settings → Tool permissions shows where each tool runs), and the chat model must
support tools (Models page: **tools** capability). Small models often ignore tools; try a larger one.

**"web search is not set up on this computer: web.searxng_url is empty"**
`homemesh web set-searxng http://127.0.0.1:8888` on the computer that runs SearXNG, then restart.

**"SearXNG at http://127.0.0.1:8888 is not reachable"**
On Bazzite: `systemctl --user status homemesh-searxng` and `journalctl --user -u homemesh-searxng`.
Re-run `bash deploy/bazzite/install-searxng.sh` (keeps your settings).

**"SearXNG refused to answer in JSON"** (HTTP 403)
Its `settings.yml` lacks `json` under `search.formats`. Add it
(`~/.config/homemesh-searxng/settings.yml`) and `systemctl --user restart homemesh-searxng`.

**"SearXNG's rate limiter blocked the request"** (HTTP 429)
Set `server.limiter: false` in its settings (the installer does), then restart it.

**Searches return nothing / `unresponsive_engines`**
Search engines sometimes rate-limit or show CAPTCHAs to SearXNG. Wait, or enable other engines in
SearXNG's settings. HomeMesh reports this honestly instead of inventing results.

**"… points to a private or local network address"**
Deliberate: `fetch_url` never reads pages on your home network or this computer. Open such pages
yourself (or with `open_url`, which asks first).

**"its HTTPS certificate is not valid for this name"**
The site's certificate doesn't match, or something on your network intercepts HTTPS (some antivirus
and corporate proxies). HomeMesh never skips certificate checks.

**An approval card appears for `fetch_url`**
The model wants an address that you didn't give and no search result contained. Approve if the link
looks right, deny otherwise (see "No silent data leaks" in WEB_ACCESS.md).

**`open_url`: "not running inside a graphical desktop session"** (Linux)
HomeMesh on that computer was started outside the desktop (e.g. over SSH). On Windows it opens the
browser of the signed-in user; HomeMesh must run as that user (the normal autostart does).

## Chat and memory

**A reply was "interrupted"**
The coordinator restarted, or the worker disconnected mid-reply. The partial text is kept. Use
**Regenerate**. Chat requests are never retried automatically on another computer.

**"Remember that…" didn't save anything**
Explicit-memory detection can be disabled in Settings → Memory. Very long or very short statements are
ignored. You can always add memories on the Memory page.

**Tool approval request expired (`approval_closed`)**
Approvals time out after `tools.approval_timeout_s` (5 minutes). Ask again.

## Database, backups, upgrades

**"The database schema (vN) is newer than this HomeMesh version supports"**
You started an older HomeMesh on a newer database. Install the newer version again, or restore a backup
made with this version.

**Database migration failed**
Startup stops; the failed migration was rolled back, so the database is unchanged. A copy taken before
migrating is in `data/backups/pre-migration-v<from>-to-v<to>-<time>.sqlite3`. Report the error with the
log. To return to the previous version: reinstall it; the database is still at the old version.

**Database corruption (`doctor` shows integrity failure)**
Stop HomeMesh and restore the latest backup: `homemesh restore <file>`. The damaged data folder is kept
aside as `<data>-pre-restore-<time>`.

**Restore says HomeMesh is running**
`homemesh stop` first (restore never overwrites live data).

**`backup_decrypt_failed` / "Wrong passphrase"**
The passphrase is wrong or the file was modified. There is no recovery without the passphrase.

## Windows specifics

* **"An Application Control policy has blocked this file"** (or a program silently doesn't start):
  Windows 11 **Smart App Control** blocks unsigned programs and libraries. HomeMesh runs through Python's
  signed launcher (`python.exe -m homemesh`), but Python packages with compiled parts (numpy,
  cryptography, pydantic) ship unsigned `.pyd` files that it may block. Check Windows Security → App &
  browser control → Smart App Control. Turning it off is your decision: Windows doesn't let you turn it
  back on without reinstalling. Microsoft Defender antivirus keeps running either way.

* `homemesh stop` asks the process to stop via a stop-request file. If it doesn't stop within 30 s,
  `homemesh stop --force` terminates it (interrupted jobs are recorded on the next start). HomeMesh
  records its process's start time with the PID, so after a logoff or reboot a recycled PID that
  now belongs to another program is never treated as HomeMesh, let alone terminated.
* After the PC wakes from sleep, connected computers get a fresh heartbeat timeout instead of being
  dropped.
* Keep `homemesh logs -f` open as long as you like: it no longer blocks log rotation, and if another
  program holds the log open, HomeMesh keeps writing and rotates later instead of losing records.
* If Windows Defender SmartScreen or the firewall prompts when the coordinator first listens on the
  network, allow **Private** networks only. Workers need no inbound rule.
* Antivirus software that scans the data folder can slow down SQLite. Excluding the data folder is your
  decision; HomeMesh never changes Defender settings.

## Getting help

Include `homemesh --json doctor` and relevant parts of `homemesh logs`. Logs contain no message
contents, but do contain computer names, model names and file paths of indexed documents. Review them
before sharing.
