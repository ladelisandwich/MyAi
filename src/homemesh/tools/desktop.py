"""Open a web link in the desktop's default browser on the computer that runs the tool.

Only links that passed :func:`homemesh.tools.urlsafety.normalize_url` reach this module, so
the string is always ``http://`` or ``https://`` followed by printable ASCII. No shell is used.

* Windows: ``os.startfile`` (the shell's "open" verb), which hands ``http``/``https`` links to the
  browser registered for them. HomeMesh runs in the signed-in user's session, so the browser
  opens on that user's desktop.
* macOS: ``open``.
* Linux: ``xdg-open`` in the user's graphical session. A HomeMesh started by systemd before the
  desktop (autostart) has no display in its environment, so the opener is started through the user
  manager (``systemd-run --user``), which knows the desktop's environment. Without any desktop
  session it says so instead of guessing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading

from homemesh.common.proc import run_quiet, spawn_quiet

LAUNCH_WAIT_S = 10.0


class BrowserUnavailable(OSError):
    pass


def _user_manager_has_desktop() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    try:
        r = run_quiet(["systemctl", "--user", "show-environment"], capture_output=True, text=True, timeout=5,
                      check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and any(line.startswith(("WAYLAND_DISPLAY=", "DISPLAY="))
                                     for line in r.stdout.splitlines())


async def open_in_browser(url: str) -> str:
    """Ask the desktop to open ``url``. Returns how it was opened. Raises on failure."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("only http:// and https:// links can be opened")
    if sys.platform == "win32":
        await asyncio.to_thread(os.startfile, url)  # type: ignore[attr-defined]
        return "Windows default browser"
    if sys.platform == "darwin":
        cmd = ["open", url]
    else:
        opener = shutil.which("xdg-open")
        if opener is None:
            raise BrowserUnavailable("xdg-open is not installed on this computer")
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
            cmd = [opener, url]
        elif await asyncio.to_thread(_user_manager_has_desktop):
            # Started as a systemd user service before the desktop session existed, HomeMesh's own
            # environment has no display; the user manager learns it when the desktop starts.
            # systemd-run starts the opener there, in the desktop's environment.
            cmd = ["systemd-run", "--user", "--collect", "--quiet", "--", opener, url]
        else:
            raise BrowserUnavailable("no graphical desktop session is running for this user on this computer, "
                                     "so HomeMesh cannot open a browser here")
    proc = spawn_quiet(cmd, start_new_session=True)
    try:
        code = await asyncio.to_thread(proc.wait, LAUNCH_WAIT_S)
    except subprocess.TimeoutExpired:
        # Some openers stay running while the browser starts; reap it in the background.
        threading.Thread(target=proc.wait, daemon=True, name="browser-opener").start()
        return "default browser (opener still running)"
    if code != 0:
        raise OSError(f"{os.path.basename(cmd[0])} could not open the link (exit code {code})")
    return "default browser"
