"""Platform-appropriate locations for configuration and data.

Resolution order:

1. ``HOMEMESH_HOME`` - a single directory holding ``homemesh.toml`` and ``data/``.
   Used by containers, portable installs and tests.
2. Platform defaults:
   - Linux/BSD: ``$XDG_CONFIG_HOME/homemesh`` and ``$XDG_DATA_HOME/homemesh``
     (``~/.config/homemesh`` and ``~/.local/share/homemesh``). These are user-writable on
     immutable systems such as Bazzite, so no system-wide installation is needed.
   - Windows: ``%APPDATA%\\HomeMesh`` (config) and ``%LOCALAPPDATA%\\HomeMesh`` (data).
   - macOS: ``~/Library/Application Support/HomeMesh`` for both.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILENAME = "homemesh.toml"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILENAME

    @property
    def db_file(self) -> Path:
        return self.data_dir / "homemesh.sqlite3"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def worker_credentials_file(self) -> Path:
        return self.secrets_dir / "worker-credentials.json"

    @property
    def cli_token_file(self) -> Path:
        return self.secrets_dir / "cli-admin.token"

    @property
    def setup_token_file(self) -> Path:
        return self.secrets_dir / "setup-token.txt"

    @property
    def pid_file(self) -> Path:
        return self.data_dir / "homemesh.pid"

    @property
    def lock_file(self) -> Path:
        """Locked by the running HomeMesh process for its whole lifetime (one instance per data folder)."""
        return self.data_dir / "homemesh.lock"

    @property
    def stop_request_file(self) -> Path:
        """Created by ``homemesh stop``; the running process watches for it (works on every OS,
        including Windows where a windowless process cannot receive a console signal)."""
        return self.data_dir / "homemesh.stop-request"

    @property
    def workspace_dir(self) -> Path:
        """Default workspace where the assistant may create files (with approval)."""
        return self.data_dir / "workspace"

    def ensure(self) -> None:
        for d in (self.config_dir, self.data_dir, self.logs_dir, self.backups_dir, self.attachments_dir):
            d.mkdir(parents=True, exist_ok=True)
        ensure_private_dir(self.secrets_dir)
        ensure_private_dir(self.tls_dir)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        if not _inside_user_profile(path):
            _restrict_windows_acl(path)
    elif os.name == "posix":
        os.chmod(path, 0o700)


def _inside_user_profile(path: Path) -> bool:
    profile = os.environ.get("USERPROFILE")
    if not profile:
        return False
    try:
        path.resolve().relative_to(Path(profile).resolve())
    except ValueError:
        return False
    return True


def _restrict_windows_acl(path: Path) -> None:
    """Folders under the user profile inherit an ACL private to that user. Elsewhere (HOMEMESH_HOME on
    another drive) the inherited ACL may let other local users read keys and tokens, so replace it
    with: this user and SYSTEM only. Uses Windows' own icacls on a folder this user created; no admin
    rights needed. Best effort: if it fails, `homemesh doctor` reports it."""
    from homemesh.common.proc import run_quiet

    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    if not user:
        return
    account = f"{domain}\\{user}" if domain else user
    try:
        run_quiet(["icacls", str(path), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F",
                   "/grant:r", "*S-1-5-18:(OI)(CI)F"], capture_output=True, check=False, timeout=30)
    except (OSError, ValueError):
        pass


def write_private_file(path: Path, data: str | bytes) -> None:
    """Write a file readable only by the current user (0600 on POSIX).

    On Windows the file inherits the ACL of its folder: private to the user under the profile
    (``%LOCALAPPDATA%``), and restricted by :func:`ensure_private_dir` elsewhere.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    tmp = path.with_suffix(path.suffix + ".tmp")
    if os.name == "posix":
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as fh:
            fh.write(data)
    else:
        with open(tmp, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as fh:
            fh.write(data)
    os.replace(tmp, path)


def default_paths(home: str | os.PathLike[str] | None = None) -> AppPaths:
    home = home or os.environ.get("HOMEMESH_HOME")
    if home:
        base = Path(home).expanduser().resolve()
        return AppPaths(config_dir=base, data_dir=base / "data")

    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return AppPaths(config_dir=appdata / "HomeMesh", data_dir=local / "HomeMesh")
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "HomeMesh"
        return AppPaths(config_dir=base, data_dir=base / "data")

    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return AppPaths(config_dir=xdg_config / "homemesh", data_dir=xdg_data / "homemesh")
