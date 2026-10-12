"""Explicit, versioned schema migrations.

Migrations are numbered SQL files in ``migrations/`` (``0001_initial.sql``, ...). Each runs
in its own transaction and is recorded in ``schema_migrations`` with a checksum.

Safety rules:

* Before applying migrations to a database that already holds data, a consistent snapshot
  is written to ``<data>/backups/pre-migration-v<from>-to-v<to>-<timestamp>.sqlite3``.
* A database whose schema is *newer* than this software (downgrade) is refused rather
  than modified - install the newer HomeMesh version or restore a backup.
* An applied migration whose file content changed is reported (checksum mismatch) so that
  accidental edits to released migrations are caught.

Recovery procedure: see TROUBLESHOOTING.md ("Database migration failed").
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from homemesh.common.errors import HomeMeshError
from homemesh.common.logging import get_logger
from homemesh.common.timeutil import now_iso

log = get_logger(__name__)
_NAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


class MigrationError(HomeMeshError):
    code = "migration_error"


def load_migrations() -> list[Migration]:
    pkg = resources.files("homemesh.coordinator.db") / "migrations"
    out = []
    for entry in pkg.iterdir():
        m = _NAME_RE.match(entry.name)
        if m:
            out.append(Migration(int(m.group(1)), m.group(2), entry.read_text(encoding="utf-8")))
    out.sort(key=lambda m: m.version)
    for i, m in enumerate(out, start=1):
        if m.version != i:
            raise MigrationError(f"migration numbering gap: expected {i:04d}, found {m.version:04d}")
    return out


LATEST_SCHEMA_VERSION = len(load_migrations())


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def current_version(conn: sqlite3.Connection) -> int:
    _ensure_table(conn)
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def _has_user_data(conn: sqlite3.Connection) -> bool:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("users", "conversations", "memories"):
        if t in tables and conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():
            return True
    return False


def migrate(conn: sqlite3.Connection, backups_dir: Path | None = None) -> list[int]:
    """Apply pending migrations. Returns the list of applied versions."""
    migrations = load_migrations()
    latest = migrations[-1].version if migrations else 0
    current = current_version(conn)
    if current > latest:
        raise MigrationError(
            f"The database schema (v{current}) is newer than this HomeMesh version supports (v{latest}).",
            hint="Upgrade HomeMesh to the version that created this database, or restore a backup made "
                 "with this version ('homemesh restore').",
        )
    applied = {r[0]: r[1] for r in conn.execute("SELECT version, checksum FROM schema_migrations")}
    for m in migrations:
        if m.version in applied and applied[m.version] != m.checksum:
            log.warning("Migration %04d_%s was modified after being applied (checksum mismatch).",
                        m.version, m.name)
    pending = [m for m in migrations if m.version > current]
    if not pending:
        return []
    if backups_dir is not None and current > 0 and _has_user_data(conn):
        stamp = now_iso().replace(":", "").replace("-", "").replace(".", "")
        dest = backups_dir / f"pre-migration-v{current}-to-v{latest}-{stamp}.sqlite3"
        dest.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(dest))
        try:
            conn.backup(target)
        finally:
            target.close()
        log.info("Database backed up to %s before migrating v%d -> v%d", dest, current, latest)
    done = []
    for m in pending:
        log.info("Applying database migration %04d_%s", m.version, m.name)
        try:
            conn.execute("BEGIN IMMEDIATE")
            # executescript would COMMIT implicitly; run statements one by one inside our transaction.
            for stmt in split_sql(m.sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, name, checksum, applied_at) VALUES (?,?,?,?)",
                         (m.version, m.name, m.checksum, now_iso()))
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise MigrationError(
                f"Database migration {m.version:04d}_{m.name} failed: {exc}. No changes were applied by it.",
                hint="See TROUBLESHOOTING.md 'Database migration failed'. A pre-migration backup is in the "
                     "backups directory.",
            ) from exc
        done.append(m.version)
    return done


def split_sql(script: str) -> list[str]:
    """Split a migration script into statements, keeping CREATE TRIGGER ... END; blocks intact."""
    statements: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for raw in script.splitlines():
        line = raw.strip()
        if not buf and (not line or line.startswith("--")):
            continue
        if line.startswith("--"):
            continue
        buf.append(raw)
        upper = line.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if upper.startswith("END;") or upper == "END;":
                statements.append("\n".join(buf))
                buf, in_trigger = [], False
        elif line.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf and "".join(buf).strip():
        statements.append("\n".join(buf))
    return [s.strip().rstrip(";") for s in statements if s.strip()]
