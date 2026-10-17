"""The web UI mirrors parts of ``protocol.update`` in TypeScript (``apps/web/src/lib/update.ts``): phase words and
labels, the restart phases, the sha/run-id patterns and ``installer_command()``.

Both sides are checked against one fixture, ``apps/web/src/__tests__/fixtures/update-mirror.json``: this test
holds Python to it, ``apps/web/src/__tests__/update-lib.test.ts`` holds TypeScript to it. A change on one side
fails here until the fixture (and so the other side) is updated too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from homemesh.protocol import update as u

FIXTURE = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "__tests__" / "fixtures" / "update-mirror.json"


@pytest.fixture(scope="module")
def mirror() -> dict:
    if not FIXTURE.is_file():  # an unpacked wheel/sdist without the web sources
        pytest.skip("web sources not present")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_vocabularies_match(mirror: dict) -> None:
    assert mirror["phases"] == list(u.PHASES)
    assert mirror["phase_labels"] == u.PHASE_LABELS
    assert set(mirror["restart_phases"]) == set(u.RESTART_PHASES)
    assert set(mirror["final_statuses"]) == set(u.FINAL_STATUSES)
    assert set(mirror["success_statuses"]) == set(u.SUCCESS_STATUSES)
    assert set(mirror["active_row_states"]) == set(u.ACTIVE_ROW_STATES)
    assert mirror["no_word_after_s"] == u.NO_WORD_AFTER_S
    assert mirror["dismiss_after_s"] == u.DISMISS_AFTER_S
    assert mirror["path_max"] == u.PATH_MAX
    assert mirror["sha_pattern"] == u.SHA_PATTERN
    assert mirror["run_id_pattern"] == u.RUN_ID_PATTERN


def test_installer_commands_match(mirror: dict) -> None:
    assert len(mirror["installer_commands"]) >= 10
    for case in mirror["installer_commands"]:
        assert u.installer_command(case["os"], case["source_dir"], pull=case["pull"]) == case["expected"], case


def test_sha_and_run_id_checks_match(mirror: dict) -> None:
    for case in mirror["shas"]:
        assert u.is_sha(case["value"]) is case["is_sha"], case
        assert u.short_sha(case["value"]) == case["short"], case
    for case in mirror["run_ids"]:
        assert u.is_run_id(case["value"]) is case["is_run_id"], case

