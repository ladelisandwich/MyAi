"""``python -m homemesh`` of the stand-in HomeMesh used by ``tests/integration/test_update_e2e.py``.

Committed into the test repository as ``src/homemesh/__main__.py`` and installed into real venvs by the real
update runner, so it may use the standard library only (the runner strips ``PYTHONPATH``). It honours what the
runner relies on, in the formats HomeMesh uses (design section 12): the instance lock (``flock``), the PID
record (``pid``, ``started`` = the process start time, ``ready``), the stop-request file, SIGTERM,
``agent.json`` (``connected`` with the commit of the install record of the venv it runs from) and, while a run
drains, ``drain.json``. File locations come from ``standin.json`` (written by the test from ``AppPaths`` and
``homemesh.update.files``), behaviour from ``behavior.json`` (part of each commit):

* ``exit_at_start``: exit with this code at once (a new version that crashes)
* ``config_fail``: ``config validate`` fails
"""

import contextlib
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PKG = Path(__file__).resolve().parent
CFG = json.loads((PKG / "standin.json").read_text(encoding="utf-8"))
BEHAVIOR = json.loads((PKG / "behavior.json").read_text(encoding="utf-8"))


def iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_json(path: str, obj: object) -> None:
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def start_time() -> float:
    """Seconds since the epoch at which this process started (what psutil's create_time() reports)."""
    with open("/proc/stat", encoding="ascii") as fh:
        btime = next(float(line.split()[1]) for line in fh if line.startswith("btime "))
    with open(f"/proc/{os.getpid()}/stat", "rb") as fh:
        raw = fh.read().decode()
    ticks = int(raw[raw.rindex(")") + 2:].split()[19])
    return btime + ticks / os.sysconf("SC_CLK_TCK")


def installed_commit() -> str | None:
    return read_json(os.path.join(sys.prefix, "homemesh-install.json")).get("commit")


def run() -> int:
    if "exit_at_start" in BEHAVIOR:
        return int(BEHAVIOR["exit_at_start"])
    import fcntl

    lock = open(CFG["instance_lock"], "a+b")  # held for the whole life of the process
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 2
    stop: list[str] = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append("SIGTERM"))
    with contextlib.suppress(OSError):
        os.unlink(CFG["stop_request_file"])  # stale, from an earlier run
    pid, started = os.getpid(), start_time()
    registry = os.path.join(CFG["registry"], str(pid))
    with open(registry, "w", encoding="utf-8") as fh:
        fh.write(os.path.realpath(sys.prefix))
    write_json(CFG["pid_file"], {"pid": pid, "started": started, "ready": False})
    write_json(CFG["pid_file"], {"pid": pid, "started": started, "ready": True})
    write_json(CFG["agent_file"], {"v": 1, "pid": pid, "process_started": started,
                                   "build_commit": installed_commit(), "state": "connected",
                                   "detail": "stand-in", "at": iso()})
    try:
        while not stop:
            if os.path.exists(CFG["stop_request_file"]):
                with contextlib.suppress(OSError):
                    os.unlink(CFG["stop_request_file"])
                break
            run_id = read_json(CFG["latest_file"]).get("run_id")
            if isinstance(run_id, str) and run_id.startswith("u") and "/" not in run_id and "\\" not in run_id:
                run_dir = os.path.join(CFG["runs_dir"], run_id)
                current = read_json(os.path.join(run_dir, "run.json"))
                if current.get("phase") == "draining" and current.get("status") is None:
                    write_json(os.path.join(run_dir, "drain.json"),
                               {"v": 1, "pid": pid, "running": [], "at": iso()})
            time.sleep(0.1)
    finally:
        for path in (CFG["pid_file"], registry):
            with contextlib.suppress(OSError):
                os.unlink(path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["version"]:
        print(f"HomeMesh stand-in (commit {installed_commit()})")
        return 0
    if argv[:2] == ["config", "validate"]:
        if BEHAVIOR.get("config_fail"):
            print("node.roles: unknown role 'stand-in'")
            return 1
        print("Configuration is valid.")
        return 0
    if argv[:2] == ["service", "install"]:
        return 0
    if argv[:1] == ["run"]:
        return run()
    print(f"stand-in: unknown command {argv}", file=sys.stderr)
    return 2


sys.exit(main(sys.argv[1:]))
