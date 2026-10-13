"""Remote updater protocol: additive hello/heartbeat fields that can never invalidate a message, strict job
payloads, capped result/report models and the error-code table."""

import json
import re

import pytest
from pydantic import ValidationError, create_model

from homemesh.common.config import ALL_TASK_TYPES
from homemesh.protocol import messages as pm
from homemesh.protocol import update as pu
from homemesh.protocol.hardware import CPUInfo, HardwareReport, MemoryInfo, OSInfo
from homemesh.protocol.jobs import IDEMPOTENT_JOB_TYPES, PAYLOAD_SCHEMAS, RESULT_SCHEMAS, validate_payload

SHA1 = "a" * 40
SHA256 = "0123456789abcdef" * 4
RUN_ID = "u20261013120000-0123abcd"


def _hello(**kw):
    return pm.Hello(node_id="n1", node_name="pc", software_version="0.3.0", protocol_versions=[1],
                    hardware=HardwareReport(os=OSInfo(system="Linux"), cpu=CPUInfo(), memory=MemoryInfo()),
                    backends=[], models=[], task_types=["chat"], mode="balanced", limits=pm.NodeLimits(), **kw)


def _build(**kw):
    base = {"version": "0.3.0", "commit": SHA1, "source": "installer", "os": "linux", "can_update": True,
            "update_allowed": True, "source_dir": "/src/MyAi", "logs_dir": "/data/logs"}
    return pu.BuildInfo(**{**base, **kw})


def _report(**kw):
    base = {"v": 1, "run_id": RUN_ID, "job_id": "job_1", "origin": "remote", "phase": "building", "status": None,
            "started_at": "2026-10-13T12:00:00.000Z", "updated_at": "2026-10-13T12:01:00.000Z"}
    return {**base, **kw}


# --------------------------------------------------------------------------- hello / heartbeat

def test_hello_without_build_from_an_older_worker():
    raw = json.loads(pm.dump(_hello()))
    raw.pop("build"), raw.pop("update")
    h = pm.parse_worker_message(json.dumps(raw))
    assert isinstance(h, pm.Hello) and h.build is None and h.update is None


def test_hello_with_build_is_accepted_by_an_older_coordinator():
    old_fields = {k: (f.annotation, f) for k, f in pm.Hello.model_fields.items() if k not in ("build", "update")}
    OldHello = create_model("OldHello", __base__=pm._Msg, **old_fields)
    h = _hello(build=_build().to_wire(), update=pu.UpdateRunReport.model_validate(_report()).to_wire())
    old = OldHello.model_validate_json(pm.dump(h))
    assert old.node_id == "n1" and not hasattr(old, "build")
    new = pm.parse_worker_message(pm.dump(h))
    assert pu.parse_build(new.build) == _build()
    assert pu.parse_report(new.update).phase == "building"


@pytest.mark.parametrize("bad", [
    [1, 2], "build", 7, True,
    {"version": "x" * (pu.MAX_BUILD_BYTES + 1)},
    {"nested": {"deep": ["é" * 2100]}},  # 4.2 KB as UTF-8
])
def test_bad_build_becomes_none_and_the_hello_stays_valid(bad):
    raw = json.loads(pm.dump(_hello()))
    raw["build"] = bad
    h = pm.parse_worker_message(json.dumps(raw))
    assert isinstance(h, pm.Hello) and h.build is None and h.node_id == "n1"


def test_malformed_but_small_build_is_kept_raw_and_parsed_leniently():
    raw = json.loads(pm.dump(_hello()))
    raw["build"] = {"version": 3, "commit": "not-a-sha", "source": "martian"}
    h = pm.parse_worker_message(json.dumps(raw))
    assert h.build == {"version": 3, "commit": "not-a-sha", "source": "martian"}
    assert pu.parse_build(h.build) is None  # the coordinator stores {"invalid": true}


def test_update_report_size_cap_on_hello_and_heartbeat():
    small = _report(log="/l/" + "x" * 200)
    big = _report(log="/l/" + "x" * pu.MAX_REPORT_BYTES)
    assert _hello(update=small).update == small
    assert _hello(update=big).update is None
    hb = pm.Heartbeat(seq=1, telemetry={}, mode="balanced", update=big)
    assert hb.update is None
    hb = pm.parse_worker_message(pm.dump(pm.Heartbeat(seq=1, telemetry={}, mode="balanced", update=small)))
    assert hb.update == small
    assert pm.Heartbeat(seq=1, telemetry={}, mode="balanced").update is None


def test_lenient_dict_never_raises():
    class Weird:
        pass

    for value in ({(1, 2): "tuple key"}, {"x": Weird()}, {"x": float("nan")}, None, object()):
        assert pu.lenient_dict(value, 1000) is None
    assert pu.lenient_dict({1: "int key"}, 1000) == {"1": "int key"}  # normalised to plain JSON
    deep: dict = {}
    cur = deep
    for _ in range(5000):
        cur["a"] = {}
        cur = cur["a"]
    assert pu.lenient_dict(deep, 10**9) in (None, deep)  # whatever json makes of it: no exception


def test_job_failed_carries_an_optional_hint():
    m = pm.parse_worker_message(pm.dump(pm.JobFailed(job_id="j", code="update_not_allowed", error="off",
                                                     hint="run it")))
    assert m.hint == "run it"
    assert pm.JobFailed(job_id="j", code="x", error="y").hint is None


# --------------------------------------------------------------------------- job payloads

def test_update_job_types_registered_but_not_idempotent_or_worker_task_types():
    assert pu.UPDATE_JOB_TYPES == {"update.check", "update.apply"}
    assert pu.UPDATE_JOB_TYPES <= set(PAYLOAD_SCHEMAS) and pu.UPDATE_JOB_TYPES <= set(RESULT_SCHEMAS)
    assert not pu.UPDATE_JOB_TYPES & IDEMPOTENT_JOB_TYPES
    assert not pu.UPDATE_JOB_TYPES & set(ALL_TASK_TYPES)


def test_check_payload_is_empty_and_strict():
    assert validate_payload("update.check", {}) == pu.UpdateCheckPayload()
    with pytest.raises(ValidationError):
        validate_payload("update.check", {"ref": "origin/main"})


@pytest.mark.parametrize("sha", [SHA1, SHA256])
def test_apply_payload_accepts_full_shas(sha):
    p = validate_payload("update.apply", {"expected_commit": sha})
    assert p.expected_commit == sha and p.force is False
    assert validate_payload("update.apply", {"expected_commit": sha, "force": True}).force is True


@pytest.mark.parametrize("payload", [
    {"expected_commit": "@{u}"},
    {"expected_commit": "origin/main"},
    {"expected_commit": "--output=x"},
    {"expected_commit": "a38a3fa"},  # 7-char sha
    {"expected_commit": "A" * 40},  # upper case
    {"expected_commit": "a" * 41},
    {"expected_commit": "a" * 40 + "\n"},
    {"expected_commit": "a" * 39 + "g"},
    {"expected_commit": int("1" * 40)},
    {"expected_commit": SHA1, "force": "true"},
    {"expected_commit": SHA1, "run_id": RUN_ID},  # extra key
    {"expected_commit": SHA1, "source_dir": "/tmp"},  # extra key
    {},
])
def test_apply_payload_rejects_anything_but_a_full_sha(payload):
    with pytest.raises(ValidationError):
        validate_payload("update.apply", payload)


def test_apply_result():
    assert pu.UpdateApplyResult(run_id=RUN_ID).state == "started"
    with pytest.raises(ValidationError):
        pu.UpdateApplyResult(run_id="../../etc")


# --------------------------------------------------------------------------- check result

def test_check_result_caps_sanitizes_and_normalises():
    nasty = "fix \x1b[2K\r\x07it" + "→" * 300
    res = pu.UpdateCheckResult.model_validate({
        "v": 1, "ok": True, "upstream_commit": SHA1, "behind": 60, "ahead": 0, "can_update": True,
        "branch": "main\x00", "commits": [{"sha": SHA1, "subject": nasty, "date": "2026-10-13T12:00:00+02:00"}] * 60,
        "problems": [{"code": "no_linger", "message": "m", "blocking": False}] * 30, "future_field": 1})
    assert len(res.commits) == pu.MAX_COMMITS and len(res.problems) == pu.MAX_PROBLEMS
    subject = res.commits[0].subject
    assert len(subject) == pu.SUBJECT_MAX and "\x1b" not in subject and "\r" not in subject and "\x07" not in subject
    assert subject.startswith("fix [2Kit→") and res.branch == "main"
    assert res.can_update is True
    blocked = res.model_copy(update={"problems": [pu.UpdateProblem(code="update_low_disk", blocking=True)]})
    assert pu.UpdateCheckResult.model_validate(blocked.model_dump()).can_update is False
    zero = pu.UpdateCheckResult(ok=True, upstream_commit=SHA1, behind=0, can_update=True)
    assert zero.can_update is False


def test_check_result_error_shape():
    res = pu.UpdateCheckResult.model_validate(
        {"v": 1, "ok": False, "error": {"code": "update_not_allowed", "message": "off", "hint": "allow"},
         "can_update": True})
    assert res.error.code == "update_not_allowed" and res.can_update is False
    with pytest.raises(ValidationError):
        pu.UpdateCheckResult.model_validate({"v": 1, "ok": False})
    with pytest.raises(ValidationError):
        pu.UpdateCheckResult.model_validate({"v": 1, "ok": True})  # no upstream_commit / behind
    with pytest.raises(ValidationError):
        pu.UpdateCheckResult.model_validate({"ok": True, "upstream_commit": "HEAD", "behind": 1})


# --------------------------------------------------------------------------- run report

def test_run_report_validation_and_states():
    r = pu.UpdateRunReport.model_validate({**_report(phase="stopping"), "committed": True, "runner_pid": 9})
    assert r.restarting and not r.final
    r = pu.UpdateRunReport.model_validate(_report(phase="cleanup", status="ok", to_commit=SHA1))
    assert r.final and not r.restarting
    for bad in (_report(run_id="u1"), _report(phase="dancing"), _report(status="great"), _report(origin="hub"),
                _report(to_commit="main")):
        assert pu.parse_report(bad) is None
    assert pu.parse_report("nope") is None and pu.parse_report(None) is None


def test_run_report_wire_form_always_fits():
    long = "é" * 2000
    warnings = [{"code": "w" * 100, "message": long, "hint": long} for _ in range(15)]
    r = pu.UpdateRunReport.model_validate(_report(
        status="rolled_back", error={"code": "update_new_version_crashed", "message": long, "hint": long},
        warnings=warnings, log="/" + "日" * 400))
    assert len(r.warnings) == pu.MAX_WARNINGS and len(r.error.code) <= pu.CODE_MAX
    wire = r.to_wire()
    assert pu.json_size(wire) <= pu.MAX_REPORT_BYTES
    assert wire["status"] == "rolled_back" and wire["error"]["code"] == "update_new_version_crashed"
    assert _hello(update=wire).update == wire


def test_build_wire_form_always_fits():
    huge = "日" * 600
    b = _build(reason=huge, cannot_update_reason=huge, branch=huge, source_dir="/" + huge, commit_date=huge)
    wire = b.to_wire()
    assert pu.json_size(wire) <= pu.MAX_BUILD_BYTES and wire["commit"] == SHA1
    assert _hello(build=wire).build == wire


# --------------------------------------------------------------------------- error table

SECTION_17_CODES = """update_not_allowed update_not_supported update_invalid_request update_busy update_rate_limited
update_rolled_back_commit update_no_base_python update_launch_failed update_not_git_checkout update_git_missing
update_detached_head update_no_upstream update_bad_ref_name update_dirty_checkout update_git_locked
update_unsafe_repository update_auth_failed update_fetch_failed update_upstream_changed update_diverged
update_low_disk update_needs_installer update_node_missing update_web_build_failed update_venv_failed
update_install_failed update_config_incompatible update_smoke_failed update_jobs_running update_stop_failed
update_switch_failed update_db_snapshot_failed update_start_timeout update_new_version_crashed
update_new_version_rejected update_not_confirmed update_db_restore_failed update_rollback_failed
update_interrupted update_check_required update_still_reporting update_not_cancellable""".split()
WARNING_CODES = ("remote_url_has_credentials no_linger installed_commit_unknown checkout_not_advanced not_confirmed "
                 "web_ui_missing not_started").split()


def test_every_code_has_text_and_formats_without_fields():
    assert set(SECTION_17_CODES) <= pu.UPDATE_ERROR_CODES
    assert set(WARNING_CODES) == pu.UPDATE_WARNING_CODES
    for code in [*pu.UPDATE_ERRORS, *pu.UPDATE_WARNINGS]:
        issue = pu.format_update_error(code)
        assert issue.code == code and issue.message and issue.hint
        assert "{" not in issue.message + issue.hint, code
        assert len(code) <= pu.CODE_MAX


def test_format_fills_fields_and_never_crashes():
    i = pu.format_update_error("update_not_allowed", computer="Gaming-PC")
    assert i.message == "Remote updates are off on Gaming-PC."
    assert i.hint == "On Gaming-PC, run `homemesh update allow` (takes effect immediately)."
    i = pu.format_update_error("update_interrupted", phase="building", commit="a38a3fa")
    assert "while building the new version" in i.message and "this computer now runs a38a3fa" in i.hint
    i = pu.format_update_error("update_low_disk", free="1.2 GB")
    assert i.message == "Not enough free disk space (1.2 GB free, <need> needed)."
    i = pu.format_update_error("update_busy", variant="mesh", name="Hub")
    assert i.message == "Another computer (Hub) is updating."
    i = pu.format_update_error("update_fetch_failed", remote="origin", detail="{0} {x.y} {}", computer=None)
    assert "{0} {x.y} {}" in i.message and "this computer" in i.hint
    i = pu.format_update_error("something_new")
    assert i.code == "something_new" and "something_new" in i.message
    assert pu.format_update_error("update_busy", phase=object()).code == "update_busy"


def test_text_hygiene():
    assert pu.redact("fatal: https://bob:s3cret@github.com/x.git and ghp_AbC123_x github_pat_11AB_cd") == \
        "fatal: https://***@github.com/x.git and *** ***"
    assert pu.clean_line("a\tb\nc\x9bd e", 100) == "abcd e"
    assert pu.clean_text("a\tb\nc\r\x1b", 100) == "a\tb\nc"
    assert pu.is_sha(SHA1) and pu.is_sha(SHA256) and not pu.is_sha(SHA1 + "\n")
    assert pu.is_run_id(RUN_ID) and not pu.is_run_id("u2026101312000\uff10-0123abcd")  # fullwidth digit
    assert pu.is_venv_id("0123456789ab-20261013120000") and pu.is_venv_id("legacy-20261013120000")
    assert not pu.is_venv_id("../x-20261013120000")
    assert pu.is_ref_name("feature/x-1.2") and not pu.is_ref_name("--upload-pack=x") and not pu.is_ref_name("a b")
    assert pu.short_sha(SHA1) == "aaaaaaa" and pu.short_sha(None) is None
    for pattern, ok in ((pu.SHA_PATTERN, SHA1), (pu.RUN_ID_PATTERN, RUN_ID),
                        (pu.VENV_ID_PATTERN, "legacy-20261013120000"), (pu.REF_NAME_PATTERN, "main")):
        assert re.match(pattern, ok)


def test_installer_commands():
    assert pu.installer_command("linux", "/home/me/My Ai") == "'/home/me/My Ai/deploy/linux/install.sh' --upgrade"
    assert pu.installer_command("linux", "/src", pull=True) == \
        "cd /src && git pull --ff-only && ./deploy/linux/install.sh --upgrade"
    win = pu.installer_command("windows", r"C:\Users\me\MyAi")
    assert win == r'cd "C:\Users\me\MyAi"; powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -Upgrade'
    assert "git pull --ff-only;" in pu.installer_command("windows", None, pull=True)
    assert "<HomeMesh source folder>" in pu.installer_command("other", None)


def test_vocabularies():
    assert pu.PHASES[0] == "accepted" and pu.PHASES[-1] == "cleanup" and set(pu.PHASE_LABELS) == set(pu.PHASES)
    assert pu.RESTART_PHASES <= set(pu.PHASES) and "building" not in pu.RESTART_PHASES
    assert pu.FINAL_STATUSES == {"ok", "ok_unconfirmed", "already_up_to_date", "rolled_back", "failed",
                                 "interrupted"}
    assert pu.FINAL_STATUSES | {"requested", "running", "not_started", "abandoned"} == pu.ROW_STATES
