"""``homemesh doctor``: diagnose this computer's HomeMesh installation.

Each check returns OK / WARN / FAIL with a one-line explanation and, when something is
wrong, an actionable hint. Nothing is changed by running the doctor.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import socket
import sqlite3
import ssl
import sys
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from homemesh.common.config import HomeMeshConfig, is_loopback_host, load_config
from homemesh.common.errors import ConfigError
from homemesh.common.paths import AppPaths
from homemesh.version import SUPPORTED_PROTOCOL_VERSIONS

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "hint": self.hint}


def _python() -> Check:
    v = sys.version_info
    if v < (3, 11):
        return Check("Python", FAIL, sys.version.split()[0], "HomeMesh needs Python 3.11 or newer.")
    return Check("Python", OK, f"{sys.version.split()[0]} at {sys.executable}")


def _deps() -> Check:
    missing, versions = [], []
    for mod in ("fastapi", "uvicorn", "pydantic", "httpx", "websockets", "psutil", "argon2", "cryptography", "numpy",
                "pypdf"):
        try:
            m = importlib.import_module(mod)
            versions.append(f"{mod} {getattr(m, '__version__', '?')}")
        except ImportError:
            missing.append(mod)
    if missing:
        return Check("Dependencies", FAIL, "missing: " + ", ".join(missing),
                     "Reinstall: pip install --upgrade homemesh (inside the HomeMesh virtual environment).")
    return Check("Dependencies", OK, ", ".join(versions))


def _os() -> Check:
    from homemesh.hardware.osinfo import detect_os

    o = detect_os()
    extra = []
    if o.immutable:
        extra.append("immutable OS (user-space install recommended)")
    if o.in_container:
        extra.append("running in a container")
    return Check("Operating system", INFO, f"{o.distro_name or o.system} {o.arch}" + (f" - {', '.join(extra)}" if extra else ""))


def _config(paths: AppPaths) -> tuple[Check, HomeMeshConfig | None]:
    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        return Check("Configuration", FAIL, exc.message, exc.hint or ""), None
    if not paths.config_file.exists():
        return Check("Configuration", WARN, f"{paths.config_file} does not exist (using defaults)",
                     "Run 'homemesh setup' to create it."), cfg
    problems = cfg.security_problems()
    if problems:
        return Check("Configuration", FAIL, problems[0]), cfg
    return Check("Configuration", OK, f"{paths.config_file} (roles: {', '.join(cfg.node.roles)})"), cfg


def _data_dir(paths: AppPaths) -> Check:
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        probe = paths.data_dir / ".write-test"
        probe.write_text("x")
        probe.unlink()
    except OSError as exc:
        return Check("Data directory", FAIL, f"{paths.data_dir} is not writable: {exc}",
                     "Fix the folder permissions or set HOMEMESH_HOME to a writable folder.")
    if os.name == "posix" and paths.secrets_dir.exists() and paths.secrets_dir.stat().st_mode & 0o077:
        return Check("Data directory", WARN, f"{paths.secrets_dir} is readable by other users",
                     f"Run: chmod 700 {paths.secrets_dir}")
    import shutil

    free = shutil.disk_usage(paths.data_dir).free / 2**30
    status = WARN if free < 2 else OK
    return Check("Data directory", status, f"{paths.data_dir} ({free:.1f} GB free)",
                 "Less than 2 GB free: indexing and backups may fail." if status == WARN else "")


def _database(paths: AppPaths) -> Check:
    from homemesh.coordinator.db.migrate import LATEST_SCHEMA_VERSION

    if not paths.db_file.exists():
        return Check("Database", WARN, "not created yet", "It is created on the coordinator's first start.")
    try:
        conn = sqlite3.connect(f"file:{paths.db_file}?mode=ro", uri=True, timeout=5)
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
            fts = conn.execute("SELECT sqlite_version()").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("Database", FAIL, f"cannot open {paths.db_file}: {exc}",
                     "See TROUBLESHOOTING.md 'Database problems'; restore from a backup if it is corrupted.")
    if integrity != "ok":
        return Check("Database", FAIL, f"integrity check failed: {integrity}",
                     "Stop HomeMesh and restore the latest backup ('homemesh restore').")
    if version > LATEST_SCHEMA_VERSION:
        return Check("Database", FAIL, f"schema v{version} is newer than this software (v{LATEST_SCHEMA_VERSION})",
                     "Upgrade HomeMesh.")
    status = OK if version == LATEST_SCHEMA_VERSION else WARN
    return Check("Database", status, f"{paths.db_file} - integrity ok, schema v{version}/{LATEST_SCHEMA_VERSION}, "
                                     f"SQLite {fts}", "Pending migrations run on next start." if status == WARN else "")


def _gpus(cfg: HomeMeshConfig | None = None) -> list[Check]:
    from homemesh.hardware.detect import detect_gpus, mark_ignored_gpus

    gpus, warnings = detect_gpus()
    mark_ignored_gpus(gpus, cfg.worker.ignore_gpus if cfg else [])
    out = []
    if not gpus:
        out.append(Check("GPU", INFO, "no GPU detected - CPU inference only on this computer",
                         "If this computer has a GPU: install the vendor driver (NVIDIA: nvidia-smi must work)."))
    for g in gpus:
        vram = f"{g.vram_total_mb} MB VRAM" if g.vram_total_mb else "VRAM unknown"
        ignored = " - ignored for scheduling (worker.ignore_gpus)" if g.ignored else ""
        out.append(Check(f"GPU {g.index}", OK if g.vram_total_mb else WARN, f"{g.name} ({g.vendor}, {vram}, via {g.source}){ignored}",
                         "; ".join(f"{k}: {v}" for k, v in g.unavailable.items())))
    for w in warnings:
        if "No GPU detected" not in w:
            out.append(Check("GPU", WARN, w))
    return out


async def _backends(cfg: HomeMeshConfig) -> list[Check]:
    from homemesh.inference.factory import build_backends

    out = []
    backends = build_backends(cfg)
    for b in backends.values():
        st = await b.health()
        if st.ok:
            try:
                models = await b.list_models()
                names = ", ".join(m.name for m in models[:6]) + ("…" if len(models) > 6 else "")
                out.append(Check(f"Backend {b.name}", OK, f"{b.type} {st.version or ''} at {b.url}: {len(models)} model(s) {names}",
                                 "No models installed: e.g. 'ollama pull llama3.2'" if not models and b.type == "ollama" else ""))
            except Exception as exc:
                out.append(Check(f"Backend {b.name}", WARN, f"reachable but listing models failed: {exc}"))
        else:
            hint = "Start Ollama ('ollama serve' or its system service) or fix inference.backends url." if b.type == "ollama" \
                else "Start the server or disable this backend in homemesh.toml."
            out.append(Check(f"Backend {b.name}", FAIL if cfg.has_role("worker") else WARN, st.error or "unreachable", hint))
        await b.close()
    if not backends:
        out.append(Check("Backends", WARN, "no inference backend is enabled", "Configure [[inference.backends]]."))
    return out


def _port(cfg: HomeMeshConfig, running: bool) -> Check:
    if running:
        return Check("Port", OK, f"{cfg.server.host}:{cfg.server.port} in use by HomeMesh")
    host = cfg.server.host.strip("[]") or "0.0.0.0"
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform == "win32":
            # On Windows SO_REUSEADDR lets a bind succeed even while another program listens on the
            # port, so the check would always say "free". Exclusive use makes it a real test.
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # ignore old TIME_WAIT connections
        try:
            s.bind((host, cfg.server.port))
        except OSError as exc:
            return Check("Port", FAIL, f"{host}:{cfg.server.port} is in use by another program ({exc.strerror})",
                         "Stop the other program or change server.port in homemesh.toml.")
    return Check("Port", OK, f"{host}:{cfg.server.port} is free")


def _coordinator(cfg: HomeMeshConfig, paths: AppPaths) -> tuple[Check, bool]:
    url = cfg.local_base_url()
    verify: Any = True
    if url.startswith("https://") and (paths.tls_dir / "ca.pem").exists():
        verify = ssl.create_default_context(cafile=str(paths.tls_dir / "ca.pem"))
    try:
        r = httpx.get(url + "/api/v1/system/health", timeout=5, verify=verify, trust_env=False)
        data = r.json()
        return Check("Coordinator", OK if data.get("status") == "ok" else WARN,
                     f"running at {url} (version {data.get('version')}, database {data.get('database')})"
                     + (" - owner account not created yet" if data.get("setup_required") else "")), True
    except Exception as exc:
        return Check("Coordinator", WARN, f"not running at {url} ({type(exc).__name__})",
                     "Start it with 'homemesh start'."), False


def _worker_link(cfg: HomeMeshConfig, paths: AppPaths) -> Check:
    from homemesh.worker.credentials import WorkerCredentials

    try:
        creds = WorkerCredentials.load(paths.worker_credentials_file)
    except ConfigError as exc:
        return Check("Coordinator link", FAIL, exc.message, exc.hint or "")
    verify: Any = True
    if creds.coordinator_url.startswith("https://"):
        verify = ssl.create_default_context(cafile=creds.ca_file) if creds.ca_file else ssl.create_default_context()
    try:
        r = httpx.get(creds.coordinator_url + "/api/v1/system/health", timeout=8, verify=verify, trust_env=False)
        health = r.json()
        theirs = health.get("protocol_version")
        if theirs not in SUPPORTED_PROTOCOL_VERSIONS:
            return Check("Coordinator link", FAIL,
                         f"coordinator {health.get('version')} speaks protocol v{theirs}; this worker supports "
                         f"{list(SUPPORTED_PROTOCOL_VERSIONS)}",
                         "Upgrade HomeMesh so both computers run compatible versions (coordinator first).")
        return Check("Coordinator link", OK, f"{creds.coordinator_url} reachable (version {health.get('version')}), "
                                             f"node {creds.node_id}")
    except ssl.SSLError as exc:
        return Check("Coordinator link", FAIL, f"TLS verification failed: {exc}",
                     "The coordinator's certificate changed. If you reinstalled it, re-join with the new fingerprint.")
    except Exception as exc:
        return Check("Coordinator link", FAIL, f"cannot reach {creds.coordinator_url}: {type(exc).__name__}",
                     "Check that the coordinator is running and reachable (same network/VPN, firewall allows the port).")


def _tls(cfg: HomeMeshConfig, paths: AppPaths) -> Check | None:
    if cfg.server.tls_mode != "self_signed":
        return None
    from cryptography import x509

    p = paths.tls_dir / "server.pem"
    if not p.exists():
        return Check("TLS", WARN, "certificate not created yet", "It is created on start, or run 'homemesh tls init'.")
    cert = x509.load_pem_x509_certificate(p.read_bytes())
    from datetime import UTC, datetime

    days = (cert.not_valid_after_utc - datetime.now(UTC)).days
    from homemesh.coordinator.tls import ca_fingerprint

    return Check("TLS", OK if days > 30 else WARN, f"server certificate valid for {days} more days; CA fingerprint "
                                                   f"{ca_fingerprint(paths.tls_dir / 'ca.pem')}")


def _service() -> Check:
    from homemesh.cli import service

    if service.service_installed():
        return Check("Autostart", OK, "service installed")
    return Check("Autostart", INFO, "no service installed", "Optional: 'homemesh service install' starts HomeMesh at login.")


def _web(cfg: HomeMeshConfig) -> Check | None:
    if not cfg.has_role("coordinator"):
        return None
    from homemesh.coordinator.app import find_web_dist

    d = find_web_dist(cfg)
    if d is None:
        return Check("Web interface", FAIL, "built web UI not found",
                     "Run scripts/build_web.sh (Linux) or scripts\\build_web.ps1 (Windows), or reinstall a release build.")
    return Check("Web interface", OK, str(d))


def _internet_tools(cfg: HomeMeshConfig) -> list[Check]:
    """Only when this computer allows an internet tool. Never searches: an empty JSON query is
    answered by SearXNG itself (400 = JSON on, 403 = JSON off)."""
    allowed = [t for t in ("web_search", "fetch_url", "open_url") if t in cfg.worker.allowed_tools]
    if not allowed:
        return []
    checks = [Check("Internet tools", INFO, "allowed here: " + ", ".join(allowed),
                    "Each use is visible in the chat; open_url always asks first (docs/WEB_ACCESS.md).")]
    if "web_search" not in allowed:
        return checks
    url = cfg.web.searxng_url
    if not url:
        checks.append(Check("SearXNG", FAIL, "web_search is allowed but web.searxng_url is empty",
                            "homemesh web set-searxng http://127.0.0.1:8888 (see docs/WEB_ACCESS.md)"))
        return checks
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            health = client.get(url + "/healthz")
            probe = client.get(url + "/search", params={"format": "json"})
    except httpx.HTTPError as exc:
        checks.append(Check("SearXNG", FAIL, f"{url} not reachable ({type(exc).__name__})",
                            "Bazzite: systemctl --user status homemesh-searxng; or re-run "
                            "deploy/bazzite/install-searxng.sh"))
        return checks
    if probe.status_code == 403:
        checks.append(Check("SearXNG", FAIL, f"{url} answers, but JSON output is off",
                            "Add json to search.formats in its settings.yml and restart it."))
    elif health.status_code == 200 and probe.status_code == 400:
        host = urlsplit(url).hostname or ""
        if is_loopback_host(host):
            checks.append(Check("SearXNG", OK, f"{url} (JSON on)", "Try a real search: homemesh web test"))
        else:
            checks.append(Check("SearXNG", WARN, f"{url} (JSON on) is on another computer",
                                "Search queries leave this computer" + (" unencrypted" if url.startswith("http://")
                                                                        else "") + "; prefer SearXNG on 127.0.0.1."))
    else:
        checks.append(Check("SearXNG", WARN, f"{url} answered HTTP {health.status_code}/{probe.status_code}",
                            "Is this really SearXNG? homemesh web test shows details."))
    return checks


def run_doctor(paths: AppPaths) -> list[Check]:
    checks = [_python(), _deps(), _os()]
    cfg_check, cfg = _config(paths)
    checks += [cfg_check, _data_dir(paths)]
    if cfg is None:
        return checks
    running = False
    if cfg.has_role("coordinator"):
        c, running = _coordinator(cfg, paths)
        checks += [c, _database(paths), _port(cfg, running)]
        if (t := _tls(cfg, paths)) is not None:
            checks.append(t)
        if (w := _web(cfg)) is not None:
            checks.append(w)
    if cfg.has_role("worker"):
        if not cfg.has_role("coordinator"):
            checks.append(_worker_link(cfg, paths))
        checks += _gpus(cfg)
        checks += asyncio.run(_backends(cfg))
        checks += _internet_tools(cfg)
    checks.append(_service())
    return checks
