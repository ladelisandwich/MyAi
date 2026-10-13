"""Open a web link in the desktop's default browser on the computer that runs the tool.

Only links that passed :func:`homemesh.tools.urlsafety.normalize_url` reach this module, so
the string is always ``http://`` or ``https://`` followed by printable ASCII. No shell is used.

* Windows: ``os.startfile`` (the shell's "open" verb), which hands ``http``/``https`` links to the
  browser registered for them. HomeMesh runs in the signed-in user's session, so the browser
  opens on that user's desktop.
* macOS: ``open``.
* Linux: ``xdg-open``, which needs a graphical session (``WAYLAND_DISPLAY`` or ``DISPLAY``); a
  HomeMesh started without one (for example over SSH) reports that instead of guessing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading

from homemesh.common.proc import spawn_quiet

LAUNCH_WAIT_S = 10.0


class BrowserUnavailable(OSError):
    pass


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
        if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
            raise BrowserUnavailable("HomeMesh on this computer is not running inside a graphical desktop session, "
                                     "so it cannot open a browser here")
        opener = shutil.which("xdg-open")
        if opener is None:
            raise BrowserUnavailable("xdg-open is not installed on this computer")
        cmd = [opener, url]
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
