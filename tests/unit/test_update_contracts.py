"""The stdlib runner carries copies of what it shares with HomeMesh (design section 12). These tests keep
the copies in agreement: patterns, sanitizers, error texts, file layout rules, and every file that crosses
the old-runner / new-HomeMesh boundary (args.json, run.json, latest.json, agent.json, drain.json, the install
record, the PID record), including unknown fields being ignored."""

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from homemesh.common.instance import write_pid_record
from homemesh.common.timeutil import now_iso, parse_iso
from homemesh.protocol import update as pu
from homemesh.update import files, record, runner
from update_support import Layout, Repo

SHA = "a" * 40
SAMPLES = ["", "a" * 40, "A" * 40, "a" * 39, "a" * 64, "a" * 41, "a" * 40 + "\n", "u20261013120000-0123abcd",
           "u20261013120000-0123abcd\n", "u2026101312000\u0660-0123abcd", "abcdef012345-20261013120000",
           "nogit-20261013120000", "legacy-20261013120000", "legacy-2026101312000", "main", "feature/x-1.2",
           "-x", ".x", "/x", "x/", "x.", "x.lock", "a..b", "a//b", "a b", "origin", "@{u}", "origin/main",
           "--output=x", None, 5]


@pytest.mark.parametrize("value", SAMPLES)
def test_patterns_agree(value):
    assert runner.is_sha(value) == pu.is_sha(value)
    assert runner.is_run_id(value) == pu.is_run_id(value)
    assert runner.is_venv_id(value) == pu.is_venv_id(value)
    assert runner.is_ref_name(value) == pu.is_ref_name(value)
    assert runner.short_sha(value if isinstance(value, str) else None) == pu.short_sha(
        value if isinstance(value, str) else None)


@pytest.mark.parametrize("text", [
    "plain", "https://user:ghp_abcDEF123@github.com/o/r.git", "ssh://git@host/x", "gho_xyz and github_pat_11AB_cd",
    "\x1b[2K\rsubject\x07 \x9b x\ty\nz", "Łódź 修复 →", "x" * 600])
def test_sanitizers_agree(text):
    assert runner.redact(text) == pu.redact(text)
    for n in (5, 200, 500):
        assert runner.clean_line(text, n) == pu.clean_line(text, n)
        assert runner.clean_text(text, n) == pu.clean_text(text, n)


def test_caps_and_vocabularies_agree():
    for name in ("CODE_MAX", "MESSAGE_MAX", "HINT_MAX", "SUBJECT_MAX", "NAME_MAX", "PATH_MAX", "TIME_MAX", "ID_MAX",
                 "MAX_WARNINGS", "MAX_PROBLEMS", "MAX_COMMITS", "MAX_DIRTY_FILES"):
        assert getattr(runner, name) == getattr(pu, name), name
    assert runner.PHASES == pu.PHASES
    assert runner.PHASE_LABELS == pu.PHASE_LABELS
    assert runner.FINAL_STATUSES == pu.FINAL_STATUSES
    assert runner.TIMEOUTS["drain"] == pu.DRAIN_TIMEOUT_S


def test_error_texts_are_copied_verbatim():
    for code, texts in runner.ERRORS.items():
        assert pu.UPDATE_ERRORS[code] == texts, code
    assert runner.WARNINGS == pu.UPDATE_WARNINGS
    assert set(runner.ERRORS) <= pu.UPDATE_ERROR_CODES


@pytest.mark.parametrize("code", sorted({*runner.ERRORS, *runner.WARNINGS, "unknown_code"}))
def test_formatted_errors_agree(code):
    fields = {"computer": "box", "source_dir": "/src", "log": "/l/update.log", "phase": "building", "short": "abc1234",
              "detail": "it broke", "lines": "a\nb", "n": 3, "types": "chat", "path": "/p", "remote": "origin",
              "branch": "main", "files": "x.py", "upstream_ref": "origin/main", "free": "1 GiB", "need": "2 GiB",
              "volume": "/", "exit": "exit code 3", "commit": "abc1234", "name": "--x",
              "installer_command": "./install.sh --upgrade"}
    assert runner.issue(code, **fields) == pu.format_update_error(code, **fields).model_dump()
    assert runner.issue(code) == pu.format_update_error(code).model_dump()  # the same defaults


@pytest.mark.parametrize("os_name", ["linux", "windows"])
@pytest.mark.parametrize("source_dir", [None, "/home/me/My AI", "C:\\Users\\me\\MyAi"])
@pytest.mark.parametrize("pull", [True, False])
def test_installer_command_agrees(os_name, source_dir, pull):
    assert runner.installer_command(os_name, source_dir, pull=pull) == pu.installer_command(os_name, source_dir,
                                                                                             pull=pull)


def test_file_rules_agree():
    assert runner.RUN_DIR_FILES == files.RUN_DIR_FILES
    assert runner.SNAPSHOT_FILES == files.SNAPSHOT_FILES
    assert runner.KEEP_RUN_ERROR_CODES == files.KEEP_RUN_ERROR_CODES
    assert runner.CAPTURE_FILE_RE.pattern == files.CAPTURE_FILE_RE.pattern
    assert runner.TEMP_FILE_RE.pattern == files.TEMP_FILE_RE.pattern
    assert runner._CHECK_FILE_RE.pattern == files._CHECK_FILE_RE.pattern
    assert (runner.KEEP_RUNS, runner.KEEP_CHECKS, runner.MAX_FILE_BYTES) == (files.KEEP_RUNS, files.KEEP_CHECKS,
                                                                             files.MAX_FILE_BYTES)
    assert runner.RECORD_NAME == record.RECORD_NAME
    assert runner.ARGS_KEYS == files.RUNNER_ARGS_KEYS
    assert runner.SNAPSHOT_NAME in files.SNAPSHOT_FILES


def test_timestamps_agree():
    mine, theirs = runner.now_iso(), now_iso()
    pattern = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
    assert pattern.fullmatch(mine) and pattern.fullmatch(theirs)
    assert abs((runner.parse_iso(theirs) - parse_iso(mine)).total_seconds()) < 5
    assert runner.parse_iso("garbage") is None and runner.parse_iso(None) is None


def test_atomic_writes_and_reads_agree(tmp_path):
    obj = {"subject": "Łódź 修复 →", "n": 1, "nested": {"x": [1, 2]}}
    runner.atomic_write_json(tmp_path / "a.json", obj)
    files.atomic_write_json(tmp_path / "b.json", obj)
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    assert files.read_json(tmp_path / "a.json") == runner.read_json(tmp_path / "b.json") == obj
    (tmp_path / "bad.json").write_bytes(b"\xff{")
    assert runner.read_json(tmp_path / "bad.json") is None and runner.read_json(tmp_path / "none.json") is None
    (tmp_path / "list.json").write_text("[1]")
    assert runner.read_json(tmp_path / "list.json") is None
    with pytest.raises(ValueError):
        runner.atomic_write_json(tmp_path / "a.json", {"x": float("nan")})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.json", "b.json", "bad.json", "list.json"]


# --------------------------------------------------------------------------- pruning: same rules, same result

def _tree(base: Path) -> list[str]:
    return sorted(str(p.relative_to(base)) for p in base.rglob("*"))


def _populate(paths):
    files.ensure_update_dir(paths)
    for i in range(9):
        rid = f"u202610131200{i:02d}-0000000{i}"
        d = files.run_dir(paths, rid)
        d.mkdir(parents=True)
        status = None if i == 2 else "ok"
        err = {"code": "update_db_restore_failed"} if i == 1 else None
        files.atomic_write_json(d / "run.json", {"run_id": rid, "status": status, "error": err})
        for name in ("args.json", "pre-update.sqlite3", "homemesh.toml.bak", "cmd-1.out", "cmd-22.out"):
            (d / name).write_text("x")
        if i == 3:
            (d / "unknown.bin").write_text("keep me")
        files.apply_log_file(paths, rid).write_text("log")
        files.check_args_file(paths, rid).write_text("{}")
        files.check_result_file(paths, rid).write_text("{}")
        files.check_log_file(paths, rid).write_text("log")
    (files.checks_dir(paths) / ".u20261013120000-00000000.json.abc.tmp").write_text("in flight")
    files.write_latest(paths, "u20261013120004-00000004")


def test_pruning_agrees(tmp_path):
    from homemesh.common.paths import AppPaths

    a = AppPaths(config_dir=tmp_path / "a", data_dir=tmp_path / "a" / "data")
    b = AppPaths(config_dir=tmp_path / "b", data_dir=tmp_path / "b" / "data")
    _populate(a)
    _populate(b)
    removed_runner = runner.prune_runs(str(files.update_dir(a)), str(a.logs_dir))
    removed_files = files.prune_runs(b)
    assert removed_runner == removed_files and removed_runner
    assert runner.prune_checks(str(files.update_dir(a)), str(a.logs_dir)) == files.prune_checks(b)
    assert _tree(a.data_dir) == _tree(b.data_dir)
    kept = os.listdir(files.runs_dir(a))
    assert "u20261013120001-00000001" in kept and "u20261013120002-00000002" in kept  # restore failed / not final


# --------------------------------------------------------------------------- args.json

@pytest.fixture
def env(tmp_path):
    return Layout(tmp_path, Repo(tmp_path / "repo"))


def test_args_from_the_launcher_pass_the_runner(env):
    for mode in ("check", "apply"):
        a = env.args(mode, expected=SHA if mode == "apply" else None)
        assert set(runner.ARGS_KEYS) <= set(a)
        assert a["mode"] == mode and os.path.isabs(a["log"])


MUTATIONS = [
    ("v", 2), ("mode", "check"), ("run_id", "u1"), ("run_id", "../../x"), ("job_id", "x" * 101), ("job_id", "a\nb"),
    ("origin", "hub"), ("expected_commit", "HEAD"), ("expected_commit", "a" * 7), ("force", "yes"),
    ("computer", ""), ("git", "git"), ("source_dir", "relative/src"), ("stable_venv", "venv"),
    ("prefix", "/somewhere/else"), ("update_dir", "/tmp/elsewhere"), ("roles", []), ("roles", ["admin"]),
    ("roles", "worker"), ("service", "launchd"), ("unit_path", "/x/homemesh.service"), ("db_file", "/x/db"),
    ("log", "/tmp/any.log"), ("out", "/tmp/out.json"), ("node_dir", "rel"), ("homemesh_home", "rel/home"),
    ("instance_lock", None), ("consent_file", ""),
]


@pytest.mark.parametrize("key, value", MUTATIONS)
def test_runner_rejects_each_bad_field(env, tmp_path, key, value):
    a = env.args("apply", expected=SHA)
    bad = tmp_path / "bad-args.json"
    bad.write_text(json.dumps({**a, key: value}))
    with pytest.raises(runner.ArgsError) as exc:
        runner.load_args(bad, "apply")
    assert str(exc.value).split(":")[0] in (key, "mode", "prefix", "log", "unit_path", "db_file", "force",
                                            "update_dir")


def test_runner_check_args_rules(env, tmp_path):
    a = env.args("check")
    runner.load_args(files.check_args_file(env.paths, a["run_id"]), "check")
    for key, value in (("expected_commit", SHA), ("force", True), ("out", None)):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({**a, key: value}))
        with pytest.raises(runner.ArgsError):
            runner.load_args(bad, "check")
    with pytest.raises(runner.ArgsError):
        runner.load_args(files.check_args_file(env.paths, a["run_id"]), "apply")
    extra = tmp_path / "extra.json"
    extra.write_text(json.dumps({**a, "added_by_a_later_version": 1}))
    assert runner.load_args(extra, "check")["added_by_a_later_version"] == 1  # unknown keys ignored


# --------------------------------------------------------------------------- files crossing the version boundary

def _apply_run(env, **kw):
    args = env.args("apply", expected=SHA, **kw)
    return runner._Apply(args, runner.Hooks()), args


def test_run_json_and_latest_are_readable_by_homemesh(env):
    run, args = _apply_run(env)
    os.makedirs(run.run_dir, exist_ok=True)
    run.accept()
    data = files.read_run(env.paths, args["run_id"])
    later = {"drain_started_at", "first_error", "guard_started"}  # written by later phases, if at all
    assert set(files.RunFile.__annotations__) - later <= set(data)
    assert files.read_latest_run_id(env.paths) == args["run_id"]
    report = files.report_from_run(data)
    assert report is not None and report.phase == "accepted" and report.status is None
    assert report.expected_commit == SHA and report.from_commit == env.old_commit
    assert files.read_latest_report(env.paths)["run_id"] == args["run_id"]
    run.warn("no_linger")
    run.finish("failed", error=runner.issue("update_stop_failed", log=args["log"]), installed=env.old_commit)
    data = files.read_run(env.paths, args["run_id"])
    report = pu.UpdateRunReport.model_validate({**data, "added_by_a_later_runner": {"x": 1}})
    assert report.final and report.error.code == "update_stop_failed" and report.warnings[0].code == "no_linger"
    assert report.installed_commit == env.old_commit and pu.json_size(report.to_wire()) <= pu.MAX_REPORT_BYTES


def test_dead_run_finalization_agrees(env):
    def dead(rid):
        files.run_dir(env.paths, rid).mkdir(parents=True)
        files.atomic_write_json(files.run_file(env.paths, rid), {
            "v": 1, "run_id": rid, "job_id": "j", "origin": "remote", "phase": "switching", "status": None,
            "started_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z"})
        files.write_latest(env.paths, rid)

    dead("u20261013120001-00000001")
    by_files = files.finalize_dead_run(env.paths, installed_commit=env.old_commit, computer="testbox")
    dead("u20261013120002-00000002")
    run, _ = _apply_run(env)
    run._finalize_previous()
    by_runner = files.read_run(env.paths, "u20261013120002-00000002")
    keys = ("status", "error", "installed_commit")
    assert {k: by_runner[k] for k in keys} == {k: by_files[k] for k in keys}


def test_agent_and_drain_files_written_by_homemesh_are_understood(env):
    files.write_agent_file(env.paths, state="connected", build_commit=SHA, detail="ok")
    agent = runner.read_json(files.agent_file(env.paths))
    started = runner.proc_start_time(os.getpid())
    proc = runner._Proc(pid=os.getpid(), started=started)
    assert runner._Apply._agent_is({**agent, "unknown_later_field": 1}, proc)
    assert agent["state"] == "connected" and agent["build_commit"] == SHA
    assert not runner._Apply._agent_is({**agent, "pid": os.getpid() + 1}, proc)
    if started is not None:
        assert not runner._Apply._agent_is({**agent, "process_started": started - 50}, proc)
    rid = "u20261013120001-00000001"
    files.run_dir(env.paths, rid).mkdir(parents=True)
    files.write_drain_file(env.paths, rid, [{"job_id": "j1", "type": "chat"}])
    drain = runner.read_json(files.drain_file(env.paths, rid))
    assert runner.parse_iso(drain["at"]) is not None and drain["running"] == [{"job_id": "j1", "type": "chat"}]


def test_pid_record_identity_matches_psutil(env):
    write_pid_record(env.paths.pid_file, ready=True)
    run, _ = _apply_run(env)
    ident = run.identify()
    if sys.platform.startswith("linux") or sys.platform == "win32":
        assert ident is not None and ident.pid == os.getpid() and run.alive(ident)
    else:
        assert ident is None  # this platform can't verify a start time: no identity, no signals
    rec = json.loads(env.paths.pid_file.read_text())
    env.paths.pid_file.write_text(json.dumps({**rec, "started": rec["started"] - 5}))
    assert run.identify() is None  # a reused PID is never trusted


def test_install_record_written_by_the_runner_loads_and_identifies_the_build(env, tmp_path):
    run, args = _apply_run(env)
    to_venv = env.prefix / "venvs" / f"{SHA[:12]}-20261013120000"
    to_venv.mkdir(parents=True)
    run.sha = SHA
    run.up = runner._Upstream(branch="main", remote="origin", rbranch="main")
    run.facts = runner._Facts(sha=SHA, upstream_date="2026-10-13T12:00:00+02:00", head=None, installed=None,
                              base=SHA, behind=1, ahead=0, commits=[], commits_truncated=False, web_changed=None,
                              node_found=None, migrations=None, target_schema=3, target_layout=1, problems=[])
    run.write_record(str(to_venv))
    rec = record.load(to_venv)
    assert rec is not None and rec.commit == SHA and rec.built_by == "updater" and rec.run_id == args["run_id"]
    assert rec.layout == 1 and rec.venv == os.path.realpath(to_venv) and rec.stable_venv == str(env.stable)
    assert rec.git and rec.source_dir == str(env.repo.src) and rec.commit_date.startswith("2026-10-13")
    raw = json.loads(record.record_path(to_venv).read_text())
    assert set(raw) == set(record.InstallRecord.model_fields)
    # through the stable link, build_info() reports it as what runs
    link = tmp_path / "link"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(to_venv), str(link))
    else:
        os.symlink(to_venv, link)
    rec2 = rec.model_copy(update={"stable_venv": str(link)})
    info = record.build_info(env.paths, prefix=link, record=rec2)
    assert info.source == "updater" and info.commit == SHA and info.can_update


def test_stop_request_and_lock_names_come_from_args(env):
    a = env.args("apply", expected=SHA)
    assert a["stop_request_file"] == str(env.paths.stop_request_file)
    assert a["instance_lock"] == str(env.paths.lock_file) and a["pid_file"] == str(env.paths.pid_file)
    src = Path(runner.__file__).read_text(encoding="utf-8")
    for name in ("homemesh.stop-request", "homemesh.lock", "homemesh.pid", "remote-updates.allowed",
                 "worker-credentials.json\"", "homemesh.sqlite3"):
        assert name not in src, f"runner.py hardcodes {name}"


def test_tool_env_is_an_allowlist(env, monkeypatch):
    monkeypatch.setenv("HOMEMESH__SERVER__PORT", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("PYTHONPATH", "/evil")
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/s")
    monkeypatch.setenv("DISPLAY", ":0")
    run, _ = _apply_run(env)
    tool = run.tool_env(git=True)
    for name in ("HOMEMESH__SERVER__PORT", "OPENAI_API_KEY", "PYTHONPATH", "GIT_TRACE", "SSH_AUTH_SOCK", "DISPLAY"):
        assert name not in tool
    assert tool["GIT_TERMINAL_PROMPT"] == "0" and tool["GIT_ASKPASS"] == "" and tool["GCM_INTERACTIVE"] == "never"
    assert os.path.dirname(env.args("apply", expected=SHA)["git"]) in tool["PATH"]
    hm = run.homemesh_env(detached=True)
    assert hm["HOMEMESH_HOME"] == str(env.home) and hm["HOMEMESH_DETACHED"] == "1" and "PYTHONPATH" not in hm
    assert "HOMEMESH_DETACHED" not in run.homemesh_env()


def test_awake_deadline_ignores_a_suspend():
    t = {"now": 0.0}
    d = runner.AwakeDeadline(10, lambda: t["now"])
    for _ in range(5):
        t["now"] += 1
        assert not d.expired()
    t["now"] += 3600  # the computer slept
    assert not d.expired() and d.remaining() == pytest.approx(5)
    for _ in range(5):
        t["now"] += 1
    assert d.expired()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_timeout_kills_the_whole_process_group(env, tmp_path):
    run = runner._Check(env.args("check"), runner.Hooks())
    pidfile = tmp_path / "grandchild.pid"
    t0 = time.monotonic()
    res = run.cmd("x", ["/bin/sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], timeout_s=1.0,
                  env=run.tool_env())
    assert res.timed_out and time.monotonic() - t0 < 10
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while runner.proc_start_time(grandchild) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runner.proc_start_time(grandchild) is None, "the grandchild survived the timeout"
    assert [p.name for p in files.checks_dir(env.paths).iterdir() if p.name.endswith(".tmp")] == []


def test_command_output_is_captured_without_pipes_and_decoded_leniently(env):
    run = runner._Check(env.args("check"), runner.Hooks())
    res = run.cmd("x", [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xff \\xc5\\x81\\n');"
                                  "sys.stderr.write('warn\\n')"], timeout_s=30, env=dict(os.environ), capture=True)
    assert res.ok and res.out == "ok \ufffd Ł\n" and res.err == "warn\n"
    missing = run.cmd("x", [str(env.tmp / "does-not-exist")], timeout_s=5, env=dict(os.environ))
    assert missing.returncode == 127 and not missing.ok
