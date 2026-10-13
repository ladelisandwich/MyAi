"""``homemesh`` command-line interface.

Exit codes: 0 success - 1 error - 2 usage/configuration problem or degraded - 3 not running.
Run ``homemesh <command> --help`` for details on any command.
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import contextlib
import getpass
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from homemesh.common.config import config_to_dict, is_loopback_host, load_config, save_config
from homemesh.common.errors import ConfigError, HomeMeshError
from homemesh.common.paths import AppPaths, default_paths
from homemesh.version import PROTOCOL_VERSION, __version__

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_NOT_RUNNING = 0, 1, 2, 3

SYMBOL = {"ok": "✓", "warn": "!", "fail": "✗", "info": "•"}


def _out(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    elif isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, default=str))


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows, strict=False)] if rows else [len(h) for h in headers]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=False))
    out = [line, "  ".join("-" * w for w in widths)]
    for r in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=False)))
    return "\n".join(out)


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.paths: AppPaths = default_paths(args.home)
        self._cfg = None

    @property
    def cfg(self):
        if self._cfg is None:
            self._cfg = load_config(self.paths, config_file=Path(self.args.config) if self.args.config else None)
        return self._cfg

    def client(self):
        from homemesh.cli.client import CoordinatorClient

        url = self.args.url
        if url is None and not self.cfg.has_role("coordinator") and self.cfg.has_role("worker"):
            url = self.cfg.worker.coordinator_url
        return CoordinatorClient.local(self.cfg, self.paths, url=url, token=self.args.token, ca_file=self.args.ca_file)


def _resolve_node(client: Any, ref: str) -> dict[str, Any]:
    nodes = client.get("/api/v1/nodes")
    for n in nodes:
        if ref in (n["id"], n["name"]):
            return n
    matches = [n for n in nodes if n["name"].lower().startswith(ref.lower())]
    if len(matches) == 1:
        return matches[0]
    raise HomeMeshError(f"No computer matches '{ref}'.", code="not_found",
                        hint="See 'homemesh node list' for names and ids.")


# ---------------------------------------------------------------------------- commands

def cmd_version(ctx: Ctx) -> int:
    print(f"HomeMesh AI {__version__} (protocol v{PROTOCOL_VERSION}, Python {sys.version.split()[0]})")
    return EXIT_OK


def cmd_setup(ctx: Ctx) -> int:
    from homemesh.cli.setup import run_setup

    return run_setup(ctx.args, ctx.paths)


def cmd_run(ctx: Ctx) -> int:
    from homemesh.runtime import run_foreground

    roles = ctx.args.roles.split(",") if ctx.args.roles else None
    return run_foreground(ctx.cfg, ctx.paths, roles)


def _not_set_up(ctx: Ctx, what: str) -> int | None:
    """Refuse commands that would otherwise create a default (coordinator + worker) configuration on a
    computer that was never set up, and then start it."""
    if ctx.paths.config_file.exists():
        return None
    print(f"✗ HomeMesh is not set up on this computer yet, so there is nothing to {what}.\n"
          "  Worker: run the 'homemesh worker join …' line that 'homemesh node pair' prints on the coordinator.\n"
          "  Coordinator or single computer: run 'homemesh setup'.")
    return EXIT_USAGE


def cmd_start(ctx: Ctx) -> int:
    from homemesh.cli import service

    if (code := _not_set_up(ctx, "start")) is not None:
        return code
    print(service.start(ctx.paths))
    return EXIT_OK


def cmd_stop(ctx: Ctx) -> int:
    from homemesh.cli import service

    print(service.stop(ctx.paths, force=ctx.args.force))
    return EXIT_ERROR if service.read_pid(ctx.paths) else EXIT_OK


def cmd_restart(ctx: Ctx) -> int:
    from homemesh.cli import service

    if (code := _not_set_up(ctx, "restart")) is not None:
        return code
    print(service.stop(ctx.paths, force=ctx.args.force))
    if service.read_pid(ctx.paths):
        return EXIT_ERROR
    time.sleep(1)
    print(service.start(ctx.paths))
    return EXIT_OK


def cmd_status(ctx: Ctx) -> int:
    from homemesh.cli import service

    cfg = ctx.cfg
    pid = service.read_pid(ctx.paths)
    info: dict[str, Any] = {"roles": cfg.node.roles, "process": pid, "config": str(ctx.paths.config_file)}
    if not cfg.has_role("coordinator") and not ctx.args.url:
        info["mode"] = "worker" if cfg.has_role("worker") else "client"
        if cfg.has_role("worker"):
            info["worker_process"] = "running" if pid else "not running"
            from homemesh.cli.doctor import _worker_link

            c = _worker_link(cfg, ctx.paths)
            info["coordinator_link"] = f"{SYMBOL[c.status]} {c.detail}"
        _out(info, True) if ctx.args.json else print("\n".join(f"{k}: {v}" for k, v in info.items()))
        return EXIT_OK if pid or not cfg.has_role("worker") else EXIT_NOT_RUNNING
    client = ctx.client()
    try:
        health = client.get("/api/v1/system/health")
        st = client.get("/api/v1/system/status") if client._client.headers.get("Authorization") else None
    except HomeMeshError as exc:
        print(f"✗ {exc.message}" + (f"\n  {exc.hint}" if exc.hint else ""))
        return EXIT_NOT_RUNNING if exc.code == "not_running" else EXIT_ERROR
    finally:
        client.close()
    if ctx.args.json:
        _out({"health": health, "status": st}, True)
    else:
        print(f"HomeMesh {health['version']} at {client.base_url}: {health['status']}")
        if health.get("setup_required"):
            print("  Owner account not created yet: run 'homemesh setup' or open the web UI.")
        if st:
            print(f"  computers online: {st['nodes']['online']}/{st['nodes']['total']}   models available: "
                  f"{st['models']['available']}   jobs: {st['jobs']}")
            for n in st["nodes"]["items"]:
                print(f"   {'●' if n['online'] else '○'} {n['name']:<20} {n['mode']:<12} "
                      f"{'online' if n['online'] else 'offline'}  {len(n['running_jobs'])} job(s)")
            for w in st["warnings"]:
                print(f"  ! {w}")
    degraded = health["status"] != "ok" or bool(st and st["warnings"])
    return EXIT_USAGE if degraded else EXIT_OK


def cmd_doctor(ctx: Ctx) -> int:
    from homemesh.cli.doctor import run_doctor

    checks = run_doctor(ctx.paths)
    if ctx.args.json:
        _out([c.as_dict() for c in checks], True)
    else:
        print(f"HomeMesh doctor ({__version__})\n")
        for c in checks:
            print(f" {SYMBOL[c.status]} {c.name:<18} {c.detail}")
            if c.hint and c.status in ("warn", "fail", "info"):
                print(f"   {'':<18} → {c.hint}")
        fails = sum(c.status == "fail" for c in checks)
        warns = sum(c.status == "warn" for c in checks)
        print(f"\n{fails} problem(s), {warns} warning(s).")
    return EXIT_ERROR if any(c.status == "fail" for c in checks) else EXIT_OK


def cmd_logs(ctx: Ctx) -> int:
    log = ctx.paths.logs_dir / "homemesh.log"
    if not log.exists():
        print(f"No log file at {log}. With systemd you can also use: journalctl --user -u homemesh -f")
        return EXIT_ERROR
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-ctx.args.lines:]
    print("\n".join(lines))
    if ctx.args.follow:
        # Re-open the file for each read instead of holding it open: on Windows an open handle
        # stops HomeMesh from rotating its log. A file that got smaller was rotated: start over.
        pos = log.stat().st_size
        try:
            while True:
                time.sleep(0.5)
                try:
                    size = log.stat().st_size
                    if size < pos:
                        pos = 0
                    if size > pos:
                        with log.open("rb") as fh:
                            fh.seek(pos)
                            data = fh.read(size - pos)
                        pos += len(data)
                        print(data.decode("utf-8", errors="replace"), end="", flush=True)
                except FileNotFoundError:
                    pos = 0
        except KeyboardInterrupt:
            pass
    return EXIT_OK


def cmd_service(ctx: Ctx) -> int:
    from homemesh.cli import service

    if ctx.args.action == "install":
        print(service.install_service(ctx.paths))
    elif ctx.args.action == "uninstall":
        print(service.uninstall_service())
    else:
        print("installed" if service.service_installed() else "not installed")
    return EXIT_OK


def cmd_node(ctx: Ctx) -> int:
    a = ctx.args
    client = ctx.client()
    try:
        if a.action == "list":
            nodes = client.get("/api/v1/nodes")
            if a.json:
                _out(nodes, True)
            else:
                rows = []
                for n in nodes:
                    hw = n.get("hardware") or {}
                    gpus = ", ".join(f"{g['name']} ({g.get('vram_total_mb') or '?'} MB)" for g in hw.get("gpus", [])) or "none"
                    rows.append([n["name"], n["id"], "online" if n["online"] else "offline", n["mode"],
                                 f"{(hw.get('memory') or {}).get('total_mb') or '?'} MB", gpus, str(len(n["models"]))])
                print(_table(rows, ["NAME", "ID", "STATUS", "MODE", "RAM", "GPU", "MODELS"]))
        elif a.action == "pair":
            info = client.post("/api/v1/nodes/pairing", {"label": a.label or "", "ttl_s": a.ttl})
            if a.json:
                _out(info, True)
            else:
                print(f"Pairing code: {info['code']}   (single use, expires {info['expires_at']})")
                print(f"On the new computer run:\n  {info['join_command']}")
                if not info.get("tls_fingerprint"):
                    print("\nNote: this coordinator has no TLS; remote workers cannot join until you run "
                          "'homemesh tls init' and restart (or use a VPN and --allow-insecure-http).")
        elif a.action in ("pause", "resume", "mode"):
            node = _resolve_node(client, a.node)
            mode = {"pause": "paused", "resume": "balanced"}.get(a.action) or a.mode
            client.patch(f"/api/v1/nodes/{node['id']}", {"mode": mode})
            print(f"{node['name']}: mode set to {mode}")
        elif a.action == "rename":
            node = _resolve_node(client, a.node)
            client.patch(f"/api/v1/nodes/{node['id']}", {"name": a.new_name})
            print(f"Renamed {node['name']} to {a.new_name}")
        elif a.action == "revoke":
            node = _resolve_node(client, a.node)
            if not a.yes and input(f"Remove {node['name']} and revoke its credentials? [y/N] ").lower() != "y":
                return EXIT_USAGE
            client.post(f"/api/v1/nodes/{node['id']}/revoke")
            print(f"{node['name']} removed.")
    finally:
        client.close()
    return EXIT_OK


def cmd_worker(ctx: Ctx) -> int:
    a = ctx.args
    if a.action == "join":
        from homemesh.worker.enroll import join

        cfg = ctx.cfg
        name = a.name or cfg.node.name
        creds = join(a.coordinator, a.code, ctx.paths, node_name=name, fingerprint=a.fingerprint,
                     system_trust=a.system_trust, allow_insecure_http=a.allow_insecure_http)
        if cfg.has_role("coordinator") and ctx.paths.config_file.exists():
            print("Note: this computer was configured as a coordinator. After joining another coordinator it runs "
                  "as a worker only; its local coordinator data is kept but no longer started.")
        # A computer that joins a coordinator contributes as a worker; it must not start its own coordinator.
        cfg.node.roles = ["worker"]
        cfg.worker.coordinator_url = creds.coordinator_url
        cfg.node.name = name
        save_config(cfg, ctx.paths.config_file)
        print(f"✓ Joined {creds.coordinator_url} as '{name}' ({creds.node_id}).")
        print("Start the worker with 'homemesh start' (or 'homemesh run'). Authorise folders for document indexing "
              "with 'homemesh worker allow-path <folder>'.")
        return EXIT_OK
    if a.action == "discover":
        from homemesh.coordinator.discovery import discover

        found = discover(ctx.cfg.server.discovery_port, a.timeout)
        if not found:
            print("No coordinator answered. Discovery must be enabled on the coordinator "
                  "(server.discovery_enabled = true) and only works on the same network segment; "
                  "otherwise join with the coordinator's address.")
            return EXIT_USAGE
        for f in found:
            print(f"{f.get('name')}  {f['url']}  (HomeMesh {f.get('version')}, answered from {f['responder']})")
            if f.get("tls_fingerprint"):
                print(f"   fingerprint: {f['tls_fingerprint']}")
        print("\nDiscovery answers are not authenticated: compare the fingerprint with the coordinator's "
              "'Add a computer' dialog before joining.")
        return EXIT_OK
    if a.action == "leave":
        from homemesh.worker.enroll import leave

        print("Removed local credentials." if leave(ctx.paths) else "This computer was not joined.")
        print("Also remove it on the coordinator (Computers page or 'homemesh node revoke').")
        return EXIT_OK
    if a.action in ("allow-path", "disallow-path", "allow-tool", "disallow-tool") and (
            code := _not_set_up(ctx, "configure")) is not None:
        return code
    if a.action == "allow-path":
        from homemesh.knowledge.pathsafety import validate_new_root

        cfg = ctx.cfg
        try:
            p = str(validate_new_root(Path(a.path)))
        except PermissionError as exc:
            print(f"✗ {exc}")
            return EXIT_USAGE
        if p not in cfg.worker.allowed_roots:
            cfg.worker.allowed_roots = [*cfg.worker.allowed_roots, p]
            save_config(cfg, ctx.paths.config_file)
        print(f"✓ {p} may now be indexed by HomeMesh on this computer. Restart the worker to apply "
              "('homemesh restart').")
        return EXIT_OK
    if a.action in ("allow-tool", "disallow-tool"):
        from homemesh.tools.registry import ToolRegistry

        cfg = ctx.cfg
        node_tools = sorted(t.name for t in ToolRegistry().all() if t.location == "node")
        if a.tool not in node_tools:
            print(f"✗ '{a.tool}' is not a tool that runs on a computer. Choose one of: {', '.join(node_tools)}")
            return EXIT_USAGE
        if a.action == "allow-tool":
            if a.tool not in cfg.worker.allowed_tools:
                cfg.worker.allowed_tools = [*cfg.worker.allowed_tools, a.tool]
                save_config(cfg, ctx.paths.config_file)
            print(f"✓ '{a.tool}' may now run on this computer. Restart HomeMesh to apply ('homemesh restart').")
            if a.tool == "web_search" and not cfg.web.searxng_url:
                print("  Web search also needs your SearXNG address: homemesh web set-searxng http://127.0.0.1:8888")
            if a.tool == "open_url":
                print("  Every link is shown to you with an Approve button before the browser opens it.")
        else:
            cfg.worker.allowed_tools = [t for t in cfg.worker.allowed_tools if t != a.tool]
            save_config(cfg, ctx.paths.config_file)
            print(f"✓ '{a.tool}' is no longer allowed on this computer. Restart HomeMesh to apply.")
        return EXIT_OK
    if a.action == "disallow-path":
        cfg = ctx.cfg
        cfg.worker.allowed_roots = [r for r in cfg.worker.allowed_roots if r != a.path]
        save_config(cfg, ctx.paths.config_file)
        print("Updated. Restart the worker to apply.")
        return EXIT_OK
    # status
    from homemesh.worker.credentials import WorkerCredentials

    try:
        creds = WorkerCredentials.load(ctx.paths.worker_credentials_file)
        print(f"Joined to {creds.coordinator_url} as node {creds.node_id}"
              + (f" (CA pinned: {creds.ca_fingerprint})" if creds.ca_fingerprint else ""))
    except ConfigError as exc:
        print(exc.message)
    print(f"Allowed folders: {', '.join(ctx.cfg.worker.allowed_roots) or 'none'}")
    print(f"Allowed tools:   {', '.join(ctx.cfg.worker.allowed_tools) or 'none'}")
    print(f"Mode: {ctx.cfg.worker.mode}   max concurrent jobs: {ctx.cfg.worker.max_concurrent_jobs}")
    return EXIT_OK


def cmd_web(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.cfg
    if a.action == "set-searxng":
        if (code := _not_set_up(ctx, "configure")) is not None:
            return code
        url = "" if a.url.lower() in ("none", "off", "") else a.url
        try:
            cfg.web.searxng_url = url
        except ValueError as exc:
            print(f"✗ {exc}")
            return EXIT_USAGE
        save_config(cfg, ctx.paths.config_file)
        print(f"✓ web.searxng_url = {cfg.web.searxng_url or '(not set)'}. Restart HomeMesh to apply ('homemesh restart').")
        host = urlsplit(cfg.web.searxng_url).hostname or ""
        if cfg.web.searxng_url and not is_loopback_host(host):
            print(f"  Note: {host} is not this computer. Every search query goes there"
                  + (" unencrypted" if cfg.web.searxng_url.startswith("http://") else "")
                  + "; the HomeMesh SearXNG installer keeps it on 127.0.0.1.")
        if cfg.web.searxng_url and "web_search" not in cfg.worker.allowed_tools:
            print("  Allow the tool on this computer too: homemesh worker allow-tool web_search")
        return EXIT_OK
    # test: run the real tools locally, outside the coordinator, so the owner can check the setup.
    import asyncio

    from homemesh.tools.base import ToolContext
    from homemesh.tools.builtin.web import FETCH_URL, WEB_SEARCH
    from homemesh.tools.registry import ToolArgumentError, ToolRegistry

    tctx = ToolContext(web=cfg.web)
    try:
        if a.fetch:
            args = ToolRegistry.validate_args(FETCH_URL, {"url": a.fetch, "max_chars": 1500})
            out = asyncio.run(asyncio.wait_for(FETCH_URL.handler(tctx, args), FETCH_URL.timeout_s))
            print(f"✓ {out['final_url']} (HTTP {out['status']}, {out['content_type']})")
            print(f"  Title: {out['title'] or '(none)'}")
            print("  " + out["content"][:600].replace("\n", "\n  "))
            return EXIT_OK
        args = ToolRegistry.validate_args(WEB_SEARCH, {"query": a.query, "max_results": 5})
        out = asyncio.run(asyncio.wait_for(WEB_SEARCH.handler(tctx, args), WEB_SEARCH.timeout_s))
    except TimeoutError:
        print("✗ No answer in time (the site or SearXNG is too slow).")
        return EXIT_ERROR
    except (ToolArgumentError, OSError, ValueError) as exc:
        print(f"✗ {exc}")
        return EXIT_ERROR
    if a.json:
        _out(out, True)
        return EXIT_OK
    print(f"✓ SearXNG at {cfg.web.searxng_url} answered: {len(out['results'])} result(s) for {a.query!r}")
    for r in out["results"]:
        print(f"  - {r['title']}\n    {r['url']}")
    for e in out.get("unresponsive_engines", []):
        print(f"  ! engine did not answer: {e}")
    if "web_search" not in cfg.worker.allowed_tools:
        print("  Note: web_search is not in worker.allowed_tools here yet: homemesh worker allow-tool web_search")
    if not out["results"]:
        print("  ! no results: SearXNG works, but the search engines it asked returned nothing this time.")
    return EXIT_OK


def _wait_job(client: Any, job_id: str, label: str) -> int:
    last = ""
    while True:
        j = client.get(f"/api/v1/jobs/{job_id}")
        msg = f"{label}: {j['status']}" + (f" - {j.get('progress_message')}" if j.get("progress_message") else "")
        if j.get("progress") is not None:
            msg += f" ({j['progress'] * 100:.0f}%)"
        if msg != last:
            print(msg)
            last = msg
        if j["status"] in ("completed", "failed", "cancelled", "interrupted"):
            if j["status"] != "completed":
                print(f"✗ {j.get('error_code')}: {j.get('error')}")
                return EXIT_ERROR
            return EXIT_OK
        time.sleep(1)


def cmd_model(ctx: Ctx) -> int:
    a = ctx.args
    client = ctx.client()
    try:
        if a.action == "list":
            data = client.get("/api/v1/models")
            if a.json:
                _out(data, True)
                return EXIT_OK
            rows = []
            for m in data["models"]:
                where = ", ".join(f"{i['node_name']}{'*' if i['loaded'] else ''}" for i in m["instances"])
                rows.append([("[MOCK] " if m["mock"] else "") + m["name"], ",".join(m["capabilities"]),
                             m.get("parameter_size") or "-", str(m.get("context_length") or "-"), where,
                             ",".join(m["roles"]) or "-"])
            print(_table(rows, ["MODEL", "CAPABILITIES", "PARAMS", "CONTEXT", "NODES (*=loaded)", "ROLES"]))
        elif a.action == "roles":
            _out(client.get("/api/v1/models")["roles"], True)
        elif a.action == "set-role":
            r = client.put("/api/v1/models/roles", {"roles": {a.role: None if a.model in ("auto", "none") else a.model}})
            for w in r.get("warnings", []):
                print(f"! {w}")
            print(f"{a.role} -> {a.model}")
        else:
            node = _resolve_node(client, a.node) if a.node else None
            if node is None:
                nodes = [n for n in client.get("/api/v1/nodes") if n["online"]]
                if len(nodes) != 1:
                    raise HomeMeshError("Several computers are online; choose one with --node.", code="usage")
                node = nodes[0]
            backend = a.backend or next((b["name"] for b in node.get("backends") or [] if b.get("type") == "ollama"),
                                        "ollama")
            if a.action == "delete" and not a.yes and input(f"Delete {a.model} from {node['name']}? [y/N] ").lower() != "y":
                return EXIT_USAGE
            r = client.post("/api/v1/models/manage", {"action": a.action, "node_id": node["id"], "backend": backend,
                                                      "model": a.model})
            return _wait_job(client, r["job_id"], f"{a.action} {a.model} on {node['name']}")
    finally:
        client.close()
    return EXIT_OK


def cmd_jobs(ctx: Ctx) -> int:
    a = ctx.args
    client = ctx.client()
    try:
        if a.action == "list":
            q = []
            if a.status:
                q.append(f"status={a.status}")
            if a.type:
                q.append(f"type={a.type}")
            data = client.get("/api/v1/jobs?" + "&".join(q) + f"&limit={a.limit}")
            if a.json:
                _out(data, True)
            else:
                rows = [[j["id"], j["type"], j["status"], j.get("node_name") or "-", j.get("model") or "-",
                         j["created_at"][:19].replace("T", " "), (j.get("error_code") or "")] for j in data["items"]]
                print(_table(rows, ["ID", "TYPE", "STATUS", "NODE", "MODEL", "CREATED (UTC)", "ERROR"]))
        elif a.action == "show":
            _out(client.get(f"/api/v1/jobs/{a.job_id}"), True)
        elif a.action == "cancel":
            _out(client.post(f"/api/v1/jobs/{a.job_id}/cancel"), a.json)
    finally:
        client.close()
    return EXIT_OK


def _passphrase(args: argparse.Namespace, confirm: bool) -> str | None:
    if getattr(args, "passphrase_stdin", False):
        return sys.stdin.readline().rstrip("\r\n") or None
    if getattr(args, "encrypt", False):
        p = getpass.getpass("Backup passphrase: ")
        if confirm and getpass.getpass("Repeat passphrase: ") != p:
            raise HomeMeshError("Passphrases do not match.", code="usage")
        return p
    return None


def cmd_backup(ctx: Ctx) -> int:
    from homemesh.coordinator.backup import create_backup, list_backups, verify_backup

    a = ctx.args
    if a.action == "list":
        rows = [[b["name"], f"{b['size'] / 2**20:.1f} MB", "yes" if b["encrypted"] else "no", b["created_at"]]
                for b in list_backups(Path(a.dest) if a.dest else ctx.paths.backups_dir)]
        print(_table(rows, ["FILE", "SIZE", "ENCRYPTED", "CREATED"]) if rows else "No backups yet.")
        return EXIT_OK
    if a.action == "verify":
        manifest = verify_backup(Path(a.file), _passphrase(a, confirm=False) if a.file.endswith(".enc") else None)
        print(f"✓ Backup is intact: {len(manifest['files'])} files, schema v{manifest['schema_version']}, "
              f"created {manifest['created_at']} by HomeMesh {manifest['app_version']}")
        return EXIT_OK
    passphrase = _passphrase(a, confirm=True)
    info = create_backup(ctx.paths, Path(a.dest) if a.dest else None, passphrase=passphrase,
                         include_secrets=not a.no_secrets)
    print(f"✓ Backup written to {info.path} ({info.path.stat().st_size / 2**20:.1f} MB, "
          f"{'encrypted' if passphrase else 'NOT encrypted'})")
    if not passphrase and not a.no_secrets:
        print("! This backup contains private keys and personal data. Store it safely, or use --encrypt.")
    return EXIT_OK


def cmd_restore(ctx: Ctx) -> int:
    from homemesh.cli import service
    from homemesh.coordinator.backup import restore_backup

    a = ctx.args
    if service.read_pid(ctx.paths):
        print("✗ HomeMesh is running. Stop it first ('homemesh stop').")
        return EXIT_USAGE
    if not a.yes and input(f"Restore {a.file}? Current data will be moved aside (not deleted). [y/N] ").lower() != "y":
        return EXIT_USAGE
    passphrase = _passphrase(a, confirm=False) if a.file.endswith(".enc") or a.passphrase_stdin else None
    if a.file.endswith(".enc") and passphrase is None:
        passphrase = getpass.getpass("Backup passphrase: ")
    res = restore_backup(Path(a.file), ctx.paths, passphrase=passphrase)
    m = res["manifest"]
    print(f"✓ Restored backup from {m['created_at']} (HomeMesh {m['app_version']}, schema v{m['schema_version']}).")
    print(f"  Previous data was moved to {res['previous_data_moved_to']}")
    print("  Start HomeMesh again with 'homemesh start'. Database migrations run automatically if needed.")
    return EXIT_OK


def cmd_user(ctx: Ctx) -> int:
    from homemesh.common.security import hash_password, validate_password_strength
    from homemesh.coordinator.db.database import Database
    from homemesh.coordinator.db.migrate import migrate
    from homemesh.coordinator.db.repositories import Repositories

    a = ctx.args
    if a.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            print("✗ Passwords do not match.")
            return EXIT_USAGE
    if err := validate_password_strength(password):
        print(f"✗ {err}")
        return EXIT_USAGE

    async def go() -> int:
        from homemesh.coordinator.auth import AuthService

        ctx.paths.ensure()
        db = Database(ctx.paths.db_file)
        await db.open()
        try:
            await db.run(lambda c: migrate(c, ctx.paths.backups_dir))
            repos = Repositories.create(db)
            if a.action == "create-owner":
                auth = AuthService(repos, ctx.paths, session_ttl_hours=24, login_attempts_per_minute=10,
                                   secure_cookies=False)
                await auth.create_owner(a.username or "owner", password)
                await auth.ensure_cli_token()
                print(f"✓ Owner '{a.username or 'owner'}' created.")
            else:
                user = await (repos.users.by_username(a.username) if a.username else repos.users.owner())
                if not user:
                    print("✗ No such user.")
                    return EXIT_USAGE
                await repos.users.set_password(user["id"], hash_password(password))
                await repos.sessions.revoke_all_for_user(user["id"])
                await repos.audit.add("cli", user["id"], "auth.password_reset")
                print(f"✓ Password for '{user['username']}' reset; all sessions signed out.")
            return EXIT_OK
        finally:
            await db.close()

    return asyncio.run(go())


def cmd_tls(ctx: Ctx) -> int:
    from homemesh.coordinator.tls import ca_fingerprint, ensure_tls

    if ctx.args.action == "init":
        cfg = ctx.cfg
        extra = list(dict.fromkeys([*cfg.server.tls_extra_sans, *(ctx.args.san or [])]))
        if ctx.args.new_ca and ctx.paths.tls_dir.exists():
            from homemesh.cli import service

            if service.read_pid(ctx.paths):
                print("✗ Stop HomeMesh first ('homemesh stop'), then run this again.")
                return EXIT_USAGE
            aside = ctx.paths.tls_dir.with_name(f"tls-replaced-{time.strftime('%Y%m%d-%H%M%S')}")
            ctx.paths.tls_dir.rename(aside)
            print(f"  Previous certificate authority moved to {aside} (not deleted).")
        mat = ensure_tls(ctx.paths.tls_dir, extra)
        cfg.server.tls_mode = "self_signed"
        cfg.server.tls_extra_sans = extra
        save_config(cfg, ctx.paths.config_file)
        print(f"✓ TLS enabled (tls_mode = self_signed). Certificate names: {', '.join(mat.sans)}")
        print(f"  CA fingerprint: {mat.ca_fingerprint}")
        print("  To accept connections from other devices also set server.host = \"0.0.0.0\" (or run 'homemesh setup'),"
              " then restart.")
        if ctx.args.new_ca:
            print("  New CA: every worker must join again ('homemesh worker leave' then 'worker join' with a new code),\n"
                  "  and devices that trusted the old CA need the new one (INSTALLATION.md, 'Trusting the HomeMesh "
                  "certificate').")
        return EXIT_OK
    ca = ctx.paths.tls_dir / "ca.pem"
    if not ca.exists():
        print("No local CA yet. Run 'homemesh tls init'.")
        return EXIT_USAGE
    print(f"CA certificate: {ca}\nFingerprint:    {ca_fingerprint(ca)}")
    return EXIT_OK


def cmd_config(ctx: Ctx) -> int:
    a = ctx.args
    if a.action == "path":
        print(ctx.paths.config_file)
        return EXIT_OK
    try:
        cfg = ctx.cfg
    except ConfigError as exc:
        print(f"✗ {exc.message}\n  {exc.hint or ''}")
        return EXIT_USAGE
    if a.action == "validate":
        problems = cfg.security_problems()
        for p in problems:
            print(f"✗ {p}")
        if not problems:
            print(f"✓ {ctx.paths.config_file} is valid.")
        return EXIT_USAGE if problems else EXIT_OK
    _out(config_to_dict(cfg), True)
    return EXIT_OK


# ---------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="homemesh", description="HomeMesh AI - your private, multi-computer personal AI.")
    p.add_argument("--home", help="single directory for config + data (default: platform locations or $HOMEMESH_HOME)")
    p.add_argument("--config", help="path to homemesh.toml")
    p.add_argument("--url", help="coordinator URL for remote management (default: this computer)")
    p.add_argument("--token", help="API token for --url (create one in Settings → Security)")
    p.add_argument("--ca-file", help="CA certificate to trust for --url")
    p.add_argument("--json", action="store_true", help="machine-readable output where supported")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("setup", help="interactive first-time setup (standalone, coordinator, worker or client)")
    s.add_argument("--mode", choices=["standalone", "coordinator", "worker", "client"])
    s.add_argument("--name", help="name for this computer")
    s.add_argument("--non-interactive", action="store_true")
    s.add_argument("--username")
    s.add_argument("--password-stdin", action="store_true", help="read the owner password from stdin")
    s.add_argument("--port", type=int)
    s.add_argument("--lan", dest="lan", action="store_true", default=None, help="listen on the home network (enables TLS)")
    s.add_argument("--local-only", dest="lan", action="store_false", help="listen on this computer only")
    s.add_argument("--tls", action="store_true", help="enable TLS even for local-only use")
    s.add_argument("--ollama-url", help="Ollama address (default http://127.0.0.1:11434)")
    s.add_argument("--allow-path", action="append", help="authorise a folder for document indexing (repeatable)")
    s.add_argument("--mock", action="store_true", help="enable the MOCK backend (testing only)")
    s.add_argument("--service", dest="service", action="store_true", default=None, help="install autostart")
    s.add_argument("--no-service", dest="service", action="store_false")
    s.add_argument("--coordinator", help="worker/client: coordinator URL")
    s.add_argument("--code", help="worker: pairing code")
    s.add_argument("--fingerprint", help="worker: coordinator CA fingerprint")
    s.add_argument("--system-trust", action="store_true", help="worker: coordinator uses a publicly trusted certificate")
    s.add_argument("--allow-insecure-http", action="store_true")
    s.set_defaults(func=cmd_setup)

    r = sub.add_parser("run", help="run HomeMesh in the foreground (used by services)")
    r.add_argument("--roles", help="comma-separated roles to run (default: from config)")
    r.set_defaults(func=cmd_run)
    for name, fn, help_ in (("start", cmd_start, "start in the background (service if installed)"),
                            ("stop", cmd_stop, "stop HomeMesh"), ("restart", cmd_restart, "restart HomeMesh")):
        sp = sub.add_parser(name, help=help_)
        if name != "start":
            sp.add_argument("--force", action="store_true", help="terminate forcibly if a clean stop takes over 30s")
        sp.set_defaults(func=fn)
    sub.add_parser("status", help="show whether HomeMesh is running and healthy").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="diagnose installation, GPU, backends, network and database").set_defaults(func=cmd_doctor)
    lg = sub.add_parser("logs", help="show the log file")
    lg.add_argument("-f", "--follow", action="store_true")
    lg.add_argument("-n", "--lines", type=int, default=100)
    lg.set_defaults(func=cmd_logs)
    sub.add_parser("version", help="print version information").set_defaults(func=cmd_version)

    sv = sub.add_parser("service", help="install/uninstall autostart (systemd user unit / Windows per-user autostart entry)")
    sv.add_argument("action", choices=["install", "uninstall", "status"])
    sv.set_defaults(func=cmd_service)

    n = sub.add_parser("node", help="manage computers (run on the coordinator)")
    nsub = n.add_subparsers(dest="action", required=True)
    nsub.add_parser("list")
    pp = nsub.add_parser("pair", help="create a one-time pairing code for a new worker")
    pp.add_argument("--label", default="")
    pp.add_argument("--ttl", type=int, default=600)
    for act in ("pause", "resume"):
        nsub.add_parser(act).add_argument("node")
    nm = nsub.add_parser("mode")
    nm.add_argument("node")
    nm.add_argument("mode", choices=["performance", "balanced", "background", "paused"])
    rn = nsub.add_parser("rename")
    rn.add_argument("node")
    rn.add_argument("new_name")
    rv = nsub.add_parser("revoke")
    rv.add_argument("node")
    rv.add_argument("--yes", action="store_true")
    n.set_defaults(func=cmd_node)

    w = sub.add_parser("worker", help="join/leave a coordinator and manage local permissions")
    wsub = w.add_subparsers(dest="action", required=True)
    j = wsub.add_parser("join")
    j.add_argument("--coordinator", required=True)
    j.add_argument("--code", required=True)
    j.add_argument("--fingerprint")
    j.add_argument("--name")
    j.add_argument("--system-trust", action="store_true")
    j.add_argument("--allow-insecure-http", action="store_true")
    wsub.add_parser("leave")
    dsc = wsub.add_parser("discover", help="find coordinators on this network (if they enabled discovery)")
    dsc.add_argument("--timeout", type=float, default=2.0)
    wsub.add_parser("status")
    ap = wsub.add_parser("allow-path")
    ap.add_argument("path")
    dp = wsub.add_parser("disallow-path")
    dp.add_argument("path")
    at = wsub.add_parser("allow-tool", help="let a tool run on this computer (e.g. web_search, fetch_url, open_url)")
    at.add_argument("tool")
    dt = wsub.add_parser("disallow-tool")
    dt.add_argument("tool")
    w.set_defaults(func=cmd_worker)

    m = sub.add_parser("model", help="list and manage models")
    msub = m.add_subparsers(dest="action", required=True)
    msub.add_parser("list")
    msub.add_parser("roles")
    sr = msub.add_parser("set-role")
    sr.add_argument("role", choices=["chat", "reasoning", "coding", "fast", "embedding", "vision"])
    sr.add_argument("model", help="model name, or 'auto'")
    for act in ("pull", "delete", "load", "unload"):
        mp = msub.add_parser(act)
        mp.add_argument("model")
        mp.add_argument("--node")
        mp.add_argument("--backend")
        mp.add_argument("--yes", action="store_true")
    m.set_defaults(func=cmd_model)

    jb = sub.add_parser("jobs", help="list, inspect and cancel jobs")
    jsub = jb.add_subparsers(dest="action", required=True)
    jl = jsub.add_parser("list")
    jl.add_argument("--status")
    jl.add_argument("--type")
    jl.add_argument("--limit", type=int, default=50)
    jsub.add_parser("show").add_argument("job_id")
    jsub.add_parser("cancel").add_argument("job_id")
    jb.set_defaults(func=cmd_jobs)

    b = sub.add_parser("backup", help="create, list or verify backups")
    b.add_argument("action", nargs="?", default="create", choices=["create", "list", "verify"])
    b.add_argument("file", nargs="?", help="backup file (verify)")
    b.add_argument("--dest", help="destination folder (default: <data>/backups)")
    b.add_argument("--encrypt", action="store_true", help="encrypt with a passphrase (prompted)")
    b.add_argument("--passphrase-stdin", action="store_true")
    b.add_argument("--no-secrets", action="store_true", help="exclude TLS keys and node credentials")
    b.set_defaults(func=cmd_backup)
    rs = sub.add_parser("restore", help="restore a backup (HomeMesh must be stopped)")
    rs.add_argument("file")
    rs.add_argument("--yes", action="store_true")
    rs.add_argument("--passphrase-stdin", action="store_true")
    rs.set_defaults(func=cmd_restore)

    u = sub.add_parser("user", help="owner account management (on the coordinator computer)")
    u.add_argument("action", choices=["create-owner", "reset-password"])
    u.add_argument("--username")
    u.add_argument("--password-stdin", action="store_true")
    u.set_defaults(func=cmd_user)

    t = sub.add_parser("tls", help="local TLS certificates")
    t.add_argument("action", choices=["init", "show"])
    t.add_argument("--san", action="append", help="extra host name or IP for the certificate (repeatable)")
    t.add_argument("--new-ca", action="store_true",
                   help="init: replace the certificate authority (e.g. if its key may have leaked); the old one is kept aside")
    t.set_defaults(func=cmd_tls)

    wb = sub.add_parser("web", help="set up and test internet access for the assistant (see docs/WEB_ACCESS.md)")
    wbsub = wb.add_subparsers(dest="action", required=True)
    ss = wbsub.add_parser("set-searxng", help="address of your SearXNG, e.g. http://127.0.0.1:8888 ('none' to clear)")
    ss.add_argument("url")
    wt = wbsub.add_parser("test", help="run a real web search (or --fetch URL) from this computer")
    wt.add_argument("query", nargs="?", default="HomeMesh self-hosted AI")
    wt.add_argument("--fetch", metavar="URL", help="fetch this page instead of searching")
    wb.set_defaults(func=cmd_web)

    c = sub.add_parser("config", help="show, locate or validate homemesh.toml")
    c.add_argument("action", nargs="?", default="show", choices=["show", "path", "validate"])
    c.set_defaults(func=cmd_config)
    return p


DETACHED_ENV = "HOMEMESH_DETACHED"  # same name as cli.service.DETACHED_ENV
_ASCII_FALLBACK = {"✓": "[ok]", "✗": "[x]", "●": "*", "○": "o", "→": "->", "…": "...", "·": "-", "—": "-",
                   "\u2013": "-", "≈": "~", "≥": ">=", "≤": "<="}


def _ascii_fallback(exc: UnicodeError) -> tuple[str, int]:
    if not isinstance(exc, UnicodeEncodeError):
        raise exc
    text = exc.object[exc.start:exc.end]
    return "".join(_ASCII_FALLBACK.get(ch, "?") for ch in text), exc.end


def _console_output_codepage() -> int:
    """The code page Windows PowerShell and cmd use to read a program's piped output (0 = none)."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleOutputCP())  # type: ignore[attr-defined]
    except (OSError, AttributeError, ValueError):
        return 0


def _make_output_safe() -> None:
    """Never crash on output: a Windows console redirected to a file or pipe uses a legacy code
    page (e.g. cp1252) that cannot encode symbols such as ✓, so map them to ASCII there.

    On Windows, piped output (e.g. ``notepad (homemesh config path)`` in PowerShell) is also written
    in the console's code page, which is what PowerShell decodes it with; Python would otherwise use
    the ANSI code page and a folder name such as "José" would come out garbled."""
    codecs.register_error("homemesh_ascii", _ascii_fallback)
    cp = 0 if os.environ.get("PYTHONIOENCODING") else _console_output_codepage()
    for stream in (sys.stdout, sys.stderr):
        if stream is None:  # pythonw has no console streams
            continue
        if cp and not stream.isatty():
            with contextlib.suppress(AttributeError, ValueError, LookupError):
                stream.reconfigure(encoding=f"cp{cp}" if cp != 65001 else "utf-8")
        try:
            "✓✗●→…".encode(stream.encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            with contextlib.suppress(AttributeError, ValueError):
                stream.reconfigure(errors="homemesh_ascii")


def _capture_console(argv: list[str]) -> None:
    """Background runs (the Windows autostart entry runs pythonw.exe, which has no console at all;
    'homemesh start' detaches) would lose anything printed before logging starts, such as a broken
    homemesh.toml. Send it to <data>/logs/console.log instead, kept small."""
    from datetime import datetime

    from homemesh.common.paths import default_paths

    try:
        logs = default_paths().logs_dir
        logs.mkdir(parents=True, exist_ok=True)
        path = logs / "console.log"
        if path.exists() and path.stat().st_size > 1024 * 1024:
            os.replace(path, logs / "console.log.1")
        fh = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return
    fh.write(f"--- {datetime.now().isoformat(timespec='seconds')} pid {os.getpid()}: homemesh {' '.join(argv)}\n")
    sys.stdout = sys.stderr = fh
    os.environ[DETACHED_ENV] = "1"


def main(argv: list[str] | None = None) -> int:
    _make_output_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    if args.home:
        os.environ["HOMEMESH_HOME"] = args.home
    if sys.stdout is None or sys.stderr is None or os.environ.get(DETACHED_ENV) == "1":
        _capture_console(sys.argv[1:] if argv is None else argv)
    try:
        return int(args.func(Ctx(args)) or 0)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:  # output piped into e.g. `head`, which exited early
        with contextlib.suppress(OSError):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return EXIT_OK
    except HomeMeshError as exc:
        print(f"✗ {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"  → {exc.hint}", file=sys.stderr)
        return EXIT_NOT_RUNNING if exc.code == "not_running" else (EXIT_USAGE if exc.code in ("config_error", "usage") else EXIT_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
