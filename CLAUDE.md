# CLAUDE.md: working on HomeMesh AI

HomeMesh is a self-hosted, local-first personal AI that runs across the owner's home computers. One
Python package (`src/homemesh`) provides the coordinator, worker and CLI; `apps/web` is the React PWA.
Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing structure, and [docs/MILESTONES.md](docs/MILESTONES.md)
for what is verified and what isn't.

## Commands

```bash
uv venv && uv pip install -e '.[dev]'        # .venv; Python ≥ 3.11
ruff check .                                 # lint (CI pins ruff 0.17.*)
pytest -q                                    # unit + integration, ~60 s, mock backend only
pytest -q tests/unit/test_scheduler.py -k pin
pytest -m smoke                              # real Ollama at 127.0.0.1:11434 (opt-in; env HOMEMESH_SMOKE_*)
cd apps/web && npm ci && npm run typecheck && npm test && npm run build
scripts/build_web.sh                         # build UI into src/homemesh/web_dist (gitignored)
scripts/dev.sh                               # coordinator (mock) on :8765 + Vite on :5173
HOMEMESH_HOME=/tmp/hm homemesh run           # throw-away instance
```

CI (`.github/workflows/ci.yml`) runs lint and tests on Ubuntu (3.11, 3.13) and **Windows** (3.13), a
start → chat → stop lifecycle, and the web build. Keep it green on Windows too.

## Layout

* `common/` config (`homemesh.toml`, pydantic, `HOMEMESH__SECTION__KEY` env), paths, security helpers
* `protocol/` worker protocol messages + job payloads (pydantic). Bump `PROTOCOL_VERSION` only for
  breaking changes, and document them in docs/PROTOCOL.md.
* `hardware/`, `inference/`, `scheduler/`, `memory/`, `knowledge/`, `tools/`: pure-ish domain code
* `coordinator/` FastAPI app (`app.py`), services, `db/` (SQLite, migrations, repositories), `api/`
* `worker/` agent (WebSocket client), executor, local policy, enrollment
* `cli/` the `homemesh` command; `runtime.py` runs roles in one process
* `tests/unit`, `tests/integration` (`tests/harness.py` starts a real coordinator on a free port and
  simulated workers over real sockets), `tests/smoke` (real Ollama)

Import boundaries are enforced by `tests/unit/test_import_rules.py`: pure packages never import
`coordinator`/`worker`; `coordinator` and `worker` never import each other; OS-specific code (`winreg`,
`schtasks`) only in `hardware/` and `cli/service.py`.

## Non-negotiable rules

1. **No fake results.** Never fabricate hardware values, capabilities, metrics or model output. Unknown
   is `None` / "not reported" with a reason. Capabilities come from the backend or explicit declaration.
2. **The mock backend is labelled everywhere** (`mock=true` on models, routes and messages, a UI badge,
   a status warning) and is never evidence that real inference works. Only enable it via config or env.
3. **Security invariants:** no non-loopback binding without TLS (unless `allow_insecure_lan`);
   never disable TLS verification (pin the CA instead); backends stay on localhost; write/destructive
   tools always need approval; workers re-check paths and tools locally; retrieved content is
   untrusted data in prompts; secrets stored hashed or in owner-only files; no telemetry; cloud
   off by default.
4. **Respect the user's computer:** no root/admin requirements, no firewall/SELinux/Defender/network
   changes, no killing other processes, uninstallers keep data unless explicitly confirmed, and no
   `curl | sh`.
5. **Durable state first:** write the job/message row before acting; every failure ends in a clear,
   persisted state with an error code and hint. Never auto-retry chat (side effects); idempotent jobs
   (`embed`, `knowledge.scan`) may be requeued.
6. **Honest docs:** when you can't verify something (GPU, Windows, containers, LAN), say so in
   docs/MILESTONES.md rather than implying it works.

## Conventions and pitfalls

* All SQL lives in `coordinator/db/repositories/`. Schema changes go in a **new** numbered migration
  (`migrations/000N_name.sql`); never edit an applied one (checksums are recorded).
* `Database` runs everything on one thread: use `await db.run(fn)` / repository methods, never a
  second connection in the coordinator.
* `with sqlite3.connect(...)` does **not** close the connection. Use `contextlib.closing(...)`; open
  handles break file operations on Windows.
* HTTP clients to backends/coordinator use `trust_env=False`; the worker's websocket uses `proxy=None`.
* CLI output may contain ✓/✗/→. `cli/main.py` installs an ASCII fallback for legacy code pages, so keep
  printing through normal `print`.
* Long-running coordinator tasks are kept in sets (`_background`) so they aren't garbage-collected;
  timeouts are named `timeout_s`.
* Tests: `asyncio_mode=auto`; use the `Harness` for anything involving workers; `HOMEMESH_MOCK_DELAY`
  speeds the mock. Mock directives in prompts: `[[mock:fail]]`, `[[mock:slow]]`, `[[mock:oom]]`,
  `[[mock:tool:NAME {json}]]`.
* Frontend: no router or UI kit; pages in `apps/web/src/pages`, API types in `src/api/types.ts`, Markdown via
  `lib/markdown.ts` (marked + DOMPurify). CSP forbids inline scripts. The service worker never caches `/api/`.
* User-facing errors: raise `HomeMeshError` subclasses with a stable `code`, a plain-language `message` and
  an actionable `hint`. Document new codes in TROUBLESHOOTING.md.

## Git

Work on the branch you are given; commit messages explain *why*; update CHANGELOG.md for user-visible
changes and docs/MILESTONES.md when the validation status changes.
