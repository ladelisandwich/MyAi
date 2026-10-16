"""A stand-in for ``homemesh run`` in the updater tests.

It does what the update runner relies on, with HomeMesh's own code: holds the instance lock
(``common.instance.InstanceLock``), writes the PID record (``write_pid_record``: pid, start time, ready) and
``agent.json`` (``update.files.write_agent_file``), and stops on the stop-request file or SIGTERM.

How it behaves comes from ``<venv>/fake-behavior.json`` (the fake ``pip install`` copies a commit's
``behavior.json`` there), so "push a commit that crashes" works like the real thing:

* ``exit_at_start``: exit with this code right away (a broken new version)
* ``never_ready``: never set ``ready`` in the PID record
* ``agent``: ``connected`` (default) | ``rejected`` | ``unreachable`` | ``none``; ``detail``: agent detail text
* ``migrate_to``: add schema_migrations rows up to this version (WAL, like the coordinator);
  ``crash_after_migrate``: then SIGKILL itself (leaves the migration in the -wal file)
* ``ignore_stop``: ignore SIGTERM and the stop request (needs a forced stop)
* ``exit_after``: exit with code 1 after this many seconds (a crash after start)
* ``ready_delay``: seconds before ``ready``
* ``running_jobs``: what it lists in ``drain.json`` while the run drains (default: nothing, i.e. drained)

Usage: ``python fake_homemesh.py run --home <HOMEMESH_HOME> --venv <venv it "runs from">``
"""

import argparse
import contextlib
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd")
    p.add_argument("--home", required=True)
    p.add_argument("--venv", required=True)
    a = p.parse_args()

    from homemesh.common.instance import InstanceLock, write_pid_record
    from homemesh.common.paths import AppPaths
    from homemesh.update import files

    home = Path(a.home)
    paths = AppPaths(config_dir=home, data_dir=home / "data")
    venv = Path(a.venv)
    try:
        behavior = json.loads((venv / "fake-behavior.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        behavior = {}
    try:
        commit = json.loads((venv / "homemesh-install.json").read_text(encoding="utf-8")).get("commit")
    except (OSError, ValueError):
        commit = None
    registry = home / "fake-pids"
    registry.mkdir(exist_ok=True)
    (registry / str(os.getpid())).write_text(str(venv), encoding="utf-8")

    def log(msg: str) -> None:
        with open(home / "fake-homemesh.log", "a", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()} {venv.name}: {msg}\n")

    if "exit_at_start" in behavior:
        log(f"exit_at_start {behavior['exit_at_start']}")
        return int(behavior["exit_at_start"])
    lock = InstanceLock(paths.lock_file)
    if not lock.acquire():
        log("instance lock held")
        return 2
    stop = []
    if behavior.get("ignore_stop"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.append("SIGTERM"))
    with contextlib.suppress(OSError):
        paths.stop_request_file.unlink()  # a stale request from an earlier run (like runtime.py)
    write_pid_record(paths.pid_file)
    log("started")
    if behavior.get("migrate_to"):
        conn = sqlite3.connect(paths.db_file)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                     " checksum TEXT NOT NULL, applied_at TEXT NOT NULL)")
        current = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        for v in range(current + 1, int(behavior["migrate_to"]) + 1):
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?)", (v, f"m{v}", "x", "now"))
            conn.execute(f"CREATE TABLE added_by_v{v} (x INTEGER)")
        conn.commit()
        log(f"migrated to {behavior['migrate_to']}")
        if behavior.get("crash_after_migrate"):
            os.kill(os.getpid(), signal.SIGKILL)  # the -wal file keeps the migration
        conn.close()
    time.sleep(float(behavior.get("ready_delay", 0)))
    if not behavior.get("never_ready"):
        write_pid_record(paths.pid_file, ready=True)
    agent = behavior.get("agent", "connected")
    if agent != "none":
        files.write_agent_file(paths, state=agent, build_commit=commit, detail=behavior.get("detail", ""))
    started = time.monotonic()
    code = 0
    while True:
        latest = files.read_latest_run(paths)  # the agent's drain duty (design 7.4)
        if latest is not None and latest[1].get("phase") == "draining" and latest[1].get("status") is None:
            with contextlib.suppress(OSError):
                files.write_drain_file(paths, latest[0], behavior.get("running_jobs", []))
        if stop:
            log("SIGTERM")
            break
        if not behavior.get("ignore_stop") and paths.stop_request_file.exists():
            with contextlib.suppress(OSError):
                paths.stop_request_file.unlink()
            log("stop request")
            break
        if "exit_after" in behavior and time.monotonic() - started > float(behavior["exit_after"]):
            log("exit_after")
            code = 1
            break
        time.sleep(0.05)
    with contextlib.suppress(OSError):
        paths.pid_file.unlink()
    lock.release()
    with contextlib.suppress(OSError):
        (registry / str(os.getpid())).unlink()
    return code


if __name__ == "__main__":
    sys.exit(main())
