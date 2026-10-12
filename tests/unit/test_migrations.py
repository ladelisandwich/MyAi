import sqlite3
from contextlib import closing

import pytest

from homemesh.coordinator.db import migrate as mig


def _conn(path=":memory:"):
    c = sqlite3.connect(str(path), isolation_level=None)
    c.execute("PRAGMA foreign_keys=ON")
    return c


def test_fresh_migrate_and_idempotent():
    c = _conn()
    assert mig.migrate(c) == list(range(1, mig.LATEST_SCHEMA_VERSION + 1))
    assert mig.migrate(c) == []
    assert mig.current_version(c) == mig.LATEST_SCHEMA_VERSION
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("users", "nodes", "jobs", "messages", "memories", "chunks", "embeddings", "audit_events", "tool_approvals"):
        assert t in tables


def test_refuses_newer_schema():
    c = _conn()
    mig.migrate(c)
    c.execute("INSERT INTO schema_migrations VALUES (999, 'future', 'x', 'now')")
    with pytest.raises(mig.MigrationError) as exc:
        mig.migrate(c)
    assert "newer" in exc.value.message and exc.value.hint


def test_pre_migration_backup_and_atomic_failure(tmp_path, monkeypatch):
    db = tmp_path / "hm.sqlite3"
    c = _conn(db)
    mig.migrate(c)
    c.execute("INSERT INTO users(id, username, password_hash, created_at, updated_at) VALUES ('u','o','h','t','t')")
    real = mig.load_migrations()
    good = mig.Migration(len(real) + 1, "add_column", "ALTER TABLE users ADD COLUMN nickname TEXT;")
    monkeypatch.setattr(mig, "load_migrations", lambda: [*real, good])
    assert mig.migrate(c, tmp_path / "backups") == [good.version]
    backups = list((tmp_path / "backups").glob("pre-migration-*.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0], isolation_level=None)) as b:
        assert b.execute("SELECT username FROM users").fetchone()[0] == "o"
    bad = mig.Migration(good.version + 1, "broken", "CREATE TABLE ok_table(x INT);\nTHIS IS NOT SQL;")
    monkeypatch.setattr(mig, "load_migrations", lambda: [*real, good, bad])
    with pytest.raises(mig.MigrationError, match="No changes were applied"):
        mig.migrate(c, tmp_path / "backups")
    assert mig.current_version(c) == good.version
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='ok_table'").fetchone()  # rolled back


def test_split_sql_keeps_triggers_intact():
    stmts = mig.split_sql("CREATE TABLE a(x);\n-- comment\nCREATE TRIGGER t AFTER INSERT ON a BEGIN\n"
                          "  INSERT INTO a VALUES (1);\n  INSERT INTO a VALUES (2);\nEND;\nCREATE INDEX i ON a(x);")
    assert len(stmts) == 3 and stmts[1].startswith("CREATE TRIGGER") and stmts[1].endswith("END")
