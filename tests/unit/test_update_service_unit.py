"""cli/service.py for the blue/green layout: launchers use the stable venv path, the unit gives up on a
crash loop, and ``homemesh stop`` stops a unit in every state systemd still manages."""

import sys
from pathlib import Path

import pytest

from homemesh.cli import service
from homemesh.common.paths import AppPaths
from homemesh.update import record

ROOT = Path(__file__).resolve().parents[2]


def _installed(tmp_path, monkeypatch):
    """A venv under <prefix>/venvs/<id> with its record, running as sys.prefix (a console script from
    ~/.local/bin runs exactly this concrete path)."""
    prefix = tmp_path / "prefix"
    venv = prefix / "venvs" / "aaaaaaaaaaaa-20261016120000"
    venv.mkdir(parents=True)
    stable = prefix / "venv"
    record.write(record.make_record(venv=venv, stable_venv=stable, built_by="installer"))
    monkeypatch.setattr(sys, "prefix", str(venv))
    exe = record.stable_python(stable)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return stable, exe


def test_launchers_use_the_stable_venv_path(tmp_path, monkeypatch):
    assert service._python_for_service() in (sys.executable, str(Path(sys.executable).with_name("pythonw.exe")))
    _stable, exe = _installed(tmp_path, monkeypatch)
    assert service._python_for_service() == str(exe)
    unit = service.render_unit(AppPaths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data"))
    assert f"ExecStart={exe} -m homemesh run" in unit


def test_unit_gives_up_after_five_quick_failures(tmp_path):
    unit = service.render_unit(AppPaths(config_dir=tmp_path / "cfg", data_dir=tmp_path / "data"))
    head, _, rest = unit.partition("[Service]")
    assert "[Unit]" in head and "StartLimitIntervalSec=300" in head and "StartLimitBurst=5" in head
    assert "StartLimit" not in rest and "Restart=on-failure" in rest and "RestartPreventExitStatus=2" in rest
    ref = (ROOT / "deploy" / "linux" / "homemesh.service").read_text(encoding="utf-8")
    head, _, rest = ref.partition("[Service]")
    assert "StartLimitIntervalSec=300" in head and "StartLimitBurst=5" in head
    assert "ExecStart=%h/.local/share/homemesh/venv/bin/python -m homemesh run" in rest


@pytest.mark.parametrize(("state", "systemd_stops"), [
    ("active", True), ("activating", True), ("reloading", True), ("deactivating", True),
    ("inactive", False), ("failed", False)])
def test_stop_uses_systemd_in_every_state_it_manages(tmp_path, monkeypatch, state, systemd_stops):
    """'activating' is also a crashed unit waiting to auto-restart: only systemctl stops that."""
    unit = tmp_path / "homemesh.service"
    unit.write_text("[Unit]\n", encoding="utf-8")
    calls = []

    class Result:
        def __init__(self, stdout=""):
            self.stdout, self.stderr, self.returncode = stdout, "", 0

    def fake_run(args, **kwargs):
        calls.append(args[2:])
        return Result(state + "\n" if args[2] == "is-active" else "")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    monkeypatch.setattr(service, "systemd_available", lambda: True)
    monkeypatch.setattr(service, "run_quiet", fake_run)
    monkeypatch.setattr(service, "read_pid", lambda paths: None)
    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    message = service.stop(paths)
    assert (["stop", service.UNIT_NAME] in calls) is systemd_stops
    assert message == ("Stopped via systemd." if systemd_stops else "HomeMesh is not running.")


def test_stop_also_stops_a_homemesh_started_outside_the_unit(tmp_path, monkeypatch):
    unit = tmp_path / "homemesh.service"
    unit.write_text("[Unit]\n", encoding="utf-8")

    class Result:
        stdout, stderr, returncode = "active\n", "", 0

    pids = iter([999999, 999999, None])
    killed = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(service, "unit_path", lambda: unit)
    monkeypatch.setattr(service, "systemd_available", lambda: True)
    monkeypatch.setattr(service, "run_quiet", lambda args, **kw: Result())
    monkeypatch.setattr(service, "read_pid", lambda paths: next(pids, None))
    monkeypatch.setattr(service.os, "kill", lambda pid, sig: killed.append(pid))
    paths = AppPaths(config_dir=tmp_path, data_dir=tmp_path / "data")
    paths.data_dir.mkdir()
    assert service.stop(paths) == "Stopped."
    assert killed == [999999] and paths.stop_request_file.read_text(encoding="utf-8") == "999999"
