r"""Service management: systemd user units (Linux/Bazzite), a per-user autostart entry
(Windows), and a portable background-process fallback.

Nothing here requires administrator rights. On Linux the unit lives in
``~/.config/systemd/user/homemesh.service``; to keep it running while logged out, the owner
can run ``loginctl enable-linger $USER`` (printed as a hint, never executed automatically).
On Windows a value named "HomeMesh" under ``HKCU\Software\Microsoft\Windows\CurrentVersion\Run``
starts ``pythonw -m homemesh run`` when the user logs in. (A Scheduled Task with a logon
trigger usually needs administrator rights, so it is not used.)
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil

from homemesh.common.instance import legacy_pid, lock_is_held, pid_record_ready, read_pid_record
from homemesh.common.paths import AppPaths
from homemesh.common.proc import run_quiet

DETACHED_ENV = "HOMEMESH_DETACHED"  # set for background runs: no console, see cli.main
UNIT_NAME = "homemesh.service"
AUTOSTART_NAME = "HomeMesh"
WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _python_for_service() -> str:
    """The interpreter the unit / Run entry / background start use.

    With an install record it is the one under the stable path ``<prefix>/venv`` (a link to the current
    ``venvs/<id>``), so launchers follow updates instead of pinning one venv: a console script run from
    ``~/.local/bin`` has the concrete ``venvs/<id>/bin/python`` as ``sys.executable``."""
    from homemesh.update.record import service_python

    stable = service_python()
    if stable is not None:
        return str(stable)
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return str(exe)


def _win_autostart(enable: bool | None) -> bool:
    """Query (``None``), add (``True``) or remove (``False``) the per-user Run entry."""
    import winreg  # Windows only

    access = winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WIN_RUN_KEY, 0, access) as key:
        if enable:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, f'"{_python_for_service()}" -m homemesh run')
            return True
        try:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        except FileNotFoundError:
            return False
        if enable is False:
            winreg.DeleteValue(key, AUTOSTART_NAME)
        return True


def systemd_available() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return False
    r = run_quiet(["systemctl", "--user", "show-environment"], capture_output=True, text=True, check=False)
    return r.returncode == 0


def unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT_NAME


def render_unit(paths: AppPaths) -> str:
    env_lines = []
    if os.environ.get("HOMEMESH_HOME"):
        env_lines.append(f"Environment=HOMEMESH_HOME={os.environ['HOMEMESH_HOME']}")
    env = "\n".join(env_lines)
    return f"""[Unit]
Description=HomeMesh AI (personal AI platform)
Documentation=https://github.com/ladelisandwich/myai
After=network-online.target
Wants=network-online.target
# Not set up yet (no config): don't start a default instance at login.
ConditionPathExists={paths.config_file}
# A broken install (e.g. the system Python changed under the venv) must not restart every 5 s forever.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
ExecStart={_python_for_service()} -m homemesh run
Restart=on-failure
RestartSec=5
# Exit code 2 = refused to start because of the configuration: retrying cannot help.
RestartPreventExitStatus=2
TimeoutStopSec=30
{env}
# Hardening (user services)
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths={paths.data_dir} {paths.config_dir}

[Install]
WantedBy=default.target
"""


def install_service(paths: AppPaths) -> str:
    if sys.platform.startswith("linux"):
        if not systemd_available():
            return ("systemd user services are not available here. Use 'homemesh start' (background process) "
                    "or run 'homemesh run' from your desktop's autostart.")
        p = unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(render_unit(paths), encoding="utf-8")
        run_quiet(["systemctl", "--user", "daemon-reload"], check=False)
        run_quiet(["systemctl", "--user", "enable", UNIT_NAME], check=False)
        return (f"Installed {p} and enabled it at login. Start now with 'homemesh start'.\n"
                f"To keep HomeMesh running when you are logged out, run once: loginctl enable-linger {os.environ.get('USER', '$USER')}")
    if sys.platform == "win32":
        try:
            _win_autostart(True)
        except OSError as exc:
            return f"Could not add the autostart entry: {exc}"
        extra = ("\nNote: HOMEMESH_HOME is set only in this terminal; set it as a user environment variable "
                 "so the autostarted process uses the same data.") if os.environ.get("HOMEMESH_HOME") else ""
        return ("HomeMesh will start in the background when you log in (per-user autostart entry, no admin rights "
                "needed). Start it now with 'homemesh start'." + extra)
    return "Automatic start is not implemented for this OS yet; use 'homemesh start'."


def uninstall_service() -> str:
    if sys.platform.startswith("linux"):
        p = unit_path()
        if systemd_available():
            run_quiet(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False, capture_output=True)
        if p.exists():
            p.unlink()
            if systemd_available():
                run_quiet(["systemctl", "--user", "daemon-reload"], check=False)
            return f"Removed {p}. Your data was not touched."
        return "No systemd unit was installed."
    if sys.platform == "win32":
        try:
            removed = _win_autostart(False)
        except OSError as exc:
            return f"Could not remove the autostart entry: {exc}"
        return "Removed the autostart entry. Your data was not touched." if removed else "No autostart entry was installed."
    return "Nothing to uninstall."


def service_installed() -> bool:
    if sys.platform.startswith("linux"):
        return unit_path().exists()
    if sys.platform == "win32":
        try:
            return _win_autostart(None)
        except OSError:
            return False
    return False


def read_pid(paths: AppPaths) -> int | None:
    """PID of the HomeMesh process running for this data folder, or None.

    Running means: the instance lock is held AND the PID record matches a live process that started
    at the recorded time. A PID left behind by a process Windows ended at logoff may by now belong to
    another program; it is never reported (or stopped) as HomeMesh.
    """
    if not lock_is_held(paths.lock_file):
        # A version from before the lock (still running during an upgrade) holds none.
        return legacy_pid(paths.pid_file)
    return read_pid_record(paths.pid_file)


def start(paths: AppPaths) -> tuple[bool, str]:
    """Start HomeMesh in the background and wait until it serves. Returns (running, message)."""
    if read_pid(paths):
        return True, "HomeMesh is already running."
    if sys.platform.startswith("linux") and unit_path().exists() and systemd_available():
        r = run_quiet(["systemctl", "--user", "start", UNIT_NAME], capture_output=True, text=True, check=False)
        if r.returncode != 0:
            return False, f"systemctl failed: {r.stderr.strip()}"
        if _wait_ready(paths, lambda: not _unit_active()):
            return True, "Started via systemd."
        return False, ("systemd started HomeMesh, but it did not come up. See 'journalctl --user -u homemesh -n 50' "
                       f"and {paths.logs_dir / 'homemesh.log'}.")
    # Portable fallback: a detached background process. It logs to homemesh.log; anything printed
    # before logging starts (e.g. a broken config file) goes to console.log (see cli.main).
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL,
                    "env": {**os.environ, DETACHED_ENV: "1"}}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    child = subprocess.Popen([_python_for_service(), "-m", "homemesh", "run"], **kwargs)
    if _wait_ready(paths, lambda: child.poll() is not None):
        return True, f"Started in the background (logs: {paths.logs_dir / 'homemesh.log'})."
    if child.poll() is not None:
        why = ""
        if child.returncode == 2 and (lock_is_held(paths.lock_file) or legacy_pid(paths.pid_file)):
            why = " Another HomeMesh process is already running for this data folder; 'homemesh stop' it first."
        return False, (f"HomeMesh exited right away (code {child.returncode}).{why} See {paths.logs_dir / 'console.log'} "
                       f"and {paths.logs_dir / 'homemesh.log'}, or run 'homemesh run' to see the error.")
    return False, (f"Started, but it is not answering yet - check 'homemesh status' in a minute, and "
                   f"{paths.logs_dir / 'homemesh.log'} and {paths.logs_dir / 'console.log'}.")


UNIT_ACTIVE_STATES = ("active", "activating", "reloading")


def _unit_state() -> str:
    """``systemctl --user is-active homemesh.service``: active, activating, reloading, deactivating,
    inactive, failed, …"""
    r = run_quiet(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True, text=True, check=False)
    return (r.stdout or "").strip()


def _unit_active() -> bool:
    return _unit_state() in UNIT_ACTIVE_STATES


def _wait_ready(paths: AppPaths, gave_up: Callable[[], bool], timeout_s: float = 60.0) -> bool:
    """Wait for the running process's ready flag (the first start after an upgrade also migrates the
    database, so this can take a while). Stops early when ``gave_up()`` says the process is gone."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.2)
        if read_pid(paths) and pid_record_ready(paths.pid_file):
            return True
        if gave_up():
            time.sleep(0.3)
            return bool(read_pid(paths) and pid_record_ready(paths.pid_file))
    return False


def stop(paths: AppPaths, timeout: float = 30.0, *, force: bool = False) -> str:
    if sys.platform.startswith("linux") and unit_path().exists() and systemd_available():
        # Every state except inactive/failed is systemd's to stop: "activating" is also a unit waiting to
        # auto-restart after a crash, which a plain kill would not stop (the next restart comes 5 s later).
        state = _unit_state()
        if state in UNIT_ACTIVE_STATES or state == "deactivating":
            run_quiet(["systemctl", "--user", "stop", UNIT_NAME], check=False)
            if not read_pid(paths):
                return "Stopped via systemd."
            # Another HomeMesh, started outside the unit (e.g. 'homemesh run' in a terminal), still runs.
    pid = read_pid(paths)
    if not pid:
        return "HomeMesh is not running."
    try:
        proc: psutil.Process | None = psutil.Process(pid)  # identity fixed now (PID plus start time)
    except psutil.Error:
        proc = None
    # Graceful on every OS: the process watches for this file and shuts down cleanly (running
    # jobs are cancelled and recorded). On POSIX we also send SIGTERM, which does the same.
    paths.stop_request_file.write_text(str(pid), encoding="utf-8")
    if sys.platform != "win32":
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_pid(paths) != pid:
            # It released the lock; wait for the process itself to end too, so its files (the venv's
            # python.exe and extension modules on Windows) are free when an installer replaces them.
            if proc is not None:
                with contextlib.suppress(psutil.Error):
                    proc.wait(timeout=max(1.0, min(15.0, deadline - time.monotonic())))
            return "Stopped."
        time.sleep(0.3)
    if force:
        if read_pid(paths) != pid:  # verified again right before terminating anything
            return "Stopped."
        if sys.platform == "win32":
            run_quiet(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGKILL)
        return (f"Process {pid} did not stop within {timeout:.0f}s and was terminated forcibly. "
                "Interrupted jobs are marked as such on the next start.")
    return f"Process {pid} did not exit within {timeout:.0f}s. Use 'homemesh stop --force' to terminate it."
