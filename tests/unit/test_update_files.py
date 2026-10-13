"""<data>/update/ files: atomic owner-only writes, tolerant reads, the latest pointer, the update lock,
finalizing a dead run, pruning and the args.json contract."""

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fsutil import symlink_or_skip
from homemesh.common.instance import InstanceLock
from homemesh.protocol import update as pu
from homemesh.update import consent, files

SHA = "c" * 40


def _rid(i: int) -> str:
    return f"u202610131200{i:02d}-0000000{i % 10}"


def _run(paths, run_id, **kw):
    run = {"v": 1, "run_id": run_id, "job_id": "job_x", "origin": "remote", "expected_commit": SHA, "force": False,
           "phase": "building", "status": None, "committed": False, "switched": False, "runner_pid": 1,
           "started_at": "2026-10-13T12:00:00.000Z", "updated_at": "2026-10-13T12:00:01.000Z",
           "finished_at": None, "error": None, "warnings": [], "log": str(files.apply_log_file(paths, run_id)),
           **kw}
    files.run_dir(paths, run_id).mkdir(parents=True, exist_ok=True)
    files.atomic_write_json(files.run_file(paths, run_id), run)
    return run


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------- writes and reads

def test_atomic_write_round_trip_owner_only_and_no_leftovers(tmp_path):
    target = tmp_path / "x.json"
    files.atomic_write_json(target, {"subject": "Łódź 修复 →", "n": 1})
    files.atomic_write_json(target, {"subject": "second", "n": 2})
    assert files.read_json(target) == {"subject": "second", "n": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]
    if os.name == "posix":
        assert _mode(target) == 0o600
    with pytest.raises(ValueError):
        files.atomic_write_json(target, {"x": float("nan")})
    assert files.read_json(target)["n"] == 2 and len(list(tmp_path.iterdir())) == 1


def test_atomic_write_refuses_a_symlinked_destination(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    symlink_or_skip(real, tmp_path / "link.json")
    with pytest.raises(OSError):
        files.atomic_write_bytes(tmp_path / "link.json", b"{}")
    assert real.read_text(encoding="utf-8") == "{}"


def test_replace_is_retried_while_windows_readers_block_it(tmp_path, monkeypatch):
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) < 3:
            raise PermissionError(13, "in use")
        return real_replace(src, dst)

    monkeypatch.setattr(files.os, "replace", flaky)
    files.atomic_write_json(tmp_path / "r.json", {"ok": True})
    assert len(calls) == 3 and files.read_json(tmp_path / "r.json") == {"ok": True}

    def always(src, dst):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(files.os, "replace", always)
    monkeypatch.setattr(files, "REPLACE_RETRY_S", 0.1)
    with pytest.raises(PermissionError):
        files.atomic_write_json(tmp_path / "r.json", {"ok": False})
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]  # temp file removed


def test_tolerant_reads(tmp_path):
    p = tmp_path / "f.json"
    assert files.read_json(p) is None
    for raw in (b"", b"{partial", b"[1, 2]", b'"str"', b"\xff\xfe{}", b"[" * 100000):
        p.write_bytes(raw)
        assert files.read_json(p) is None, raw[:10]
    p.write_bytes(b'{"a": "' + b"x" * 100 + b'"}')
    assert files.read_json(p, max_bytes=50) is None
    assert files.read_json(p)["a"] == "x" * 100


def test_open_private_append(tmp_path):
    log = tmp_path / "update-x.log"
    with files.open_private_append(log) as fh:
        fh.write(b"one\n")
    with files.open_private_append(log) as fh:
        fh.write(b"two\n")
    assert log.read_bytes() == b"one\ntwo\n"
    if os.name == "posix":
        assert _mode(log) == 0o600


# --------------------------------------------------------------------------- layout

def test_layout_and_run_ids(home):
    rid = files.new_run_id(datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC))
    assert pu.is_run_id(rid) and rid.startswith("u20261013120000-")
    assert files.new_run_id() != files.new_run_id()
    base = files.ensure_update_dir(home)
    assert base == home.data_dir / "update" and (base / "runs").is_dir() and (base / "checks").is_dir()
    if os.name == "posix":
        assert _mode(base) == 0o700
    assert files.run_file(home, rid) == base / "runs" / rid / "run.json"
    assert files.drain_file(home, rid).name == "drain.json" and files.args_file(home, rid).name == "args.json"
    assert files.check_args_file(home, rid) == base / "checks" / f"{rid}.args.json"
    assert files.check_result_file(home, rid) == base / "checks" / f"{rid}.json"
    assert files.apply_log_file(home, rid) == home.logs_dir / f"update-{rid}.log"
    assert files.check_log_file(home, rid) == home.logs_dir / f"update-check-{rid}.log"
    for bad in ("../../etc", "u1", "", rid + "/x"):
        with pytest.raises(ValueError):
            files.run_dir(home, bad)


def test_latest_pointer_and_report(home):
    files.ensure_update_dir(home)
    assert files.read_latest_run_id(home) is None and files.read_latest_report(home) is None
    rid = _rid(1)
    _run(home, rid, extra_runner_field={"kept": True})
    assert files.read_latest_run_id(home) is None  # the pointer, never the newest directory
    files.write_latest(home, rid)
    assert files.read_json(files.latest_file(home)) == {"v": 1, "run_id": rid}
    run_id, run = files.read_latest_run(home)
    assert run_id == rid and run["extra_runner_field"] == {"kept": True}
    report = files.read_latest_report(home)
    assert report["run_id"] == rid and report["phase"] == "building" and report["status"] is None
    assert "extra_runner_field" not in report and "runner_pid" not in report
    assert pu.json_size(report) <= pu.MAX_REPORT_BYTES
    files.atomic_write_json(files.latest_file(home), {"v": 1, "run_id": "../x"})
    assert files.read_latest_run_id(home) is None
    files.write_latest(home, rid)
    files.atomic_write_json(files.run_file(home, rid), {"v": 1, "run_id": rid, "phase": "warp"})
    assert files.read_latest_run(home) is not None and files.read_latest_report(home) is None


# --------------------------------------------------------------------------- lock, finalizer, in-progress

def test_update_lock_probe_and_dead_run_finalizer(home):
    assert files.update_lock_held(home) is False  # no update dir at all: nothing created
    assert not files.update_dir(home).exists()
    files.ensure_update_dir(home)
    rid = _rid(2)
    _run(home, rid, phase="switching")
    files.write_latest(home, rid)
    runner_lock = InstanceLock(files.lock_file(home))  # what the runner holds for its whole life
    assert runner_lock.acquire()
    try:
        assert files.update_lock_held(home) is True
        assert files.update_in_progress(home) == {"run_id": rid, "phase": "switching",
                                                  "log": str(files.apply_log_file(home, rid))}
        assert files.finalize_dead_run(home, installed_commit=SHA, computer="pc") is None  # runner alive
        assert files.read_run(home, rid)["status"] is None
    finally:
        runner_lock.release()
    assert files.update_lock_held(home) is False and files.update_in_progress(home) is None
    run = files.finalize_dead_run(home, installed_commit=SHA, computer="Gaming-PC")
    assert run["status"] == "interrupted" and run["installed_commit"] == SHA and run["finished_at"]
    assert run["error"]["code"] == "update_interrupted" and "while switching versions" in run["error"]["message"]
    assert run["error"]["hint"] == "Gaming-PC now runs ccccccc. Check for updates again."
    assert files.read_run(home, rid) == run and run["phase"] == "switching"
    assert files.finalize_dead_run(home, installed_commit=SHA) is None  # already final
    assert files.read_latest_report(home)["status"] == "interrupted"
    lock = files.update_lock(home)
    assert lock.acquire()  # the finalizer released the lock
    lock.release()


def test_finalizer_with_unknown_commit(home):
    files.ensure_update_dir(home)
    rid = _rid(3)
    _run(home, rid)
    files.write_latest(home, rid)
    run = files.finalize_dead_run(home, installed_commit=None)
    assert run["installed_commit"] is None and "this computer now runs an unknown version" in run["error"]["hint"]


# --------------------------------------------------------------------------- agent.json / drain.json

def test_agent_and_drain_files(home):
    files.write_agent_file(home, state="connected", build_commit=SHA, detail="x" * 1000)
    agent = files.read_agent_file(home)
    assert agent["v"] == 1 and agent["pid"] == os.getpid() and agent["state"] == "connected"
    assert agent["build_commit"] == SHA and len(agent["detail"]) == pu.AGENT_DETAIL_MAX
    assert isinstance(agent["process_started"], float)
    rid = _rid(4)
    files.run_dir(home, rid).mkdir(parents=True)
    files.write_drain_file(home, rid, [{"job_id": "job_1", "type": "chat"}])
    drain = files.read_drain_file(home, rid)
    assert drain["running"] == [{"job_id": "job_1", "type": "chat"}] and drain["pid"] == os.getpid()


# --------------------------------------------------------------------------- pruning

def test_prune_runs(home):
    files.ensure_update_dir(home)
    ids = [_rid(i) for i in range(10, 18)]  # 8 finished runs, oldest first
    snapshots = ("pre-update.sqlite3", "homemesh.toml.bak", "worker-credentials.json.bak")
    for rid in ids:
        _run(home, rid, status="ok")
        for name in (*snapshots, "cmd-1.out", "drain.json", ".run.json.abc123.tmp"):
            (files.run_dir(home, rid) / name).write_bytes(b"x")
        files.apply_log_file(home, rid).write_text("log", encoding="utf-8")
    files.write_latest(home, ids[0])  # the run latest.json names is protected
    _run(home, ids[1], status=None)  # so is a run without a final status
    (files.run_dir(home, ids[2]) / "mystery.bin").write_bytes(b"?")  # an unknown file keeps its directory

    def has(rid, name):
        return (files.run_dir(home, rid) / name).exists()

    assert files.prune_runs(home, keep=5) == []  # the 3 candidates: protected, protected, unknown file
    assert sorted(p.name for p in files.runs_dir(home).iterdir()) == ids
    assert [p.name for p in files.run_dir(home, ids[2]).iterdir()] == ["mystery.bin"]  # known files went
    assert all(has(ids[0], n) and has(ids[1], n) for n in snapshots)  # protected runs keep their snapshots
    assert all(has(ids[-1], n) for n in snapshots)  # the newest run keeps its snapshot and backups
    assert not any(has(rid, n) for rid in ids[3:-1] for n in snapshots)  # older ones lose them
    assert all(has(rid, "run.json") and has(rid, "cmd-1.out") for rid in ids[3:])

    files.write_latest(home, ids[-1])
    _run(home, ids[1], status="failed")
    (files.run_dir(home, ids[2]) / "mystery.bin").unlink()
    assert files.prune_runs(home, keep=5) == ids[:3]
    assert sorted(p.name for p in files.runs_dir(home).iterdir()) == ids[3:]
    assert not files.apply_log_file(home, ids[0]).exists() and files.apply_log_file(home, ids[3]).exists()


def test_prune_runs_skips_links(home, tmp_path):
    files.ensure_update_dir(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.json").write_text("{}", encoding="utf-8")
    symlink_or_skip(outside, files.runs_dir(home) / _rid(20))
    for i in range(21, 23):
        _run(home, _rid(i), status="ok")
    assert files.prune_runs(home, keep=1) == [_rid(21)]
    assert (outside / "run.json").exists()


def test_prune_checks(home):
    files.ensure_update_dir(home)
    ids = [_rid(i) for i in range(30, 37)]
    for rid in ids:
        files.atomic_write_json(files.check_result_file(home, rid), {"v": 1, "ok": False})
        files.atomic_write_json(files.check_args_file(home, rid), {"v": 1})
        files.check_log_file(home, rid).write_text("log", encoding="utf-8")
    (files.checks_dir(home) / "notes.txt").write_text("keep me", encoding="utf-8")
    assert files.prune_checks(home, keep=5) == ids[:2]
    left = sorted(p.name for p in files.checks_dir(home).iterdir())
    assert len(left) == 11 and "notes.txt" in left and f"{ids[2]}.json" in left
    assert not files.check_log_file(home, ids[0]).exists() and files.check_log_file(home, ids[2]).exists()


# --------------------------------------------------------------------------- args.json

def _args(home, tmp_path, **kw):
    base = {"mode": "apply", "run_id": _rid(40), "computer": "Gaming-PC", "roles": ["worker"],
            "source_dir": tmp_path / "src", "stable_venv": tmp_path / "prefix" / "venv",
            "git": tmp_path / "git", "node_dir": None, "service": "posix", "job_id": "job_1",
            "origin": "remote", "expected_commit": SHA, "homemesh_home": None}
    return files.build_runner_args(home, **{**base, **kw})


def test_runner_args_apply(home, tmp_path):
    args = _args(home, tmp_path, force=True)
    assert tuple(args) == files.RUNNER_ARGS_KEYS and args["v"] == 1
    assert args["prefix"] == str(tmp_path / "prefix") and args["stable_venv"] == str(tmp_path / "prefix" / "venv")
    assert args["db_file"] is None and args["out"] is None and args["force"] is True
    assert args["log"] == str(files.apply_log_file(home, args["run_id"]))
    assert args["consent_file"] == str(consent.marker_path(home))
    assert args["instance_lock"] == str(home.lock_file) and args["pid_file"] == str(home.pid_file)
    assert args["update_dir"] == str(files.update_dir(home))
    assert Path(args["current_package_dir"], "update", "runner.py").is_file()
    assert all(Path(args[k]).is_absolute() for k in args if isinstance(args[k], str) and ("dir" in k or k in (
        "git", "log", "prefix", "stable_venv", "current_venv", "config_file", "pid_file", "instance_lock")))
    path = files.write_runner_args(home, args)
    assert path == files.args_file(home, args["run_id"]) and files.read_json(path) == json.loads(json.dumps(args))
    if os.name == "posix":
        assert _mode(path) == 0o600


def test_runner_args_check_and_validation(home, tmp_path, monkeypatch):
    monkeypatch.setenv("HOMEMESH_HOME", str(home.config_dir))
    args = files.build_runner_args(
        home, mode="check", run_id=_rid(41), computer="Hub", roles=["coordinator", "worker"],
        source_dir=tmp_path / "src", stable_venv=tmp_path / "venv", git=tmp_path / "git",
        node_dir=tmp_path / "node", service="systemd", unit_path=tmp_path / "homemesh.service")
    assert args["out"] == str(files.check_result_file(home, _rid(41))) and args["expected_commit"] is None
    assert args["db_file"] == str(home.db_file) and args["homemesh_home"] == str(home.config_dir)
    assert args["log"] == str(files.check_log_file(home, _rid(41)))
    assert files.write_runner_args(home, args) == files.check_args_file(home, _rid(41))
    for bad in ({"expected_commit": None}, {"expected_commit": "origin/main"}, {"run_id": "../x"},
                {"mode": "rollback"}, {"roles": []}, {"roles": ["admin"]}, {"service": "launchd"},
                {"service": "systemd"}, {"source_dir": "relative/src"}, {"origin": "hub"},
                {"mode": "check"}, {"unit_path": tmp_path / "u.service"}):
        with pytest.raises(ValueError):
            _args(home, tmp_path, **bad)
