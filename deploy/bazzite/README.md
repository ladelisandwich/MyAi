# HomeMesh AI on Bazzite (and other Fedora Atomic / immutable systems)

Bazzite's root filesystem is read-only and managed by `rpm-ostree`. HomeMesh never layers
packages and never writes outside your home directory. There are two supported ways to run it:

| | **A. Native user install** (recommended for GPU workers) | **B. Rootless Podman Quadlets** |
|---|---|---|
| What runs | Python venv in `~/.local/share/homemesh/venv`, systemd **user** service | `homemesh` and `ollama` containers as systemd **user** services |
| GPU visibility for HomeMesh | Full (reads `nvidia-smi` / sysfs directly) | Only if you pass the GPU to the HomeMesh container (see below) |
| Ollama | Your choice: native, Bazzite's own setup, or the Quadlet here | `ollama.container` (CDI GPU) |
| Needs root | No | No |

> **Status:** these files have been written against Podman 5 / Quadlet documentation but have
> **not been run on a real Bazzite machine or with a real NVIDIA GPU** during development (the
> development environment had no GPU and no container daemon). Please follow the validation
> steps below and report anything that differs.

## A. Native user install

```bash
git clone https://github.com/ladelisandwich/myai.git ~/src/homemesh   # or unpack a release
cd ~/src/homemesh
./deploy/linux/install.sh          # Python 3 is preinstalled on Bazzite
homemesh setup                     # standalone, coordinator, worker or client
homemesh service install           # systemd --user unit, starts at login
homemesh start
homemesh doctor
```

The web UI needs a one-time build with Node.js 20.19+. Bazzite does not include Node; you can
use Homebrew (`brew install node`; Universal Blue images such as Bazzite provide `brew`) or build inside a distrobox
(`distrobox create -i fedora:42 -n build && distrobox enter build -- ./scripts/build_web.sh`).
Release archives that already contain `src/homemesh/web_dist/` skip this step.

To keep HomeMesh running while nobody is logged in (a headless worker), run once:
`loginctl enable-linger $USER`. The installer only prints this hint; it never runs it for you.

For Ollama, use whatever you already have (for example Bazzite's documented Ollama setup, a
Homebrew install, or `ollama.container` below). HomeMesh only needs its URL; the default is
`http://127.0.0.1:11434`.

## B. Rootless Podman Quadlets

```bash
cd ~/src/homemesh
podman build -t localhost/homemesh:0.1.0 -f deploy/containers/Containerfile .
mkdir -p ~/.config/containers/systemd
cp deploy/bazzite/homemesh.network deploy/bazzite/ollama.container deploy/bazzite/homemesh.container \
   ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user start ollama homemesh
podman exec -it homemesh homemesh user create-owner --username owner
```

Then open <http://127.0.0.1:8765>. By default HomeMesh is published on `127.0.0.1` only, and
Ollama is published on `127.0.0.1` only (it has no authentication; never publish it on the LAN).
To serve other devices, follow the TLS comments in `homemesh.container`.

Data lives in `~/.local/share/homemesh-container` (HomeMesh) and `~/.local/share/ollama-container`
(models). Back up with `podman exec homemesh homemesh backup create`.

## Validating the NVIDIA GPU (e.g. RTX 3080)

Run these on the Bazzite host. None of them changes anything.

1. **Driver:** `nvidia-smi` shows the card, driver version and total memory (an RTX 3080 has
   10 GB or 12 GB depending on the model; HomeMesh reports what the driver says, it never assumes).
2. **CDI spec for rootless containers:** `nvidia-ctk cdi list` should print `nvidia.com/gpu=all`
   (and one entry per GPU).
   * If it prints nothing, the CDI specification has not been generated. Generating it writes a
     system file and needs root (`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`).
     HomeMesh will not do this for you; decide yourself whether to run it, or use option A with a
     native Ollama instead.
3. **Ollama sees the GPU:** `podman exec ollama nvidia-smi` lists the card. After a chat,
   `podman exec ollama ollama ps` shows the model with `100% GPU` (or a CPU/GPU split if it does not
   fit in VRAM).
4. **HomeMesh sees the GPU:**
   * Native install: `homemesh doctor` shows the GPU name and total VRAM; the **Nodes** page shows
     live free VRAM, utilisation and temperature, and the **Models** page shows `VRAM` for loaded models.
   * Quadlet: uncomment `AddDevice=nvidia.com/gpu=all` in `homemesh.container` so `nvidia-smi` is
     available inside it; otherwise HomeMesh reports "no GPU detected" and schedules on RAM only
     (Ollama will still use the GPU, but HomeMesh cannot account for VRAM).
5. **End-to-end:** in the web UI send a message; the reply header shows the model, the node and
   `gpu` placement, and the message details show tokens/second and model load time measured by
   Ollama.

If a game is running, set the node to **Balanced** or **Background** mode (Nodes page, or
`homemesh node mode <name> background`; `homemesh node pause <name>` stops new work entirely). HomeMesh then avoids new GPU work while the GPU is busy; it never
stops or kills other programs.

## SELinux note

`ollama.container` sets `SecurityLabelDisable=true`, which turns off SELinux *labelling for that
one container* so it can use the GPU device nodes. It does not change the system's SELinux mode.
Try removing the line first; if the GPU is not visible inside the container, put it back.

## Firewall

A **worker** only makes outbound connections to the coordinator and needs no firewall change.
If this machine is the **coordinator** for other devices, port 8765/TCP must be reachable from
your LAN. Bazzite uses firewalld; opening a port is a system change, so HomeMesh leaves it to you:
`sudo firewall-cmd --zone=home --add-port=8765/tcp` (add `--permanent` to keep it). Only do this
after enabling TLS (`homemesh tls init`).
