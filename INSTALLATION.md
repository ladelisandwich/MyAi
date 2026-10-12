# Installation

HomeMesh runs as a normal user program. No installer needs administrator/root rights, and
none of them change firewall rules, security settings or system packages. Wherever such a
change would help, the docs give you the command to run yourself.

* [Concepts: roles and modes](#concepts)
* [Linux (Fedora, Ubuntu, Arch, …)](#linux)
* [Bazzite and other immutable systems](#bazzite)
* [Windows 10/11](#windows)
* [Containers (Docker / Podman)](#containers)
* [Adding computers (pairing)](#adding-computers)
* [Using HomeMesh from phones and other devices](#clients)
* [Behind your own reverse proxy](#reverse-proxy)
* [Upgrading](#upgrading) · [Uninstalling](#uninstalling)

## Concepts

Every computer runs the same `homemesh` program with one or more **roles**:

| Role | What it does | Where |
|---|---|---|
| **coordinator** | API, web UI, database (conversations, memory, documents index), scheduler | exactly one, ideally always-on |
| **worker** | runs AI jobs on its local backends (Ollama, llama.cpp, vLLM) and scans local folders | any number of computers |
| **client** | nothing to install; a browser (or the installed PWA) pointed at the coordinator | phones, tablets, laptops |

`homemesh setup` offers four presets:

* **standalone**: coordinator and worker on one computer, listening on `127.0.0.1` only. Start here.
* **coordinator**: the same, but listening on your home network with TLS, so other computers and phones can connect.
* **worker**: joins an existing coordinator with a pairing code.
* **client**: stores the coordinator URL for the CLI; use a browser for everything else.

Each worker has a **mode**, which you can change at any time from the Nodes page:

* **performance**: use the computer fully.
* **balanced** (default): leave memory headroom, and pause new GPU work while games or other programs load the GPU.
* **background**: only indexing and embeddings; chat only when no other computer can take it.
* **paused**: no new work.

## Prerequisites

* **Python 3.11 or newer** with `venv` and SQLite FTS5 (standard in python.org, Fedora, Ubuntu and Arch builds).
* **Ollama** (<https://ollama.com/download>) on every computer that should run models, with at least one model:
  `ollama pull llama3.2:3b` (chat) and `ollama pull nomic-embed-text` (document search).
  Ollama listens on `127.0.0.1:11434` by default. Keep it that way: HomeMesh talks to it locally.
* **Node.js 20.19+**, only to build the web UI once from source. Release archives already contain it.
* GPU drivers as usual (NVIDIA: `nvidia-smi` must work). HomeMesh does not install drivers.

<a id="linux"></a>
## Linux

```bash
git clone https://github.com/ladelisandwich/myai.git ~/src/homemesh && cd ~/src/homemesh
./deploy/linux/install.sh            # options: --upgrade  --no-web  --python /usr/bin/python3.13
homemesh setup
homemesh service install             # optional: systemd --user unit, starts at login
homemesh start
homemesh doctor
```

The installer creates a virtual environment in `~/.local/share/homemesh/venv`, installs
HomeMesh from the checkout, builds the web UI if `npm` is available, and links
`~/.local/bin/homemesh`. It does not use `sudo` and never pipes downloaded scripts into a shell.

Files:

| | |
|---|---|
| Config | `~/.config/homemesh/homemesh.toml` |
| Data (database, keys, logs, backups) | `~/.local/share/homemesh/data/` |
| Program | `~/.local/share/homemesh/venv/` |
| Service | `~/.config/systemd/user/homemesh.service` |

Set `HOMEMESH_HOME=/some/dir` to keep config and data together in one folder (portable installs).

A systemd user service stops when you log out unless lingering is enabled. For an always-on
worker or coordinator, run `loginctl enable-linger $USER` once (HomeMesh prints this hint; it
doesn't run it for you).

<a id="bazzite"></a>
## Bazzite and other immutable systems

Use the native user install above (nothing is layered with `rpm-ostree`) or rootless Podman
Quadlets. See [deploy/bazzite/README.md](deploy/bazzite/README.md), including the steps to validate
an NVIDIA GPU (`nvidia-smi`, `nvidia-ctk cdi list`, `ollama ps`).

<a id="windows"></a>
## Windows 10/11

1. Install Python 3.11+ from <https://www.python.org/downloads/windows/> or `winget install Python.Python.3.13`.
2. Install Ollama for Windows from <https://ollama.com/download> and pull a model (`ollama pull llama3.2:3b`).
3. In a normal (non-administrator) PowerShell window, from the HomeMesh folder:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1        # options: -Upgrade -AutoStart -NoWeb -NoPath -Python <path>
```

`-ExecutionPolicy Bypass` applies only to that one PowerShell process; your system policy is unchanged.

4. Open a **new** terminal and run `homemesh setup`, then `homemesh start`.

| | |
|---|---|
| Program | `%LOCALAPPDATA%\Programs\HomeMesh\` (venv + `bin\homemesh.cmd`, added to your *user* PATH) |
| Config | `%APPDATA%\HomeMesh\homemesh.toml` |
| Data | `%LOCALAPPDATA%\HomeMesh\data\` |
| Autostart (`-AutoStart` or `homemesh service install`) | value `HomeMesh` under `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` |

GPU detection uses `nvidia-smi` (installed with the NVIDIA driver) and falls back to Windows CIM
(`Win32_VideoController` plus the driver's registry value for cards with more than 4 GB, because
CIM's `AdapterRAM` field wraps at 4 GB).

**Firewall.** A computer that is only a **worker** makes outbound connections and needs no rule.
If this PC is the **coordinator** for other devices, Windows asks the first time Python listens on
the network. Allow **Private networks** only. To add the rule yourself instead (in an
administrator PowerShell, if you choose to):

```powershell
New-NetFirewallRule -DisplayName "HomeMesh" -Direction Inbound -Protocol TCP -LocalPort 8765 -Profile Private -Action Allow
```

> The Windows scripts and the Windows-specific code paths (autostart entry, CIM GPU query, stop
> handling) have **not yet been run on a real Windows PC**. The Python test suite runs on Windows in
> CI. Please report problems.

<a id="containers"></a>
## Containers (Docker / Podman)

```bash
docker compose -f deploy/containers/compose.yaml up -d --build
# NVIDIA GPU: add  -f deploy/containers/compose.nvidia.yaml  (needs nvidia-container-toolkit)
docker compose -f deploy/containers/compose.yaml exec homemesh homemesh user create-owner --username owner
```

* Ollama runs on an internal network only and is never published.
* HomeMesh is published on `127.0.0.1:8765`. For LAN access, enable TLS as shown in the comments in `compose.yaml`.
* Data is kept in the `homemesh-data` and `ollama-models` volumes. Back up with
  `docker compose … exec homemesh homemesh backup create`.
* Inside a container HomeMesh can only see a GPU that is passed to it. The NVIDIA overlay does
  this so VRAM is reported correctly.

> The Containerfile and compose files have **not been built in this project's development
> environment** (no container daemon was available). They follow the documented formats; please report problems.

<a id="adding-computers"></a>
## Adding computers (pairing)

1. **Make the coordinator reachable on your LAN.** On the coordinator computer, run `homemesh setup`
   and choose *coordinator*, or edit `homemesh.toml`:

   ```toml
   [server]
   host = "0.0.0.0"
   tls_mode = "self_signed"
   tls_extra_sans = ["192.168.1.10", "desktop.local"]   # addresses other devices will use
   ```

   then `homemesh tls init` and `homemesh restart`. HomeMesh refuses to listen on the network
   without TLS unless you explicitly set `allow_insecure_lan = true`.
   Open port 8765/TCP for your **private** network in the firewall if needed (see above).

2. **Create a pairing code** on the coordinator: `homemesh node pair` (or **Computers → Add a computer**
   in the web UI). You get a one-time code (valid 10 minutes, limited attempts) and the
   coordinator's certificate **fingerprint**.

3. **Join** from the new computer:

   ```bash
   homemesh worker join --coordinator https://192.168.1.10:8765 --code ABCD-EFGH-JKLM \
       --fingerprint 3F:A2:…:9C --name gaming-pc
   homemesh start            # or: homemesh service install && homemesh start
   ```

   The worker downloads the coordinator's CA certificate, checks it against the fingerprint you
   typed (and refuses to continue on a mismatch), stores it, and from then on verifies TLS normally.
   Its credential is stored in its data folder, readable only by your user.

4. **Allow folders** for document indexing on that computer (nothing is allowed by default):
   `homemesh worker allow-path ~/Documents/Notes`.

Optional: if the coordinator sets `server.discovery_enabled = true`, a worker can run
`homemesh worker discover` to list coordinators on the local subnet. Discovery answers are not
trusted. You still need the pairing code and must compare the fingerprint.

Workers can live on other subnets or a VPN (for example Tailscale or WireGuard). They only need
to reach the coordinator's URL.

<a id="clients"></a>
## Phones and other devices

Open the coordinator URL (`https://192.168.1.10:8765`) in a browser. With a self-signed CA the
browser warns until you trust the CA: download it from `https://<coordinator>:8765/api/v1/tls/ca`
and install it as a trusted certificate on that device (iOS: Settings → General → About →
Certificate Trust Settings after installing the profile; Android: Settings → Security →
Encryption & credentials → Install a certificate → CA certificate). After that, **Add to Home
Screen** installs HomeMesh as an app.

Alternatively, put HomeMesh behind a reverse proxy with a publicly trusted certificate (next section).

<a id="reverse-proxy"></a>
## Behind your own reverse proxy

If you already run Caddy, nginx or Traefik with a real certificate:

```toml
[server]
host = "127.0.0.1"          # required: the plain-HTTP port must not be reachable from the network
tls_mode = "proxy"
public_url = "https://ai.example.home"
trusted_proxies = ["127.0.0.1"]
```

Proxy `/` to `http://127.0.0.1:8765`. Enable WebSocket upgrades for `/api/v1/worker/ws` and
disable response buffering for the server-sent event endpoints (`/api/v1/events`,
`/api/v1/conversations/*/messages`, `/api/v1/chat/runs/*/events`). Workers then join with
`--system-trust` instead of `--fingerprint`.

Never expose HomeMesh, Ollama or any model server directly to the internet. For access from
outside your home, use a VPN.

## Upgrading

```bash
cd ~/src/homemesh && git pull
./deploy/linux/install.sh --upgrade               # Windows: install.ps1 -Upgrade
```

Database migrations run automatically on start. A copy of the database is saved to
`data/backups/pre-migration-*.sqlite3` first. A newer database is never opened by an older
HomeMesh (it refuses to start rather than risk damage). Upgrade the coordinator first; workers
using an older **protocol** version are refused with a clear message (`homemesh status` on the
worker shows it).

## Uninstalling

```bash
./deploy/linux/uninstall.sh                 # keeps your data
./deploy/linux/uninstall.sh --purge-data    # also deletes data, after typing DELETE
```

On Windows run `deploy\windows\uninstall.ps1` (add `-RemoveData` to delete data, after typing
DELETE). Models downloaded by Ollama belong to Ollama and are not touched.
