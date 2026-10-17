"""``homemesh update …``: update HomeMesh on this computer from its own git checkout (design section 15).

* ``homemesh update [--yes]`` checks the branch the checkout tracks, shows the new commits, asks, then starts
  the update helper **detached** and returns: the helper builds the new version next to the running one,
  restarts HomeMesh with it and undoes the switch if the new version doesn't work. Never waits in-process
  (HomeMesh, and with it maybe this terminal's PATH entry, is replaced underneath).
* ``homemesh update check``: the same check, without installing.
* ``homemesh update allow`` / ``disallow``: consent to updates started from the hub (a marker file, read
  live; no restart). A local ``homemesh update`` never needs it.
* ``homemesh update status [--json]``: the latest update and the consent, from files only. Works while
  HomeMesh is stopped and never contacts the coordinator.
* ``homemesh update record-install`` (hidden): run by the installers with the **new** venv's python, writes
  that venv's install record.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

from homemesh.common.paths import AppPaths
from homemesh.protocol.update import (
    ACCEPT_TIMEOUT_S,
    CHECK_TIMEOUT_S,
    PHASE_LABELS,
    BuildInfo,
    UpdateCheckResult,
    UpdateIssue,
    UpdateRunReport,
    clean_line,
    format_update_error,
    installer_command,
    is_run_id,
    short_sha,
)
from homemesh.update import consent, files, launch, record

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
MAX_SHOWN_COMMITS = 20
CHECK_BUSY_PHASE = "checking for updates"


# --------------------------------------------------------------------------- shared helpers

def _phase_words(phase: Any) -> str:
    return PHASE_LABELS.get(phase, phase) if isinstance(phase, str) and phase else "working"


def in_progress_line(paths: AppPaths) -> str | None:
    """"An update is in progress (run <id>, <phase>; log <path>)" while an update runner holds the update
    lock and its run has no final status; else None. Never raises."""
    try:
        u = files.update_in_progress(paths)
    except Exception:
        return None
    if not u:
        return None
    return f"An update is in progress (run {u['run_id']}, {_phase_words(u.get('phase'))}; log {u.get('log') or '?'})"


def _print_issue(issue: UpdateIssue, *, symbol: str = "✗", indent: str = "") -> None:
    print(f"{indent}{symbol} {issue.message}")
    if issue.hint:
        print(f"{indent}  → {issue.hint}")


def _computer(ctx: Any) -> str:
    try:
        return str(ctx.cfg.node.name)
    except Exception:
        return "this computer"


def _date(iso: str | None) -> str:
    return iso[:10] if iso else "?"


def _when(iso: str | None) -> str:
    return iso[:19].replace("T", " ") + " UTC" if iso else "?"


# --------------------------------------------------------------------------- update / update check

def _not_supported(ctx: Any, info: BuildInfo) -> int:
    _print_issue(format_update_error(
        "update_not_supported", computer=_computer(ctx), reason=info.cannot_update_reason or info.reason,
        installer_command=installer_command(info.os, info.source_dir, pull=True)))
    return EXIT_ERROR


def _check(ctx: Any) -> UpdateCheckResult | int:
    """Run the check runner as a child of this process and return its result, or an exit code."""
    paths: AppPaths = ctx.paths
    info = record.build_info(paths)
    if not info.can_update:
        return _not_supported(ctx, info)
    if files.update_lock_held(paths):
        u = files.update_in_progress(paths)
        _print_issue(format_update_error("update_busy", computer=_computer(ctx),
                                         phase=(u or {}).get("phase") or CHECK_BUSY_PHASE))
        return EXIT_ERROR
    print(f"Checking for updates of {info.source_dir} (this can take a minute)…", flush=True)
    handle = launch.start_check(paths, ctx.cfg, origin="local", foreground=True)
    try:
        result = launch.wait_for_check_sync(handle, timeout_s=CHECK_TIMEOUT_S)
    except BaseException:  # failed, timed out or Ctrl+C: stop the runner and everything it started
        launch.kill_check(handle)
        raise
    finally:
        with contextlib.suppress(Exception):
            files.prune_checks(paths)
    print_check_result(result, info)
    return result


def print_check_result(result: UpdateCheckResult, info: BuildInfo | None = None) -> None:
    if not result.ok:
        _print_issue(result.error or format_update_error("update_launch_failed"))
        return
    installed = short_sha(result.installed_commit or (info.commit if info else None)) or "unknown"
    print(f"Installed: {installed}" + (f" (branch {result.branch})" if result.branch else ""))
    upstream = result.upstream_ref or "upstream"
    print(f"Newest:    {short_sha(result.upstream_commit)} on {upstream} ({_date(result.upstream_date)})")
    if result.behind:
        print(f"{result.behind} new change{'s' if result.behind != 1 else ''}:")
        for c in result.commits[:MAX_SHOWN_COMMITS]:
            print(f"  {short_sha(c.sha)}  {c.subject}")
        more = result.behind - min(len(result.commits), MAX_SHOWN_COMMITS)
        if more > 0:
            print(f"  … and {more} more")
        if result.migrations:
            print("The database will be upgraded (a copy is kept until the new version works).")
    for p in result.problems:
        _print_issue(p, symbol="✗" if p.blocking else "!")


def cmd_update(ctx: Any, *, apply: bool) -> int:
    """``homemesh update`` (``apply=True``) and ``homemesh update check``."""
    from homemesh.cli import service

    outcome = _check(ctx)
    if isinstance(outcome, int):
        return outcome
    result = outcome
    if not result.ok:
        return EXIT_ERROR
    if not result.behind:
        print(f"✓ Up to date ({short_sha(result.upstream_commit)}).")
        return EXIT_OK if not any(p.blocking for p in result.problems) else EXIT_ERROR
    if not result.can_update:
        print("✗ This update can't be installed until the problems above are fixed. Nothing was changed.")
        return EXIT_ERROR
    if not apply:
        print("Install it with: homemesh update")
        return EXIT_OK
    target = result.upstream_commit
    if target is None:  # can't happen: a successful check always names the upstream commit
        return EXIT_ERROR
    running = bool(service.read_pid(ctx.paths))
    restart = ("HomeMesh on this computer restarts; running jobs finish first (up to 2 minutes)." if running
               else "HomeMesh is not running here and stays stopped.")
    if not ctx.args.yes:
        try:
            answer = input(f"Install {short_sha(target)} now? {restart} [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing was changed.")
            return EXIT_USAGE
    run_id = files.new_run_id()
    handle = launch.start_apply(ctx.paths, ctx.cfg, run_id=run_id, expected_commit=target, force=False,
                                origin="local")
    launch.wait_for_accept_sync(handle, timeout_s=ACCEPT_TIMEOUT_S)
    print(f"Updating in the background. Follow it with: homemesh update status (log: {handle.log_file})")
    return EXIT_OK


# --------------------------------------------------------------------------- allow / disallow

def cmd_allow(ctx: Any) -> int:
    path = consent.allow(ctx.paths)
    print(f"✓ Remote updates are allowed on {_computer(ctx)} (takes effect now; no restart needed).")
    print("  The hub's web app can now update HomeMesh here to the newest commit of the branch this computer's")
    print("  checkout already tracks, from that checkout's own git remote. It can't choose other code.")
    print(f"  Turn it off with 'homemesh update disallow' (marker: {path}).")
    return EXIT_OK


def cmd_disallow(ctx: Any) -> int:
    existed = consent.disallow(ctx.paths)
    if existed:
        print(f"✓ Remote updates are off on {_computer(ctx)} (takes effect now). 'homemesh update' still works here.")
    else:
        print("Remote updates were already off on this computer.")
    return EXIT_OK


# --------------------------------------------------------------------------- status

def _latest_check(paths: AppPaths) -> UpdateCheckResult | None:
    try:
        names = sorted(n[:-5] for n in os.listdir(files.checks_dir(paths))
                       if n.endswith(".json") and not n.endswith(".args.json") and is_run_id(n[:-5]))
    except OSError:
        return None
    for run_id in reversed(names):
        data = files.read_check_result(paths, run_id)
        if data is None:
            continue
        with contextlib.suppress(Exception):
            return UpdateCheckResult.model_validate(data)
    return None


def _outcome_words(r: UpdateRunReport, helper_running: bool) -> str:
    status = r.status
    if status is None:
        if helper_running:
            return f"in progress: {_phase_words(r.phase)}"
        return (f"the update helper stopped while {_phase_words(r.phase)} without finishing; the run is marked "
                "as interrupted the next time HomeMesh runs")
    to, frm = short_sha(r.to_commit or r.expected_commit), short_sha(r.from_commit)
    if status == "ok":
        return f"updated {frm or '?'} → {to or '?'}"
    if status == "ok_unconfirmed":
        return f"updated to {to or '?'} (the hub couldn't confirm it at the time)"
    if status == "already_up_to_date":
        return "already up to date"
    if status == "rolled_back":
        return f"failed and was undone; still on {frm or 'the previous version'}"
    if status == "interrupted":
        now = short_sha(r.installed_commit)
        return f"interrupted while {_phase_words(r.phase)}" + (f"; now running {now}" if now else "")
    return "failed"


def cmd_status(ctx: Any) -> int:
    paths: AppPaths = ctx.paths
    info = record.build_info(paths)
    allowed = consent.remote_allowed(paths)
    latest = files.read_latest_run(paths)
    helper_running = files.update_lock_held(paths)
    report = files.report_from_run(latest[1]) if latest is not None else None
    check = _latest_check(paths)
    if ctx.args.json or getattr(ctx.args, "status_json", False):
        print(json.dumps({
            "build": info.model_dump(mode="json"),
            "version_line": record.format_version_line(info),
            "remote_updates_allowed": allowed,
            "helper_running": helper_running,
            "in_progress": bool(report is not None and report.status is None and helper_running),
            "latest_run": report.model_dump(mode="json") if report is not None else None,
            "latest_run_id": latest[0] if latest is not None else None,
            "last_check": check.model_dump(mode="json") if check is not None else None,
        }, indent=2))
        return EXIT_OK
    print(record.format_version_line(info))
    print("Remote updates: " + ("allowed (turn off: homemesh update disallow)" if allowed
                                else "off (turn on: homemesh update allow)"))
    if not info.can_update:
        print(f"Updating this computer: not possible: {info.cannot_update_reason or info.reason or 'unknown reason'}")
        print(f"  → {installer_command(info.os, info.source_dir, pull=True)}")
    if latest is None:
        print("Latest update: none yet.")
    elif report is None:
        print(f"Latest update: run {latest[0]} (its run file could not be read)")
        print(f"  Log: {files.apply_log_file(paths, latest[0])}")
    else:
        origin = "from the hub" if report.origin == "remote" else "started here"
        print(f"Latest update: run {report.run_id} ({origin}), started {_when(report.started_at)}")
        words = _outcome_words(report, helper_running)
        print(f"  {words[:1].upper()}{words[1:]}" + (f" (finished {_when(report.finished_at)})"
                                                    if report.finished_at else ""))
        if report.error is not None:
            _print_issue(report.error, indent="  ")
        for w in report.warnings:
            _print_issue(w, symbol="!", indent="  ")
        print(f"  Log: {report.log or files.apply_log_file(paths, report.run_id)}")
    if check is not None:
        when = _when(check.checked_at)
        if not check.ok:
            err = check.error.message if check.error else "failed"
            print(f"Last check ({when}): {err}")
        elif check.behind:
            print(f"Last check ({when}): {check.behind} new change{'s' if check.behind != 1 else ''} "
                  f"({short_sha(check.upstream_commit)} on {check.upstream_ref or 'upstream'})")
        else:
            print(f"Last check ({when}): up to date")
    elif helper_running and (report is None or report.status is not None):
        print(f"A check for updates is running ({CHECK_BUSY_PHASE}).")
    return EXIT_OK


# --------------------------------------------------------------------------- record-install (installers)

def cmd_record_install(ctx: Any) -> int:
    """Write ``<sys.prefix>/homemesh-install.json`` for the venv this python belongs to."""
    a = ctx.args
    paths: AppPaths = ctx.paths
    line = in_progress_line(paths)
    if line:
        print(f"✗ {line}.\n  Wait for it to finish ('homemesh update status'), then run the installer again.")
        return EXIT_ERROR
    source = Path(os.path.abspath(a.source))
    git = a.git or None
    if git is not None and not os.path.isfile(git):
        print(f"! git was given as {git}, which is not a file; not recorded.")
        git = None
    node_dir = a.node_dir or None
    if node_dir is not None and not os.path.isdir(node_dir):
        print(f"! The Node.js folder {node_dir} does not exist; not recorded.")
        node_dir = None
    is_checkout = (source / ".git").exists()
    facts = record.checkout_facts(source, git) if is_checkout else {}
    rec = record.make_record(stable_venv=a.stable_venv, built_by=a.built_by,
                             source_dir=source if is_checkout else None, git=git, node_dir=node_dir, **facts)
    path = record.write(rec)
    if rec.commit:
        what = f"commit {short_sha(rec.commit)}" + (f", branch {rec.branch}" if rec.branch else "") + (
            ", with local changes" if rec.dirty else "")
    elif is_checkout:
        what = "commit unknown: git could not read the checkout" if git else "commit unknown: git was not found"
    else:
        what = "not a git checkout: updates from the web app are not possible for this install"
    print(f"Recorded this install ({clean_line(what, 200)}) in {path}")
    return EXIT_OK


# --------------------------------------------------------------------------- entry point

def run(ctx: Any) -> int:
    action = getattr(ctx.args, "update_action", None)
    if action == "allow":
        return cmd_allow(ctx)
    if action == "disallow":
        return cmd_disallow(ctx)
    if action == "status":
        return cmd_status(ctx)
    if action == "record-install":
        return cmd_record_install(ctx)
    if action == "check":
        return cmd_update(ctx, apply=False)
    return cmd_update(ctx, apply=True)
