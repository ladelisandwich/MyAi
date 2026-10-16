"""node_updates storage: one active update in the whole mesh, report-driven states, late reports win over the
coordinator's guesses."""

import asyncio
import contextlib
import sqlite3

import pytest

from homemesh.coordinator.db import migrate as migrations
from homemesh.coordinator.db.database import Database
from homemesh.coordinator.db.migrate import migrate
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.db.repositories.updates import ActiveUpdateExists

SHA = "d" * 40
RUN_ID = "u20261013120000-0123abcd"


@pytest.fixture
async def repos(tmp_path):
    db = Database(tmp_path / "hm.sqlite3")
    await db.open()
    await db.run(lambda conn: migrate(conn))
    yield Repositories.create(db)
    await db.close()


async def _create(repos, update_id, node_id, **kw):
    return await repos.updates.create_active(update_id=update_id, node_id=node_id, expected_commit=SHA,
                                             from_commit="e" * 40, force=False, requested_by="usr_1", **kw)


async def test_one_active_update_in_the_mesh_under_concurrent_creates(repos):
    a = await repos.nodes.create("hub")
    b = await repos.nodes.create("gaming-pc")
    results = await asyncio.gather(_create(repos, "job_a", a["id"]), _create(repos, "job_b", b["id"]),
                                   return_exceptions=True)
    created = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, ActiveUpdateExists)]
    assert len(created) == 1 and len(refused) == 1
    assert refused[0].existing["id"] == created[0]["id"]
    assert await repos.db.fetchval("SELECT COUNT(*) FROM node_updates") == 1
    row = created[0]
    assert row["state"] == "requested" and row["active"] is True and row["force"] is False
    assert row["report_json"] is None and row["requested_by"] == "usr_1"
    assert (await repos.updates.get_active())["id"] == row["id"]


async def test_unique_index_is_the_backstop(repos):
    n = await repos.nodes.create("hub")
    await _create(repos, "job_a", n["id"])
    with pytest.raises(sqlite3.IntegrityError):
        await repos.db.execute(
            "INSERT INTO node_updates(id, node_id, requested_at, expected_commit, state, active)"
            " VALUES ('job_raw', ?, 'now', ?, 'requested', 1)", (n["id"], SHA))
    with pytest.raises(ActiveUpdateExists):
        await _create(repos, "job_b", n["id"])
    # finished rows don't count: many inactive rows are fine
    assert await repos.updates.finish("job_a", state="ok")
    await _create(repos, "job_c", n["id"])
    assert (await repos.updates.get_active())["id"] == "job_c"


async def test_lifecycle_from_reports(repos):
    n = await repos.nodes.create("pc")
    await _create(repos, "job_a", n["id"])
    assert await repos.updates.set_running("job_a", RUN_ID)
    row = await repos.updates.get("job_a")
    assert row["state"] == "running" and row["run_id"] == RUN_ID
    report = {"run_id": RUN_ID, "job_id": "job_a", "phase": "stopping", "status": None}
    assert await repos.updates.update_from_report("job_a", phase="stopping", report=report, run_id=RUN_ID)
    row = await repos.updates.active_for_node(n["id"])
    assert row["phase"] == "stopping" and row["report_json"] == report and row["last_report_at"]
    final = {**report, "phase": "cleanup", "status": "ok"}
    assert await repos.updates.finish("job_a", state="ok", report=final, phase="cleanup")
    row = await repos.updates.get("job_a")
    assert row["state"] == "ok" and row["active"] is False and row["finished_at"] and row["phase"] == "cleanup"
    assert await repos.updates.get_active() is None and await repos.updates.active_for_node(n["id"]) is None
    # hello repeats the last report on every reconnect: a real outcome is never rewritten
    assert not await repos.updates.finish("job_a", state="failed", error_code="update_x")
    assert not await repos.updates.update_from_report("job_a", phase="building", report=report)
    assert not await repos.updates.set_running("job_a", "u20261013120001-0123abcd")
    assert (await repos.updates.get("job_a"))["state"] == "ok"


async def test_report_before_job_completion_moves_requested_to_running(repos):
    n = await repos.nodes.create("pc")
    await _create(repos, "job_a", n["id"])
    assert await repos.updates.update_from_report("job_a", phase="prechecks", report={"x": 1}, run_id=RUN_ID)
    row = await repos.updates.get("job_a")
    assert row["state"] == "running" and row["run_id"] == RUN_ID
    assert await repos.updates.set_running("job_a", "u20261013999999-ffffffff")
    assert (await repos.updates.get("job_a"))["run_id"] == RUN_ID  # first run id wins


async def test_refused_before_launch(repos):
    n = await repos.nodes.create("pc")
    await _create(repos, "job_a", n["id"])
    assert await repos.updates.finish("job_a", state="failed", error_code="update_not_allowed",
                                      error="Remote updates are off on pc.", hint="On pc, run ...")
    row = await repos.updates.get("job_a")
    assert (row["state"], row["error_code"], row["active"]) == ("failed", "update_not_allowed", False)


async def test_late_report_overwrites_abandoned_and_not_started(repos):
    n = await repos.nodes.create("pc")
    await _create(repos, "job_a", n["id"])
    assert await repos.updates.abandon("job_a", error="Outcome unknown.", hint="run status")
    row = await repos.updates.get("job_a")
    assert row["state"] == "abandoned" and row["error_code"] == "update_abandoned" and not row["active"]
    assert not await repos.updates.abandon("job_a", error="again", hint=None)
    await _create(repos, "job_b", n["id"])  # the mesh is free again
    # progress for the abandoned row while another update is active: ignored (one active update)
    assert not await repos.updates.update_from_report("job_a", phase="verifying", report={"p": 1})
    assert (await repos.updates.get("job_a"))["state"] == "abandoned"
    assert await repos.updates.finish("job_a", state="rolled_back", error_code="update_new_version_crashed",
                                      error="crashed", hint="undone", report={"status": "rolled_back"})
    row = await repos.updates.get("job_a")
    assert row["state"] == "rolled_back" and row["error_code"] == "update_new_version_crashed"
    assert (await repos.updates.get_active())["id"] == "job_b"

    assert await repos.updates.finish("job_b", state="not_started", error_code="update_not_started")
    assert not await repos.updates.finish("job_b", state="not_started")  # only ends an active row
    assert await repos.updates.finish("job_b", state="ok")  # the real outcome still wins
    assert (await repos.updates.get("job_b"))["state"] == "ok"
    with pytest.raises(ValueError):
        await repos.updates.finish("job_b", state="running")
    with pytest.raises(ValueError):
        await repos.updates.finish("job_b", state="abandoned")


async def test_progress_after_a_guess_makes_the_row_active_again(repos):
    # Dismissed after 15 min without word (the computer slept); then the runner reports again.
    n = await repos.nodes.create("pc")
    await _create(repos, "job_a", n["id"])
    assert await repos.updates.abandon("job_a", error="Outcome unknown.", hint="h")
    report = {"run_id": RUN_ID, "job_id": "job_a", "phase": "verifying", "status": None}
    assert await repos.updates.update_from_report("job_a", phase="verifying", report=report)
    row = await repos.updates.get("job_a")
    assert (row["state"], row["active"], row["phase"], row["run_id"]) == ("running", True, "verifying", RUN_ID)
    assert row["error_code"] is None and row["finished_at"] is None
    with pytest.raises(ActiveUpdateExists):  # it is the mesh's active update again
        await _create(repos, "job_b", n["id"])
    assert await repos.updates.finish("job_a", state="ok", report={**report, "status": "ok"}, phase="cleanup")
    assert (await repos.updates.get("job_a"))["state"] == "ok"


async def test_launch_timeout_is_a_guess_that_the_runner_can_correct(repos):
    # The worker gave up waiting for run.json (15 s, e.g. a slow first Python start) and sent
    # JobFailed(update_launch_failed), but the runner did start: its reports carry the row's job id.
    n = await repos.nodes.create("pc")
    await _create(repos, "job_a", n["id"])
    assert await repos.updates.finish("job_a", state="failed", error_code="update_launch_failed",
                                      error="The update helper could not be started: timeout.", hint="See log")
    row = await repos.updates.get("job_a")
    assert (row["state"], row["active"], row["run_id"]) == ("failed", False, None)
    report = {"run_id": RUN_ID, "job_id": "job_a", "phase": "building", "status": None}
    assert await repos.updates.update_from_report("job_a", phase="building", report=report)
    assert (await repos.updates.get("job_a"))["state"] == "running"
    final = {**report, "phase": "cleanup", "status": "ok"}
    assert await repos.updates.finish("job_a", state="ok", report=final, phase="cleanup")
    row = await repos.updates.get("job_a")
    assert (row["state"], row["run_id"], row["error_code"]) == ("ok", RUN_ID, None)

    # final report straight after the guess, no progress in between
    await _create(repos, "job_b", n["id"])
    assert await repos.updates.finish("job_b", state="failed", error_code="update_launch_failed")
    assert await repos.updates.finish("job_b", state="rolled_back", error_code="update_not_confirmed",
                                      report={**final, "job_id": "job_b", "status": "rolled_back"})
    row = await repos.updates.get("job_b")
    assert (row["state"], row["error_code"], row["run_id"]) == ("rolled_back", "update_not_confirmed", RUN_ID)
    # a real outcome is final, also for "failed" that came with a report
    assert not await repos.updates.finish("job_b", state="ok", report=final)


async def test_job_failure_after_the_runner_reported_is_ignored(repos):
    # The report arrived first; a JobFailed for the same job must not end a run that is going on.
    n = await repos.nodes.create("pc")
    await _create(repos, "job_a", n["id"])
    assert await repos.updates.update_from_report("job_a", phase="accepted", report={"run_id": RUN_ID})
    assert not await repos.updates.finish("job_a", state="failed", error_code="update_launch_failed")
    row = await repos.updates.get("job_a")
    assert (row["state"], row["active"], row["run_id"]) == ("running", True, RUN_ID)


async def test_migration_0004_upgrades_a_populated_0_2_0_database(tmp_path, monkeypatch):
    path = tmp_path / "hm.sqlite3"
    every = migrations.load_migrations()
    assert [m.version for m in every][-1] == 4 and every[3].name == "updates"
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        monkeypatch.setattr(migrations, "load_migrations", lambda: every[:3])  # what 0.2.0 shipped
        assert migrate(conn) == [1, 2, 3]
        monkeypatch.undo()
        t = "2026-10-13T12:00:00.000Z"
        conn.execute("INSERT INTO nodes(id, name, created_at, updated_at, connected, hardware_json)"
                     " VALUES ('node_1', 'pc', ?, ?, 1, '{\"cpu\": 8}')", (t, t))
        conn.execute("INSERT INTO users(id, username, password_hash, role, created_at, updated_at)"
                     " VALUES ('usr_1', 'me', 'h', 'owner', ?, ?)", (t, t))
        assert migrate(conn, tmp_path / "backups") == [4]
        assert [p.name.startswith("pre-migration-v3-to-v4-") for p in (tmp_path / "backups").iterdir()] == [True]
        assert conn.execute("SELECT name, hardware_json, build_json, update_check_json FROM nodes").fetchall() == \
            [("pc", '{"cpu": 8}', None, None)]
        indexes = {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='node_updates'")}
        assert "WHERE active IS NOT NULL" in indexes["node_updates_one_active"] and "node_updates_by_node" in indexes
        conn.execute("INSERT INTO node_updates(id, node_id, requested_at, expected_commit, state, active)"
                     " VALUES ('job_1', 'node_1', ?, ?, 'requested', 1)", (t, SHA))
        with pytest.raises(sqlite3.IntegrityError):  # foreign key
            conn.execute("INSERT INTO node_updates(id, node_id, requested_at, expected_commit, state)"
                         " VALUES ('job_2', 'node_x', ?, ?, 'failed')", (t, SHA))
        conn.execute("DELETE FROM nodes WHERE id='node_1'")
        assert conn.execute("SELECT COUNT(*) FROM node_updates").fetchone() == (0,)  # cascade
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert migrate(conn) == []


async def test_latest_per_node_and_history(repos):
    a = await repos.nodes.create("hub")
    b = await repos.nodes.create("pc")
    await _create(repos, "job_1", a["id"], requested_at="2026-10-13T10:00:00.000Z")
    await repos.updates.finish("job_1", state="ok")
    await _create(repos, "job_2", b["id"], requested_at="2026-10-13T11:00:00.000Z")
    await repos.updates.finish("job_2", state="failed")
    await _create(repos, "job_3", a["id"], requested_at="2026-10-13T12:00:00.000Z")
    assert (await repos.updates.latest_for_node(a["id"]))["id"] == "job_3"
    assert (await repos.updates.latest_for_node(b["id"]))["id"] == "job_2"
    latest = await repos.updates.latest_by_node()
    assert {k: v["id"] for k, v in latest.items()} == {a["id"]: "job_3", b["id"]: "job_2"}
    assert [r["id"] for r in await repos.updates.list_for_node(a["id"])] == ["job_3", "job_1"]
    assert await repos.updates.latest_for_node("node_none") is None
    await repos.nodes.delete(b["id"])  # cascade
    assert await repos.updates.get("job_2") is None


async def test_node_build_and_check_columns(repos):
    n = await repos.nodes.create("pc")
    row = await repos.nodes.get(n["id"])
    assert row["build_json"] is None and row["update_check_json"] is None
    build = {"v": 1, "version": "0.3.0", "commit": SHA, "can_update": True}
    check = {"job_id": "job_c", "checked_at": "2026-10-13T12:00:00.000Z", "result": {"ok": True, "behind": 2}}
    await repos.nodes.set_build(n["id"], build)
    await repos.nodes.set_update_check(n["id"], check)
    row = await repos.nodes.get(n["id"])
    assert row["build_json"] == build and row["update_check_json"] == check
    assert (await repos.nodes.list())[0]["build_json"] == build
    await repos.nodes.set_build(n["id"], None)
    assert (await repos.nodes.get(n["id"]))["build_json"] is None
