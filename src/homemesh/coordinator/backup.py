"""Backup and restore of irreplaceable personal data.

Archive (``homemesh-backup-<timestamp>.hmbackup``, a gzip'd tar; ``.hmbackup.enc`` when
encrypted) contains::

    manifest.json            format version, app/schema version, sha256 of every file
    db/homemesh.sqlite3      consistent snapshot via SQLite's online backup API
    config/homemesh.toml     machine configuration
    tls/*                    local CA + server certificate (so workers' pins stay valid)
    secrets/worker-credentials.json   built-in worker identity
    attachments/*            uploaded images

Model files are deliberately excluded - they are large and can be downloaded again; the
model registry (which model was where) is inside the database.

Encryption (optional, strongly recommended because the archive contains private keys and
personal data): AES-256-GCM over 1 MiB chunks, key derived from the passphrase with scrypt.
Each chunk's nonce includes its index and the final chunk is marked in the authenticated
data, so reordering or truncation is detected.

Restore never deletes anything: the current data is moved aside to
``<data>-pre-restore-<timestamp>`` before the backup is unpacked, and it refuses to run while
the coordinator is running.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import struct
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from homemesh.common.errors import HomeMeshError
from homemesh.common.paths import AppPaths
from homemesh.common.timeutil import now_iso
from homemesh.version import __version__

FORMAT_VERSION = 1
MAGIC = b"HMBK\x01"
CHUNK = 1024 * 1024
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


class BackupError(HomeMeshError):
    code = "backup_error"


@dataclass
class BackupInfo:
    path: Path
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(passphrase.encode("utf-8"))


def encrypt_file(src: Path, dest: Path, passphrase: str) -> None:
    salt = os.urandom(16)
    prefix = os.urandom(8)
    header = MAGIC + salt + struct.pack(">III", SCRYPT_N, SCRYPT_R, SCRYPT_P) + prefix
    aead = AESGCM(_derive_key(passphrase, salt))
    size = src.stat().st_size
    with src.open("rb") as fin, dest.open("wb") as fout:
        fout.write(header)
        index = 0
        remaining = size
        while True:
            data = fin.read(CHUNK)
            remaining -= len(data)
            final = remaining <= 0
            nonce = prefix + struct.pack(">I", index)
            aad = header + struct.pack(">I?", index, final)
            ct = aead.encrypt(nonce, data, aad)
            fout.write(struct.pack(">I", len(ct)) + ct)
            index += 1
            if final:
                break


def decrypt_file(src: Path, dest: Path, passphrase: str) -> None:
    with src.open("rb") as fin, dest.open("wb") as fout:
        magic = fin.read(len(MAGIC))
        if magic != MAGIC:
            raise BackupError("Not an encrypted HomeMesh backup (bad header).")
        salt = fin.read(16)
        n, r, p = struct.unpack(">III", fin.read(12))
        prefix = fin.read(8)
        header = MAGIC + salt + struct.pack(">III", n, r, p) + prefix
        if n > 2**20 or r > 16 or p > 4:
            raise BackupError("Backup uses unsupported key-derivation parameters.")
        aead = AESGCM(Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase.encode("utf-8")))
        index = 0
        while True:
            raw_len = fin.read(4)
            if not raw_len:
                raise BackupError("Encrypted backup is truncated (final chunk missing).")
            (length,) = struct.unpack(">I", raw_len)
            ct = fin.read(length)
            nonce = prefix + struct.pack(">I", index)
            peek = fin.read(1)
            final = peek == b""
            if not final:
                fin.seek(-1, io.SEEK_CUR)
            try:
                fout.write(aead.decrypt(nonce, ct, header + struct.pack(">I?", index, final)))
            except InvalidTag as exc:
                raise BackupError("Wrong passphrase or the backup file is corrupted/tampered with.",
                                  code="backup_decrypt_failed") from exc
            index += 1
            if final:
                break


def _schema_version(db_file: Path) -> int:
    conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def snapshot_database(db_file: Path, dest: Path) -> None:
    """Consistent copy of a live WAL database using the SQLite backup API."""
    src = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def create_backup(paths: AppPaths, dest_dir: Path | None = None, *, passphrase: str | None = None,
                  include_secrets: bool = True, include_attachments: bool = True) -> BackupInfo:
    if not paths.db_file.exists():
        raise BackupError(f"No database found at {paths.db_file}; nothing to back up.")
    dest_dir = dest_dir or paths.backups_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "").replace(".", "")[:15]
    name = f"homemesh-backup-{stamp}.hmbackup"
    with tempfile.TemporaryDirectory(prefix="hm-backup-") as tmp:
        stage = Path(tmp) / "stage"
        (stage / "db").mkdir(parents=True)
        snapshot_database(paths.db_file, stage / "db" / "homemesh.sqlite3")
        includes = ["database"]
        if paths.config_file.exists():
            (stage / "config").mkdir()
            shutil.copy2(paths.config_file, stage / "config" / "homemesh.toml")
            includes.append("config")
        if include_secrets:
            if paths.tls_dir.exists() and any(paths.tls_dir.iterdir()):
                shutil.copytree(paths.tls_dir, stage / "tls")
                includes.append("tls")
            if paths.worker_credentials_file.exists():
                (stage / "secrets").mkdir()
                shutil.copy2(paths.worker_credentials_file, stage / "secrets" / "worker-credentials.json")
                includes.append("worker-credentials")
        if include_attachments and paths.attachments_dir.exists() and any(paths.attachments_dir.iterdir()):
            shutil.copytree(paths.attachments_dir, stage / "attachments")
            includes.append("attachments")
        files = {p.relative_to(stage).as_posix(): _sha256(p) for p in sorted(stage.rglob("*")) if p.is_file()}
        manifest = {"format": FORMAT_VERSION, "app_version": __version__,
                    "schema_version": _schema_version(stage / "db" / "homemesh.sqlite3"), "created_at": now_iso(),
                    "hostname": socket.gethostname(), "includes": includes, "encrypted": bool(passphrase),
                    "files": files}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tar_path = Path(tmp) / name
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(stage / "manifest.json", arcname="manifest.json")
            for rel in files:
                tar.add(stage / rel, arcname=rel)
        if passphrase:
            final = dest_dir / (name + ".enc")
            encrypt_file(tar_path, final, passphrase)
        else:
            final = dest_dir / name
            shutil.move(str(tar_path), final)
    if os.name == "posix":
        os.chmod(final, 0o600)
    return BackupInfo(final, manifest)


def list_backups(directory: Path) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    out = []
    for p in sorted(directory.glob("homemesh-backup-*.hmbackup*"), reverse=True):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "encrypted": p.name.endswith(".enc"),
                    "created_at": _mtime_iso(st.st_mtime)})
    return out


def _mtime_iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _open_archive(path: Path, workdir: Path, passphrase: str | None) -> Path:
    if path.name.endswith(".enc") or path.read_bytes()[:5] == MAGIC:
        if not passphrase:
            raise BackupError("This backup is encrypted; a passphrase is required.", code="passphrase_required")
        plain = workdir / "backup.tar.gz"
        decrypt_file(path, plain, passphrase)
        return plain
    return path


def verify_backup(path: Path, passphrase: str | None = None) -> dict[str, Any]:
    """Unpack into a temporary directory and check every file against the manifest."""
    with tempfile.TemporaryDirectory(prefix="hm-verify-") as tmp:
        work = Path(tmp)
        extracted = _extract(path, work, passphrase)
        return json.loads((extracted / "manifest.json").read_text(encoding="utf-8"))


def _extract(path: Path, work: Path, passphrase: str | None) -> Path:
    archive = _open_archive(path, work, passphrase)
    out = work / "extract"
    out.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(out, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"Backup archive is unreadable: {exc}") from exc
    manifest_path = out / "manifest.json"
    if not manifest_path.exists():
        raise BackupError("Backup has no manifest.json; it was not created by HomeMesh.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT_VERSION:
        raise BackupError(f"Unsupported backup format {manifest.get('format')}.")
    for rel, digest in manifest.get("files", {}).items():
        f = out / rel
        if not f.is_file() or _sha256(f) != digest:
            raise BackupError(f"Backup file {rel} is missing or its checksum does not match (corrupted backup).",
                              code="backup_corrupt")
    # closing(): a sqlite3 connection used as a context manager only commits, it does not close.
    # An open handle would keep the file locked on Windows (cleanup/restore would then fail).
    with contextlib.closing(sqlite3.connect(str(out / "db" / "homemesh.sqlite3"))) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("The database inside the backup failed its integrity check.", code="backup_corrupt")
    return out


def restore_backup(path: Path, paths: AppPaths, *, passphrase: str | None = None) -> dict[str, Any]:
    from homemesh.coordinator.db.migrate import LATEST_SCHEMA_VERSION

    with tempfile.TemporaryDirectory(prefix="hm-restore-") as tmp:
        out = _extract(path, Path(tmp), passphrase)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        if int(manifest.get("schema_version", 0)) > LATEST_SCHEMA_VERSION:
            raise BackupError(
                f"This backup was made by a newer HomeMesh (schema v{manifest['schema_version']}); this version "
                f"supports up to v{LATEST_SCHEMA_VERSION}.", hint="Upgrade HomeMesh before restoring.")
        stamp = now_iso().replace(":", "").replace("-", "").replace(".", "")[:15]
        aside = paths.data_dir.parent / f"{paths.data_dir.name}-pre-restore-{stamp}"
        aside.mkdir(parents=True, exist_ok=True)
        moved = []
        for item in (paths.db_file, paths.db_file.with_name(paths.db_file.name + "-wal"),
                     paths.db_file.with_name(paths.db_file.name + "-shm"), paths.tls_dir, paths.attachments_dir,
                     paths.worker_credentials_file):
            if item.exists():
                target = aside / item.relative_to(paths.data_dir)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(target))
                moved.append(str(target))
        if paths.config_file.exists() and (out / "config" / "homemesh.toml").exists():
            shutil.copy2(paths.config_file, aside / "homemesh.toml")
            moved.append(str(aside / "homemesh.toml"))
        paths.ensure()
        shutil.copy2(out / "db" / "homemesh.sqlite3", paths.db_file)
        if (out / "tls").exists():
            shutil.rmtree(paths.tls_dir, ignore_errors=True)
            shutil.copytree(out / "tls", paths.tls_dir)
        if (out / "secrets" / "worker-credentials.json").exists():
            shutil.copy2(out / "secrets" / "worker-credentials.json", paths.worker_credentials_file)
        if (out / "attachments").exists():
            shutil.rmtree(paths.attachments_dir, ignore_errors=True)
            shutil.copytree(out / "attachments", paths.attachments_dir)
        if (out / "config" / "homemesh.toml").exists():
            shutil.copy2(out / "config" / "homemesh.toml", paths.config_file)
        if os.name == "posix":
            for d in (paths.secrets_dir, paths.tls_dir):
                if d.exists():
                    os.chmod(d, 0o700)
        # The CLI admin token is regenerated on next start.
        if paths.cli_token_file.exists():
            paths.cli_token_file.unlink()
        return {"manifest": manifest, "previous_data_moved_to": str(aside), "moved": moved}
