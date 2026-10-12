"""Safe file-path handling for document indexing and file tools.

Rules enforced here:

* Only paths inside explicitly authorised roots are accessible.
* Paths are resolved with ``realpath``; ``..`` tricks and symlinks that escape a root are
  rejected.
* Filesystem roots (``/``, ``C:\\``) and the bare home directory cannot be authorised as a
  root - HomeMesh never indexes a whole disk or home folder by default.
* Well-known credential stores and secret files (SSH keys, GPG, password managers, browser
  profiles, cloud CLI credentials, Windows credential vaults) are always excluded, even
  inside an authorised root.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

SENSITIVE_DIR_NAMES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".pki", ".gcloud",
    "keyrings", "Keychains", ".mozilla", ".thunderbird", "google-chrome", "chromium", "BraveSoftware",
    "Credentials", "Protect", "Vault", ".vault-token", "1Password", "KeePassXC", "Bitwarden",
    ".electrum", ".bitcoin", ".ethereum", "secrets",
}
SENSITIVE_PATH_PARTS = [
    ("AppData", "Roaming", "Microsoft", "Credentials"),
    ("AppData", "Local", "Microsoft", "Credentials"),
    ("AppData", "Roaming", "Microsoft", "Protect"),
    (".config", "gcloud"),
    (".local", "share", "keyrings"),
]
SENSITIVE_FILE_PATTERNS = [
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.keychain*",
    ".env", ".env.*", "*.env", ".netrc", ".pgpass", ".git-credentials", "credentials", "credentials.*",
    "*.gpg", "*.asc", "shadow", "*.ovpn", "wallet.dat", "*.sqlite3-wal", "Login Data", "Cookies",
]


def is_sensitive(path: Path) -> bool:
    parts = path.parts
    if any(p in SENSITIVE_DIR_NAMES for p in parts):
        return True
    for seq in SENSITIVE_PATH_PARTS:
        n = len(seq)
        for i in range(len(parts) - n + 1):
            if tuple(p.lower() for p in parts[i:i + n]) == tuple(s.lower() for s in seq):
                return True
    name = path.name
    return any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_FILE_PATTERNS)


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


def validate_new_root(root: Path) -> Path:
    """Validate a directory proposed as a collection root. Returns the resolved path."""
    if not str(root).strip():
        raise PermissionError("No folder was given.")
    real = _real(root.expanduser())
    if not real.exists():
        raise PermissionError(f"Folder does not exist: {real}")
    if not real.is_dir():
        raise PermissionError(f"Not a folder: {real}")
    if real.parent == real:
        raise PermissionError(f"Refusing to index an entire filesystem ({real}). Choose a specific folder.")
    home = _real(Path.home())
    if real == home:
        raise PermissionError("Refusing to index your whole home folder. Choose a specific sub-folder such as "
                              "Documents/Notes.")
    if is_sensitive(real):
        raise PermissionError(f"{real} looks like a credential or private-key store and cannot be indexed.")
    return real


class PathPolicy:
    def __init__(self, roots: list[Path]) -> None:
        self.roots = [_real(r.expanduser()) for r in roots if str(r).strip()]

    def _within(self, path: Path) -> Path | None:
        for root in self.roots:
            if path == root or root in path.parents:
                return root
        return None

    def check_root(self, root: Path) -> Path:
        """A collection root must lie inside an authorised root on this computer."""
        if not self.roots:
            raise PermissionError("No folders are authorised on this computer (worker.allowed_roots is empty).")
        real = validate_new_root(root)
        if self._within(real) is None:
            raise PermissionError(f"{real} is outside the folders authorised on this computer: "
                                  + ", ".join(str(r) for r in self.roots))
        return real

    def resolve(self, path: Path, *, must_exist: bool = True) -> Path:
        """Resolve a user/model-supplied path and make sure it stays inside an authorised root."""
        candidate = path.expanduser()
        if not candidate.is_absolute():
            if len(self.roots) != 1:
                raise PermissionError("Relative paths are ambiguous; give an absolute path inside an authorised folder.")
            candidate = self.roots[0] / candidate
        real = _real(candidate)
        if self._within(real) is None:
            raise PermissionError(f"Access to {real} is not allowed (outside authorised folders).")
        if is_sensitive(real):
            raise PermissionError(f"Access to {real} is blocked: it looks like a credential or secret file.")
        if must_exist and not real.exists():
            raise FileNotFoundError(f"{real} does not exist.")
        return real
