import asyncio

from homemesh.common.paths import AppPaths
from homemesh.runtime import _watch_stop_request


async def test_stop_request_file_triggers_graceful_stop(tmp_path):
    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    paths.data_dir.mkdir()
    paths.stop_request_file.write_text("stale")  # left over from an earlier run: must be ignored
    stop = asyncio.Event()
    task = asyncio.create_task(_watch_stop_request(paths, stop, interval_s=0.01))
    await asyncio.sleep(0.05)
    assert not stop.is_set() and not paths.stop_request_file.exists()
    paths.stop_request_file.write_text("123")
    await asyncio.wait_for(task, 2)
    assert stop.is_set() and not paths.stop_request_file.exists()


def test_worker_link_reports_protocol_mismatch(tmp_path, monkeypatch):
    import httpx

    from homemesh.cli import doctor
    from homemesh.common.config import HomeMeshConfig
    from homemesh.worker.credentials import WorkerCredentials

    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(WorkerCredentials, "load", classmethod(
        lambda cls, p: cls(coordinator_url="http://127.0.0.1:1", node_id="node_x", node_secret="s")))

    def fake_get(url, **kw):
        return httpx.Response(200, json={"version": "9.0.0", "protocol_version": 99})

    monkeypatch.setattr(doctor.httpx, "get", fake_get)
    check = doctor._worker_link(HomeMeshConfig(), paths)
    assert check.status == doctor.FAIL and "protocol v99" in check.detail and "Upgrade" in check.hint


def test_cli_output_survives_legacy_console_encoding(tmp_path):
    """Windows consoles redirected to a pipe use cp1252, which has no ✓/✗; output must not crash."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "HOMEMESH_HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-m", "homemesh", "config", "validate"], capture_output=True, env=env,
                       timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("cp1252", "replace")
    assert r.stdout.decode("cp1252").startswith("[ok] ")


def test_tls_init_new_ca_replaces_ca_and_keeps_old(tmp_path):
    from homemesh.cli.main import main
    from homemesh.coordinator.tls import ca_fingerprint

    assert main(["--home", str(tmp_path), "tls", "init"]) == 0
    first = ca_fingerprint(tmp_path / "data" / "tls" / "ca.pem")
    assert main(["--home", str(tmp_path), "tls", "init"]) == 0  # idempotent: same CA
    assert ca_fingerprint(tmp_path / "data" / "tls" / "ca.pem") == first
    assert main(["--home", str(tmp_path), "tls", "init", "--new-ca"]) == 0
    assert ca_fingerprint(tmp_path / "data" / "tls" / "ca.pem") != first
    kept = list((tmp_path / "data").glob("tls-replaced-*"))
    assert len(kept) == 1 and ca_fingerprint(kept[0] / "ca.pem") == first


def test_helper_programs_never_open_a_console_window_on_windows(monkeypatch):
    """HomeMesh runs windowless (pythonw); every nvidia-smi/PowerShell call must pass CREATE_NO_WINDOW,
    otherwise Windows flashes a terminal window on each heartbeat."""
    import subprocess
    import sys

    from homemesh.common import proc

    seen = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append(kw) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(sys, "platform", "win32")
    proc.run_quiet(["nvidia-smi"], capture_output=True)
    proc.run_quiet(["x"], creationflags=0x200)
    assert seen[0]["creationflags"] == proc.CREATE_NO_WINDOW and seen[0]["capture_output"] is True
    assert seen[1]["creationflags"] == 0x200 | proc.CREATE_NO_WINDOW
    monkeypatch.setattr(sys, "platform", "linux")
    proc.run_quiet(["nvidia-smi"])
    assert "creationflags" not in seen[2]


def test_no_direct_subprocess_run_outside_the_helper():
    from pathlib import Path

    import homemesh

    root = Path(homemesh.__file__).parent
    offenders = [str(f.relative_to(root)) for f in root.rglob("*.py")
                 if f.name != "proc.py" and "subprocess.run(" in f.read_text(encoding="utf-8")]
    assert offenders == [], f"use homemesh.common.proc.run_quiet instead: {offenders}"


def test_start_refuses_on_a_computer_that_is_not_set_up(tmp_path, monkeypatch):
    from homemesh.cli import main as cli_main
    from homemesh.cli import service

    monkeypatch.setattr(service, "start", lambda paths: (_ for _ in ()).throw(AssertionError("must not start")))
    assert cli_main.main(["--home", str(tmp_path), "start"]) == 2


def test_incomplete_fingerprint_is_reported_without_contacting_the_coordinator(tmp_path, monkeypatch):
    import pytest

    from homemesh.common.paths import AppPaths
    from homemesh.worker import enroll

    monkeypatch.setattr(enroll, "fetch_ca_unverified", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")))
    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    with pytest.raises(enroll.EnrollmentError, match="incomplete"):
        enroll.join("https://192.168.50.187:8765", "XXXX-XXXX-XXXX", paths, node_name="x", fingerprint="0A:69:…:40")


def test_systemd_unit_waits_for_setup_and_does_not_loop_on_config_errors(tmp_path):
    from homemesh.cli.service import render_unit
    from homemesh.common.paths import AppPaths

    unit = render_unit(AppPaths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data"))
    assert f"ConditionPathExists={tmp_path / 'cfg' / 'homemesh.toml'}" in unit
    assert "RestartPreventExitStatus=2" in unit


def test_only_one_homemesh_per_data_folder(tmp_path):
    from homemesh.common.instance import InstanceLock, lock_is_held

    lock_path = tmp_path / "homemesh.lock"
    first = InstanceLock(lock_path)
    assert first.acquire() and lock_is_held(lock_path)
    assert not InstanceLock(lock_path).acquire()
    first.release()
    assert not lock_is_held(lock_path)
    again = InstanceLock(lock_path)
    assert again.acquire()
    again.release()


async def test_second_instance_exits_before_touching_the_database(tmp_path):
    from homemesh.common.config import HomeMeshConfig
    from homemesh.common.instance import InstanceLock
    from homemesh.common.paths import default_paths
    from homemesh.runtime import run_roles

    paths = default_paths(tmp_path)
    paths.ensure()
    running = InstanceLock(paths.lock_file)
    assert running.acquire()  # stands in for the instance that is already running
    try:
        code = await run_roles(HomeMeshConfig(), paths, install_signal_handlers=False)
    finally:
        running.release()
    assert code == 2
    assert not paths.db_file.exists()  # no crash recovery against the live instance's data


# Stand-in for a HomeMesh from before the instance lock: its command line ends in "homemesh run", it
# writes a bare PID, takes no lock, stops when the stop-request file appears and then deletes its PID file.
_OLD_HOMEMESH = """
import os, sys, time
from pathlib import Path
pid_file, stop_file = Path(sys.argv[1]), Path(sys.argv[2])
pid_file.write_text(str(os.getpid()))
for _ in range(600):
    if stop_file.exists():
        stop_file.unlink()
        break
    time.sleep(0.05)
pid_file.unlink()
"""


def _start_old_homemesh(paths):
    import subprocess
    import sys
    import time

    proc = subprocess.Popen([sys.executable, "-c", _OLD_HOMEMESH, str(paths.pid_file), str(paths.stop_request_file),
                             "homemesh", "run"])
    for _ in range(100):
        if paths.pid_file.exists() and paths.pid_file.read_text().strip():
            break
        time.sleep(0.05)
    return proc


def test_a_running_pre_lock_homemesh_is_found_and_stopped_after_an_upgrade(tmp_path, monkeypatch):
    import os

    from homemesh.cli import service
    from homemesh.common.instance import legacy_pid
    from homemesh.common.paths import default_paths

    paths = default_paths(tmp_path)
    paths.ensure()
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "no-unit")
    proc = _start_old_homemesh(paths)
    try:
        assert legacy_pid(paths.pid_file) == proc.pid
        assert service.read_pid(paths) == proc.pid  # no lock held, yet it is running
        ok, message = service.start(paths)
        assert ok and message == "HomeMesh is already running."  # no second instance next to it
        assert service.stop(paths, timeout=10) == "Stopped."
        proc.wait(timeout=10)
        assert service.read_pid(paths) is None
    finally:
        if proc.poll() is None:
            proc.kill()
    # A bare PID that belongs to some other program (or to this process) is never taken for HomeMesh.
    paths.pid_file.write_text(str(os.getpid()))
    assert legacy_pid(paths.pid_file) is None


async def test_new_version_refuses_to_run_next_to_a_pre_lock_homemesh(tmp_path):
    from homemesh.common.config import HomeMeshConfig
    from homemesh.common.instance import lock_is_held
    from homemesh.common.paths import default_paths
    from homemesh.runtime import run_roles

    paths = default_paths(tmp_path)
    paths.ensure()
    proc = _start_old_homemesh(paths)
    try:
        code = await run_roles(HomeMeshConfig(), paths, install_signal_handlers=False)
        assert code == 2
        assert not paths.db_file.exists()  # no migration or crash recovery under the running version
        assert paths.pid_file.read_text().strip() == str(proc.pid)  # its PID record is left alone
        assert not lock_is_held(paths.lock_file)
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_start_reports_failure_when_homemesh_does_not_come_up(tmp_path, monkeypatch):
    from homemesh.cli import main as cli_main
    from homemesh.cli import service

    monkeypatch.setenv("HOMEMESH_HOME", str(tmp_path))
    monkeypatch.setattr(service, "start", lambda paths: (False, "HomeMesh exited right away (code 2)."))
    (tmp_path / "homemesh.toml").write_text("")
    assert cli_main.main(["start"]) == 1


def test_a_reused_pid_is_never_taken_for_homemesh(tmp_path, monkeypatch):
    import json
    import os

    from homemesh.cli import service
    from homemesh.common.instance import InstanceLock, read_pid_record, write_pid_record
    from homemesh.common.paths import default_paths

    paths = default_paths(tmp_path)
    paths.ensure()
    write_pid_record(paths.pid_file)
    assert read_pid_record(paths.pid_file) == os.getpid()
    # Same PID, different start time: the number was reused by another program.
    paths.pid_file.write_text(json.dumps({"pid": os.getpid(), "started": 12345.0}))
    assert read_pid_record(paths.pid_file) is None
    # Old bare-PID format: only trusted if that process is HomeMesh (this test process is not).
    paths.pid_file.write_text(str(os.getpid()))
    assert read_pid_record(paths.pid_file) is None
    # Without the instance lock nothing counts as running, so stop never touches the process.
    write_pid_record(paths.pid_file)
    killed = []
    monkeypatch.setattr(service.os, "kill", lambda *a: killed.append(a))
    monkeypatch.setattr(service, "run_quiet", lambda *a, **k: killed.append(a))
    monkeypatch.setattr(service, "unit_path", lambda: tmp_path / "no-unit")
    assert service.read_pid(paths) is None
    assert service.stop(paths, timeout=0.1, force=True) == "HomeMesh is not running."
    assert killed == []
    lock = InstanceLock(paths.lock_file)
    assert lock.acquire()
    try:
        assert service.read_pid(paths) == os.getpid()
    finally:
        lock.release()


def test_log_rotation_waits_while_the_file_cannot_be_renamed(tmp_path, monkeypatch):
    import logging

    from homemesh.common import logging as hm_logging

    path = tmp_path / "homemesh.log"
    h = hm_logging.SafeRotatingFileHandler(path, maxBytes=200, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(message)s"))
    (tmp_path / "homemesh.log.1").write_text("old backup\n")
    real_replace = hm_logging.os.replace

    def locked(src, dst):  # what Windows does while another program has the log open
        if str(src) == str(path):
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(hm_logging.os, "replace", locked)
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "x" * 80, None, None)
    for _ in range(10):
        h.emit(rec)
    assert path.read_text().count("x" * 80) == 10  # nothing dropped
    assert (tmp_path / "homemesh.log.1").read_text() == "old backup\n"  # history not shifted or deleted
    monkeypatch.setattr(hm_logging.os, "replace", real_replace)
    h._blocked_until = 0.0
    h.emit(rec)
    h.close()
    assert (tmp_path / "homemesh.log.1").read_text().count("x" * 80) == 10
    assert (tmp_path / "homemesh.log.2").read_text() == "old backup\n"


def test_doctor_port_check_sees_a_busy_port():
    import socket

    from homemesh.cli.doctor import _port
    from homemesh.common.config import HomeMeshConfig

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        cfg = HomeMeshConfig()
        cfg.server.host = "127.0.0.1"
        cfg.server.port = busy.getsockname()[1]
        assert _port(cfg, running=False).status == "fail"
    finally:
        busy.close()
    assert _port(cfg, running=False).status == "ok"


def test_piped_output_on_windows_uses_the_console_code_page(monkeypatch):
    import io

    from homemesh.cli import main as cli_main

    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(cli_main.sys, "stdout", out)
    monkeypatch.setattr(cli_main.sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setattr(cli_main, "_console_output_codepage", lambda: 850)  # Western European OEM
    cli_main._make_output_safe()
    print("C:\\Users\\José\\AppData ✓", file=out, end="")
    out.flush()
    assert out.buffer.getvalue().decode("cp850") == "C:\\Users\\José\\AppData [ok]"
