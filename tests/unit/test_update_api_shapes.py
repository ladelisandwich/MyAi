"""The web UI's TypeScript types for the remote updater (``apps/web/src/api/types.ts``) against what the hub
serves (interfaces section 8): the node summary's updater fields, the ``update`` row view, run reports, build
info and check results. A field the hub sends that the UI doesn't declare, or one the UI requires that the hub
never sends, fails here (``test_update_web_mirror.py`` does the same for the vocabularies and helpers).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from homemesh.coordinator.nodes import NodeManager, update_view
from homemesh.protocol.update import (
    BuildInfo,
    UpdateCheckResult,
    UpdateCommit,
    UpdateIssue,
    UpdateProblem,
    UpdateRunReport,
)

TYPES = Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "api" / "types.ts"
SHA = "a" * 40
RUN = "u20261016120000-0123abcd"


def ts_fields(name: str) -> dict[str, bool]:
    """``{field: optional}`` of ``export interface <name>`` (including the interface it extends)."""
    if not TYPES.is_file():
        pytest.skip("web sources not present")
    text = TYPES.read_text(encoding="utf-8")
    m = re.search(rf"^export interface {name}(?: extends (\w+))? \{{\n(.*?)^\}}", text, re.S | re.M)
    assert m, f"interface {name} not found in {TYPES}"
    fields = ts_fields(m.group(1)) if m.group(1) else {}
    for line in m.group(2).splitlines():
        f = re.match(r"\s+(\w+)(\?)?:", line)
        if f:
            fields[f.group(1)] = bool(f.group(2))
    return fields


def agree(name: str, served: set[str]) -> None:
    declared = ts_fields(name)
    assert served - set(declared) == set(), f"{name}: served but not declared in types.ts"
    required = {k for k, optional in declared.items() if not optional}
    assert required - served == set(), f"{name}: required in types.ts but never served"


def test_update_row_view():
    row = {"id": "job_1", "node_id": "node_1", "state": "running", "phase": "building", "run_id": RUN,
           "from_commit": SHA, "expected_commit": "b" * 40, "force": 0, "requested_at": "2026-10-16T12:00:00Z",
           "last_report_at": None, "finished_at": None, "error_code": None, "report_json": None}
    agree("NodeUpdate", set(update_view(row)))


def test_run_report_build_info_and_check_result():
    report = UpdateRunReport.model_validate({
        "run_id": RUN, "job_id": "job_1", "origin": "remote", "phase": "building", "started_at": "x",
        "updated_at": "x", "warnings": [{"code": "no_linger", "message": "m"}], "error": {"code": "c"}})
    agree("UpdateRunReport", set(report.to_wire()))
    build = BuildInfo(version="0.3.0", source="installer", os="linux", can_update=True, update_allowed=True)
    agree("BuildInfo", set(build.to_wire()))
    agree("UpdateCheckResult", set(UpdateCheckResult.model_fields))
    agree("UpdateIssue", set(UpdateIssue.model_fields))
    agree("UpdateProblem", set(UpdateProblem.model_fields))
    agree("UpdateCommit", set(UpdateCommit.model_fields))


def test_node_summary_updater_fields():
    row = {"id": "node_1", "build_json": None, "update_check_json": None}
    out = NodeManager.node_summary(SimpleNamespace(connections={}), row, update_row=None,  # type: ignore[arg-type]
                                   update_check_pending=None)
    updater = {"build", "update_check", "update_check_pending", "update", "update_live"}
    assert updater <= set(out)
    declared = ts_fields("NodeSummary")
    assert updater <= set(declared) and all(declared[k] for k in updater)  # optional: older hubs omit them
