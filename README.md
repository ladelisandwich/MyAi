# HomeMesh AI

**A private, self-hosted personal AI that runs on your own computers.** Start on one PC; add
your other machines (gaming PC with an RTX card, a Bazzite box, a Windows laptop, a home server)
and HomeMesh spreads chat, document indexing and embeddings across them, choosing a computer
from its actual free GPU memory, its mode and the models it has.

Your conversations, memories and documents stay in one place on your network. No account, no
telemetry, and no cloud unless you explicitly turn it on.

```
          phone / laptop / desktop browser (installable PWA)
                              │  HTTPS
                     ┌────────▼─────────┐
                     │   coordinator    │  API · web UI · database · scheduler · memory · RAG
                     │ (+ local worker) │
                     └──┬────────────┬──┘
     outbound WebSocket │            │ outbound WebSocket (TLS, pinned CA)
              ┌─────────▼──┐      ┌──▼──────────┐
              │  worker    │      │  worker     │   run jobs on local Ollama /
              │ RTX 3080   │      │ CPU / iGPU  │   llama.cpp / vLLM backends
              └────────────┘      └─────────────┘
```

## What works today (v0.1.0)

| Area | Status |
|---|---|
| Streaming chat with local models via **Ollama** (live-tested), llama.cpp `llama-server` and vLLM (OpenAI-compatible adapter, unit-tested) | ✅ |
| One-computer **standalone** mode; add computers with a one-time **pairing code** + TLS **fingerprint pinning** | ✅ |
| **Hardware-aware scheduler**: VRAM/RAM fit, loaded models, modes (performance / balanced / background / paused), GPU-busy back-off, an explanation for every routing decision | ✅ |
| Worker crash, disconnect, cancellation, coordinator restart: jobs end in a clear state, never silently lost | ✅ |
| **Memory** across conversations and devices: explicit ("remember that…"), editable, optional automatic suggestions that need approval | ✅ |
| **Documents (RAG)**: index folders on any computer, hybrid keyword + vector search, answers cite file/line/page | ✅ |
| **Tools** with permissions, approval prompts for anything that writes, audit log; no shell tool | ✅ |
| Web app: Chat, Dashboard, Nodes, Models, Memory, Knowledge, Jobs, Settings; installable PWA; light/dark | ✅ |
| `homemesh` CLI: setup, start/stop, status, doctor, node, worker, model, jobs, backup/restore, tls, config | ✅ |
| Encrypted backups and non-destructive restore | ✅ |
| Linux / Bazzite (systemd user service or Podman Quadlets), Windows (per-user), containers | ⚠️ written; only Linux native was run (see [MILESTONES](docs/MILESTONES.md)) |
| Voice, vision, splitting one model across computers | 🗺️ designed, not implemented ([ROADMAP](ROADMAP.md)) |

The **mock backend** exists for automated tests only. It is off by default, and when enabled every
response is labelled `MOCK` in the API and UI. It is never presented as real AI output.

## Quick start (one computer)

Requirements: Python 3.11+, [Ollama](https://ollama.com/download) with at least one model, and
Node.js 20.19+ once to build the web UI (not needed if you use a release with the UI included).

```bash
git clone https://github.com/ladelisandwich/myai.git homemesh && cd homemesh
./deploy/linux/install.sh                     # Windows: deploy\windows\install.ps1 (see INSTALLATION.md)
ollama pull llama3.2:3b && ollama pull nomic-embed-text
homemesh setup                                # choose "standalone", create the owner account
homemesh start
```

Open <http://127.0.0.1:8765>, sign in, and chat. `homemesh doctor` checks GPU detection,
Ollama, ports, the database and the service if anything looks wrong.

## Adding another computer

On the coordinator, enable LAN access with TLS (`homemesh setup` → coordinator, or
`homemesh tls init`), then:

```bash
homemesh node pair            # prints a one-time code (10 minutes) and the CA fingerprint
```

On the new computer (after installing HomeMesh and Ollama):

```bash
homemesh worker join --coordinator https://192.168.1.10:8765 --code ABCD-EFGH-JKLM --fingerprint 3F:A2:…
homemesh start
```

The worker connects **outbound** to the coordinator, so it needs no open ports. It reports its
GPU (detected via `nvidia-smi`, sysfs or Windows CIM, never assumed), its models and live
utilisation; it appears on the **Nodes** page within seconds.

## Documentation

| | |
|---|---|
| [INSTALLATION.md](INSTALLATION.md) | Linux, Bazzite, Windows, containers, multi-computer setup, upgrades |
| [CONFIGURATION.md](CONFIGURATION.md) | Every `homemesh.toml` option, environment overrides, in-app settings |
| [SECURITY.md](SECURITY.md) | Threat model, pairing, TLS, auth, tools, what is and isn't protected |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Symptoms → causes → fixes |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data model, scheduling, jobs, design decisions |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) · [docs/API.md](docs/API.md) | Worker protocol v1 and the HTTP API |
| [docs/MILESTONES.md](docs/MILESTONES.md) | Acceptance criteria, how each was validated, what is still unverified |
| [docs/DISTRIBUTED_INFERENCE.md](docs/DISTRIBUTED_INFERENCE.md) · [docs/VOICE_AND_VISION.md](docs/VOICE_AND_VISION.md) | Designs for future work |
| [ROADMAP.md](ROADMAP.md) · [CHANGELOG.md](CHANGELOG.md) | |

## Development

```bash
uv venv && uv pip install -e '.[dev]'         # or: python -m venv .venv && pip install -e '.[dev]'
ruff check . && pytest -q                     # unit + integration (mock inference; no GPU needed)
pytest -m smoke                               # against a real local Ollama (opt-in)
cd apps/web && npm ci && npm run typecheck && npm test && npm run build
scripts/dev.sh                                # API on :8765 + Vite dev server on :5173
```

See [CLAUDE.md](CLAUDE.md) for repository conventions.

## License

No license has been chosen yet; until one is added, all rights are reserved by the repository owner.
