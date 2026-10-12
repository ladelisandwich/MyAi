"""Service management: systemd user units (Linux/Bazzite), a per-user logon task (Windows),
and a portable background-process fallback.

Nothing here requires administrator rights. On Linux the unit lives in
``~/.config/systemd/user/homemesh.service``; to keep it running while logged out, the owner
can run ``loginctl enable-linger $USER`` (printed as a hint, never executed automatically).
On Windows a Scheduled Task named "HomeMesh" starts ``pythonw -m homemesh run`` at logon.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from homemesh.common.paths import AppPaths

UNIT_NAME = "homemesh.service"
TASK_NAME = "HomeMesh"


def _python_for_service() -> str:
    exe = Path(sys.executable)
    if sys.platform == "win32":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return str(exe)


def systemd_available() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "show-environment"], capture_output=True, text=True, check=False)
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

[Service]
Type=simple
ExecStart={_python_for_service()} -m homemesh run
Restart=on-failure
RestartSec=5
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
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", UNIT_NAME], check=False)
        return (f"Installed {p} and enabled it at login. Start now with 'homemesh start'.\n"
                f"To keep HomeMesh running when you are logged out, run once: loginctl enable-linger {os.environ.get('USER', '$USER')}")
    if sys.platform == "win32":
        cmd = f'"{_python_for_service()}" -m homemesh run'
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", cmd, "/F"],
                           capture_output=True, text=True, check=False)
        if r.returncode != 0:
            return f"Could not create the scheduled task: {r.stderr or r.stdout}"
        return "Created the 'HomeMesh' scheduled task (starts at logon, no admin rights needed)."
    return "Automatic start is not implemented for this OS yet; use 'homemesh start'."


def uninstall_service() -> str:
    if sys.platform.startswith("linux"):
        p = unit_path()
        if systemd_available():
            subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], check=False, capture_output=True)
        if p.exists():
            p.unlink()
            if systemd_available():
                subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            return f"Removed {p}. Your data was not touched."
        return "No systemd unit was installed."
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, check=False)
        return "Removed the 'HomeMesh' scheduled task. Your data was not touched."
    return "Nothing to uninstall."


def service_installed() -> bool:
    if sys.platform.startswith("linux"):
        return unit_path().exists()
    if sys.platform == "win32":
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True, text=True, check=False)
        return r.returncode == 0
    return False


def read_pid(paths: AppPaths) -> int | None:
    try:
        pid = int(paths.pid_file.read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def pid_alive(pid: int) -> bool:
    try:
        import psutil

        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def start(paths: AppPaths) -> str:
    if read_pid(paths):
        return "HomeMesh is already running."
    if sys.platform.startswith("linux") and unit_path().exists() and systemd_available():
        r = subprocess.run(["systemctl", "--user", "start", UNIT_NAME], capture_output=True, text=True, check=False)
        return "Started via systemd." if r.returncode == 0 else f"systemctl failed: {r.stderr.strip()}"
    if sys.platform == "win32" and service_installed():
        subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, check=False)
        return "Started the HomeMesh scheduled task."
    # Portable fallback: detached background process logging to the data directory.
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log = open(paths.logs_dir / "background.log", "ab")
    kwargs: dict = {"stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([_python_for_service(), "-m", "homemesh", "run"], **kwargs)
    for _ in range(50):
        time.sleep(0.2)
        if read_pid(paths):
            return f"Started in the background (logs: {paths.logs_dir / 'homemesh.log'})."
    return f"Started; it is taking a while to come up - check {paths.logs_dir / 'background.log'}."


def stop(paths: AppPaths, timeout: float = 30.0) -> str:
    if sys.platform.startswith("linux") and unit_path().exists() and systemd_available():
        r = subprocess.run(["systemctl", "--user", "is-active", UNIT_NAME], capture_output=True, text=True, check=False)
        if r.stdout.strip() == "active":
            subprocess.run(["systemctl", "--user", "stop", UNIT_NAME], check=False)
            return "Stopped via systemd."
    pid = read_pid(paths)
    if not pid:
        return "HomeMesh is not running."
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, check=False)
    else:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return "Stopped."
        time.sleep(0.3)
    return f"Process {pid} did not exit within {timeout:.0f}s."
