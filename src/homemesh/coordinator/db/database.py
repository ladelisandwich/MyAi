"""SQLite access layer.

All statements run on one dedicated thread that owns the connection. Async callers await
results without blocking the event loop, and writes are naturally serialised, which is
exactly what SQLite wants. WAL mode lets backups and the CLI read concurrently.

Repositories (``homemesh.coordinator.db.repositories``) hold all SQL; services never
build SQL themselves. That boundary is the migration path to PostgreSQL.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

Row = dict[str, Any]


def _connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> Row | None:
    return dict(row) if row is not None else None


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="homemesh-db")
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    def open_sync(self) -> None:
        if self._conn is None:
            if isinstance(self.path, Path):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = self._executor.submit(_connect, self.path).result()

    async def open(self) -> None:
        if self._conn is None:
            if isinstance(self.path, Path):
                self.path.parent.mkdir(parents=True, exist_ok=True)
            loop = asyncio.get_running_loop()
            self._conn = await loop.run_in_executor(self._executor, _connect, self.path)

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._executor, conn.close)
        self._executor.shutdown(wait=True)

    def close_sync(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            self._executor.submit(conn.close).result()
        self._executor.shutdown(wait=True)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    # ------------------------------------------------------------------ core execution
    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn(conn)`` on the database thread."""
        loop = asyncio.get_running_loop()
        conn = self.conn
        return await loop.run_in_executor(self._executor, fn, conn)

    def run_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return self._executor.submit(fn, self.conn).result()

    async def transaction(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``fn`` inside BEGIN IMMEDIATE ... COMMIT (rolled back on any exception)."""

        def _tx(conn: sqlite3.Connection) -> T:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

        return await self.run(_tx)

    def transaction_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _tx(conn: sqlite3.Connection) -> T:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result

        return self.run_sync(_tx)

    async def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        """Execute a write; returns rowcount."""
        return await self.run(lambda c: c.execute(sql, params).rowcount)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> int:
        items = list(seq)
        return await self.run(lambda c: c.executemany(sql, items).rowcount)

    async def fetchone(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Row | None:
        return await self.run(lambda c: row_to_dict(c.execute(sql, params).fetchone()))

    async def fetchall(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[Row]:
        return await self.run(lambda c: [dict(r) for r in c.execute(sql, params).fetchall()])

    async def fetchval(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        def _q(c: sqlite3.Connection) -> Any:
            r = c.execute(sql, params).fetchone()
            return r[0] if r is not None else None

        return await self.run(_q)

    # ------------------------------------------------------------------ maintenance
    async def integrity_check(self) -> str:
        return await self.fetchval("PRAGMA quick_check")

    async def backup_to(self, dest: Path) -> None:
        """Consistent online snapshot using SQLite's backup API (safe while serving)."""

        def _bk(conn: sqlite3.Connection) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(str(dest))
            try:
                conn.backup(target)
            finally:
                target.close()

        await self.run(_bk)

    def backup_to_sync(self, dest: Path) -> None:
        def _bk(conn: sqlite3.Connection) -> None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            target = sqlite3.connect(str(dest))
            try:
                conn.backup(target)
            finally:
                target.close()

        self.run_sync(_bk)
