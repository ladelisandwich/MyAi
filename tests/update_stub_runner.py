"""Stand-in for ``homemesh/update/runner.py`` in the update integration tests.

``update.launch`` runs it instead of the real runner when ``HOMEMESH_UPDATE_RUNNER`` names it and
``HOMEMESH_TESTING=1`` (exactly as it would run the runner: base interpreter, ``-I -S -X utf8``, its own
session, ``<mode> --args <args.json>``). ``HOMEMESH_TEST_STUB_DIR`` is a folder the test controls:

* ``plan.json``: ``{"runner": <path of the real runner.py>, "apply": "ok" | "exit_before_accept",
  "verify": "agent" | "gate", "hold": [<phase>, ...]}``;
* ``release-<phase>``: created by the test to let a held phase go on (each wait gives up after a while, so a
  failed test never leaves a stub behind for long).

``check`` runs the real runner (real git against the test's repository). ``apply`` keeps the runner's file
contracts with the runner's own primitives (update lock for its whole life, ``run.json`` before
``latest.json``, a phase written before each step, ``drain.json`` from the agent, ``agent.json`` as proof that
the new version connected) but builds, stops and starts nothing: the test plays HomeMesh being restarted.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import time
from datetime import UTC, datetime, timedelta

CONTROL = os.environ["HOMEMESH_TEST_STUB_DIR"]
WAIT_S = 90.0

with open(os.path.join(CONTROL, "plan.json"), encoding="utf-8") as _fh:
    PLAN = json.load(_fh)

if len(sys.argv) > 1 and sys.argv[1] == "check":
    sys.argv = [PLAN["runner"], *sys.argv[1:]]
    runpy.run_path(PLAN["runner"], run_name="__main__")  # ends with SystemExit(<the runner's exit code>)
    sys.exit(1)

R = runpy.run_path(PLAN["runner"])  # the real runner's helpers: lock, atomic writes, error texts, timestamps
args = R["read_json"](sys.argv[sys.argv.index("--args") + 1])
assert args is not None and args["mode"] == "apply", "stub: bad args"
update_dir, run_id = args["update_dir"], args["run_id"]
run_dir = os.path.join(update_dir, "runs", run_id)
run_file = os.path.join(run_dir, "run.json")


def log(text: str) -> None:
    with open(args["log"], "ab") as fh:
        fh.write(f"{R['now_iso']()} stub: {text}\n".encode())


def released(name: str) -> bool:
    return os.path.exists(os.path.join(CONTROL, f"release-{name}"))


def wait(what: str, ready: object) -> bool:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if ready():  # type: ignore[operator]
            return True
        time.sleep(0.05)
    log(f"gave up waiting for {what}")
    return False


lock = R["FileLock"](os.path.join(update_dir, "update.lock"))
if not lock.acquire():
    log(f"{R['EXIT_MARKER']} {R['EXIT_BUSY']}")
    sys.exit(R["EXIT_BUSY"])

if PLAN.get("apply") == "exit_before_accept":  # e.g. killed with the computer before it wrote anything
    wait("release-accept", lambda: released("accept"))
    log("exiting without a run")
    sys.exit(R["EXIT_FAILED"])

record = R["read_json"](os.path.join(args["current_venv"], "homemesh-install.json")) or {}
now = R["now_iso"]()
run = {"v": 1, "run_id": run_id, "job_id": args["job_id"], "origin": args["origin"],
       "expected_commit": args["expected_commit"], "force": bool(args["force"]), "phase": "accepted",
       "status": None, "committed": False, "switched": False, "from_commit": record.get("commit"),
       "to_commit": None, "from_venv": args["current_venv"], "to_venv": None, "build_dir": None,
       "was_running": None, "drain_deadline": None, "db_snapshot": None, "pre_schema": None,
       "runner_pid": os.getpid(), "started_at": now, "updated_at": now, "finished_at": None, "error": None,
       "warnings": [], "log": args["log"], "installed_commit": None}


def save(**changes: object) -> None:
    run.update(changes, updated_at=R["now_iso"]())
    R["atomic_write_json"](run_file, run)


def phase(name: str, **changes: object) -> None:
    log(f"phase {name}")
    save(phase=name, **changes)


def hold(name: str) -> None:
    if name in PLAN.get("hold", []):
        wait(f"release-{name}", lambda: released(name))


def finish(status: str, *, error: dict | None = None, installed: str | None = None) -> None:
    save(status=status, error=error, installed_commit=installed, finished_at=R["now_iso"]())
    log(f"final status {status}")


try:
    os.makedirs(run_dir, exist_ok=True)
    R["atomic_write_json"](run_file, run)
    R["atomic_write_json"](os.path.join(update_dir, "latest.json"), {"v": 1, "run_id": run_id})
    hold("accepted")
    target = args["expected_commit"]
    phase("prechecks")
    hold("prechecks")
    phase("building", to_commit=target)
    hold("building")

    started = datetime.now(UTC)
    phase("draining", was_running=True, drain_started_at=R["now_iso"](started),
          drain_deadline=R["now_iso"](started + timedelta(seconds=WAIT_S)))
    drain_file = os.path.join(run_dir, "drain.json")

    def drained() -> bool:
        data = R["read_json"](drain_file)
        at = R["parse_iso"](data.get("at")) if data else None
        return bool(data and at is not None and at >= started - timedelta(seconds=1) and data.get("running") == [])

    if not wait("an empty drain.json", drained):
        finish("failed", error=R["issue"]("update_jobs_running", n="Some", types="unknown"),
               installed=run["from_commit"])
        sys.exit(R["EXIT_FAILED"])
    log(f"drained: {R['read_json'](drain_file)}")
    hold("draining")
    phase("stopping", committed=True)
    hold("stopping")
    phase("switching", switched=True)
    phase("starting")
    verify_started = datetime.now(UTC)
    phase("verifying")
    hold("verifying")

    def connected() -> bool:
        agent = R["read_json"](os.path.join(update_dir, "agent.json")) or {}
        at = R["parse_iso"](agent.get("at"))
        return bool(agent.get("state") == "connected" and agent.get("build_commit") == target
                    and agent.get("pid") and at is not None and at >= verify_started)

    if PLAN.get("verify", "agent") == "agent" and not wait("agent.json connected with the new build", connected):
        finish("rolled_back", error=R["issue"]("update_not_confirmed", n=int(WAIT_S)),
               installed=run["from_commit"])
        sys.exit(R["EXIT_OK"])
    log(f"agent: {R['read_json'](os.path.join(update_dir, 'agent.json'))}")
    phase("cleanup")
    finish("ok", installed=target)
finally:
    if run.get("status") is None and os.path.exists(run_file):
        finish("interrupted", error=R["issue"]("update_interrupted", phase=run["phase"]),
               installed=run["from_commit"])
    lock.release()
sys.exit(R["EXIT_OK"])
