"""``homemesh setup``: interactive (or flag-driven) first-time configuration.

Modes:
  standalone   - this computer runs coordinator + worker + web UI (recommended start)
  coordinator  - central server only (other computers do the AI work)
  worker       - contribute this computer's hardware to an existing coordinator
  client       - nothing to install: use a browser (or the CLI with an API token)

Re-running setup is safe: existing configuration is shown as defaults and data is never
deleted.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import socket
import sys
from pathlib import Path

from homemesh.common.config import BackendConfig, HomeMeshConfig, load_config, save_config
from homemesh.common.errors import ConfigError, HomeMeshError
from homemesh.common.paths import AppPaths
from homemesh.common.security import validate_password_strength

MODES = {
    "1": "standalone", "2": "coordinator", "3": "worker", "4": "client",
    "standalone": "standalone", "coordinator": "coordinator", "worker": "worker", "client": "client",
}


def ask(prompt: str, default: str = "", *, interactive: bool = True) -> str:
    if not interactive:
        return default
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def ask_yes(prompt: str, default: bool = False, *, interactive: bool = True) -> bool:
    if not interactive:
        return default
    d = "Y/n" if default else "y/N"
    val = ask(f"{prompt} ({d})", "", interactive=True).lower()
    return default if not val else val.startswith("y")


def read_password(args: argparse.Namespace, interactive: bool) -> str:
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\r\n")
    if not interactive:
        raise ConfigError("A password is required: pass --password-stdin in non-interactive mode.")
    while True:
        p1 = getpass.getpass("Owner password (10+ characters): ")
        if err := validate_password_strength(p1):
            print(f"  {err}")
            continue
        if getpass.getpass("Repeat password: ") != p1:
            print("  Passwords do not match.")
            continue
        return p1


async def _probe_backend(cfg: HomeMeshConfig) -> str:
    from homemesh.inference.factory import build_backends

    lines = []
    for b in build_backends(cfg).values():
        st = await b.health()
        if st.ok:
            models = await b.list_models()
            chat = [m.name for m in models if "completion" in m.capabilities]
            emb = [m.name for m in models if "embedding" in m.capabilities]
            lines.append(f"  ✓ {b.name} {st.version or ''} reachable: {len(chat)} chat model(s){': ' + ', '.join(chat[:5]) if chat else ''}"
                         f"; {len(emb)} embedding model(s){': ' + ', '.join(emb[:3]) if emb else ''}")
            if not chat:
                lines.append("    No chat model yet. Example: ollama pull llama3.2:3b   (or qwen2.5:7b on a GPU with 8+ GB)")
            if not emb:
                lines.append("    For document search install an embedding model: ollama pull nomic-embed-text")
        else:
            lines.append(f"  ✗ {b.name}: {st.error}")
            lines.append("    Install Ollama from https://ollama.com (see INSTALLATION.md) - HomeMesh works without it, "
                         "but cannot answer until a backend is available.")
        await b.close()
    return "\n".join(lines)


def _create_owner(paths: AppPaths, username: str, password: str) -> None:
    from homemesh.coordinator.auth import AuthService
    from homemesh.coordinator.db.database import Database
    from homemesh.coordinator.db.migrate import migrate
    from homemesh.coordinator.db.repositories import Repositories

    async def go() -> None:
        db = Database(paths.db_file)
        await db.open()
        try:
            await db.run(lambda c: migrate(c, paths.backups_dir))
            repos = Repositories.create(db)
            auth = AuthService(repos, paths, session_ttl_hours=24, login_attempts_per_minute=10, secure_cookies=False)
            if await repos.users.count() == 0:
                await auth.create_owner(username, password)
                await auth.ensure_cli_token()
                print(f"  ✓ Owner account '{username}' created.")
            else:
                print("  • An owner account already exists (unchanged).")
        finally:
            await db.close()

    asyncio.run(go())


def run_setup(args: argparse.Namespace, paths: AppPaths) -> int:
    interactive = not args.non_interactive and sys.stdin.isatty()
    paths.ensure()
    try:
        cfg = load_config(paths)
    except ConfigError:
        cfg = HomeMeshConfig()
    print("HomeMesh AI setup\n" + "=" * 17)
    print(f"Configuration: {paths.config_file}\nData:          {paths.data_dir}\n")
    mode = args.mode
    if not mode:
        print("How will this computer be used?\n  1) Standalone - run everything here (recommended to start)\n"
              "  2) Coordinator only - central server; other computers do the AI work\n"
              "  3) Worker - add this computer's hardware to an existing HomeMesh\n"
              "  4) Client only - just use HomeMesh from a browser")
        mode = ask("Choose 1-4", "1", interactive=interactive)
    mode = MODES.get(mode)
    if mode is None:
        print("Unknown mode.")
        return 2
    cfg.node.name = args.name or ask("Name for this computer", cfg.node.name or socket.gethostname(),
                                     interactive=interactive)

    if mode == "client":
        cfg.node.roles = ["client"]
        save_config(cfg, paths.config_file)
        url = args.coordinator or ask("Coordinator address (e.g. https://192.168.1.10:8765)", "", interactive=interactive)
        print(f"\nNothing needs to run on this computer. Open {url or 'your coordinator address'} in a browser and "
              "use 'Install app' to add HomeMesh to your desktop or phone. For the CLI, create an API token in "
              "Settings → Security and run: homemesh --url <address> --token <token> status")
        return 0

    if mode == "worker":
        cfg.node.roles = ["worker"]
        url = args.coordinator or ask("Coordinator address (e.g. https://192.168.1.10:8765)", "", interactive=interactive)
        code = args.code or ask("Pairing code (from the coordinator's 'Add a computer' dialog)", "", interactive=interactive)
        fp = args.fingerprint or (ask("Coordinator TLS fingerprint (SHA256:...)", "", interactive=interactive)
                                  if url.startswith("https://") and not args.system_trust else None)
        if args.ollama_url:
            cfg.inference.backends = [BackendConfig(name="ollama", type="ollama", url=args.ollama_url)]
        for p in args.allow_path or []:
            cfg.worker.allowed_roots.append(str(Path(p).expanduser().resolve()))
        if args.mock:
            cfg.inference.mock_enabled = True
        save_config(cfg, paths.config_file)
        print(asyncio.run(_probe_backend(cfg)))
        from homemesh.worker.enroll import join

        try:
            creds = join(url, code, paths, node_name=cfg.node.name, fingerprint=fp, system_trust=args.system_trust,
                         allow_insecure_http=args.allow_insecure_http)
        except HomeMeshError as exc:
            print(f"\n✗ {exc.message}" + (f"\n  {exc.hint}" if exc.hint else ""))
            return 1
        cfg.worker.coordinator_url = creds.coordinator_url
        save_config(cfg, paths.config_file)
        print(f"\n✓ Joined {creds.coordinator_url} as node {creds.node_id}.")
        _offer_service(args, paths, interactive)
        print("Start the worker with: homemesh start   (or 'homemesh run' to watch it in this terminal)")
        return 0

    # standalone / coordinator
    cfg.node.roles = ["coordinator", "worker"] if mode == "standalone" else ["coordinator"]
    if args.port:
        cfg.server.port = args.port
    lan = args.lan if args.lan is not None else ask_yes(
        "Allow other devices on your home network (phones, other PCs) to connect? (enables TLS)",
        cfg.server.host not in ("127.0.0.1", "localhost"), interactive=interactive)
    if lan:
        cfg.server.host = "0.0.0.0"
        cfg.server.tls_mode = "self_signed"
    else:
        cfg.server.host = "127.0.0.1"
        if cfg.server.tls_mode == "self_signed" and not args.tls:
            cfg.server.tls_mode = "off"
    if args.tls:
        cfg.server.tls_mode = "self_signed"
    if args.ollama_url:
        cfg.inference.backends = [BackendConfig(name="ollama", type="ollama", url=args.ollama_url)]
    if args.mock:
        cfg.inference.mock_enabled = True
    for p in args.allow_path or []:
        cfg.worker.allowed_roots.append(str(Path(p).expanduser().resolve()))
    save_config(cfg, paths.config_file)
    print(f"  ✓ Wrote {paths.config_file}")
    if cfg.server.tls_mode == "self_signed":
        from homemesh.coordinator.tls import ensure_tls

        mat = ensure_tls(paths.tls_dir, cfg.server.tls_extra_sans)
        print(f"  ✓ TLS certificate ready. CA fingerprint (workers and browsers verify this):\n    {mat.ca_fingerprint}")
    if mode == "standalone":
        print("\nChecking local inference backend…")
        print(asyncio.run(_probe_backend(cfg)))
    username = args.username or ask("\nOwner username", "owner", interactive=interactive)
    try:
        password = read_password(args, interactive)
        _create_owner(paths, username, password)
    except HomeMeshError as exc:
        print(f"✗ {exc.message}")
        return 1
    _offer_service(args, paths, interactive)
    print(f"\nDone. Start HomeMesh with 'homemesh start' (or 'homemesh run') and open {cfg.advertised_url()}")
    if cfg.server.tls_mode == "self_signed":
        print("Your browser will warn about the private certificate until you trust the HomeMesh CA "
              "(see INSTALLATION.md → 'Trusting the HomeMesh certificate').")
    return 0


def _offer_service(args: argparse.Namespace, paths: AppPaths, interactive: bool) -> None:
    from homemesh.cli import service

    want = args.service if args.service is not None else ask_yes(
        "Start HomeMesh automatically when you log in?", False, interactive=interactive)
    if want:
        print("  " + service.install_service(paths))
