"""HomeMesh update runner: all git, npm, pip and venv work of an update, outside the HomeMesh process.

INTERFACE STUB: ``main()`` and the constants are fixed; ``load_args``, ``run_check`` and ``run_apply`` are
filled in by the runner implementer (design sections 9-11).

Invocation (``update/launch.py``)::

    <base python> -I -S -X utf8 runner.py {check|apply} --args <args.json>

Rules for this file:

* **Standard library only, one file.** It runs with ``-I -S`` from the base interpreter, so it can't import
  ``homemesh`` or a sibling module (``tests/unit/test_update_runner_stdlib.py`` enforces it). Whatever it
  shares with HomeMesh is duplicated here and kept in agreement by ``tests/unit/test_update_contracts.py``:
  the ``args.json`` fields and rules (``homemesh.update.files.RunnerArgs``), the file layout and atomic
  writes of ``homemesh.update.files``, ``run.json`` / ``latest.json`` / ``checks/<run_id>.json``
  (``homemesh.protocol.update.UpdateRunReport`` / ``UpdateCheckResult``), the install record
  (``homemesh.update.record.InstallRecord``), the patterns and the redactor/sanitizers of
  ``homemesh.protocol.update`` (``SHA_PATTERN``, ``RUN_ID_PATTERN``, ``VENV_ID_PATTERN``,
  ``REF_NAME_PATTERN``, ``redact``, ``clean_line``, ``clean_text``), the error codes and message/hint texts
  of ``UPDATE_ERRORS`` / ``UPDATE_WARNINGS``, and the lock primitive of ``common.instance.InstanceLock``.
* It is the **old** version's code that runs; the process it starts is the new version. Every file it
  shares with HomeMesh only grows additively (``"v": 1``, unknown fields ignored).
* It asserts ``sys.prefix == sys.base_prefix`` (else exit :data:`EXIT_NO_BASE_PYTHON`) and refuses to run
  inside ``homemesh.service``'s cgroup (exit :data:`EXIT_INSIDE_UNIT`).
* Captured command output goes to ``runs/<run_id>/cmd-<n>.out`` (names pruning recognises), never pipes.

Exit codes are the ``EXIT_*`` constants below; ``launch.wait_for_accept`` / ``wait_for_check`` map them to
error codes when the runner exits without a result file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0  # finished; the outcome (any final status, or a check result) is in the result file
EXIT_FAILED = 1  # unexpected crash after the result file existed; the file says what happened
EXIT_BAD_ARGS = 2  # command line or args.json invalid: nothing was written
EXIT_BUSY = 3  # the update lock is held by another runner (update_busy): nothing was written
EXIT_INSIDE_UNIT = 4  # started inside homemesh.service's cgroup (update_launch_failed): nothing was written
EXIT_NO_BASE_PYTHON = 5  # not the base interpreter (update_no_base_python): nothing was written

MODES = ("check", "apply")


class ArgsError(Exception):
    """``args.json`` failed validation; the message names the field."""


def load_args(path: Path, mode: str) -> dict[str, Any]:
    """Read and validate ``args.json`` field by field (``homemesh.update.files.RunnerArgs`` rules); its
    ``mode`` must equal the command-line ``mode``. Raises :class:`ArgsError`."""
    raise NotImplementedError


def run_check(args: dict[str, Any]) -> int:
    """Check mode (design section 10): write ``args["out"]``; return an ``EXIT_*`` code."""
    raise NotImplementedError


def run_apply(args: dict[str, Any]) -> int:
    """Apply mode (design section 11): phases accepted → … → cleanup with run.json written before each
    action; return an ``EXIT_*`` code."""
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homemesh-update-runner",
                                     description="HomeMesh update runner (started by HomeMesh; not for direct use)")
    parser.add_argument("mode", choices=MODES)
    parser.add_argument("--args", required=True, dest="args_file", metavar="ARGS_JSON",
                        help="absolute path of the args.json written by the launcher")
    ns = parser.parse_args(argv)  # exits with EXIT_BAD_ARGS (2) on a bad command line
    try:
        args = load_args(Path(ns.args_file), ns.mode)
    except ArgsError as exc:
        print(f"homemesh update runner: invalid {ns.args_file}: {exc}", file=sys.stderr)
        return EXIT_BAD_ARGS
    if ns.mode == "check":
        return run_check(args)
    return run_apply(args)


if __name__ == "__main__":
    sys.exit(main())
