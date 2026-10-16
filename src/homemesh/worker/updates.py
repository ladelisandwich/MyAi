"""Remote-update jobs on the worker (design section 7): ``update.check`` and ``update.apply``.

The worker is the security boundary of remote updates; everything the coordinator checks is UX. Before an
update job does anything it passes, in this order (each failure is a ``JobFailed`` with an ``update_*``
code, a plain-language message and a hint):

1. the payload validates against the strict models (``update_invalid_request``);
2. the consent marker exists **right now** (``update_not_allowed``; ``homemesh update allow``);
3. this install can be updated (``update_not_supported``: installer record, git checkout, Linux/Windows);
4. no runner holds the update lock (``update_busy``);
5. check: the last check started at least 30 s ago (``update_rate_limited``); apply: the commit was not
   just rolled back here (``update_rolled_back_commit``; ``homemesh update`` on the computer can retry it).

Update jobs are always accepted first (``JobAccepted``) and then end in ``JobCompleted`` or ``JobFailed``,
never ``JobRejected`` (whose code the coordinator records only as "rejected"). They run even when the
worker is paused or at capacity and don't count towards its capacity.

All git/pip/npm work happens in the runner (``update/runner.py``), a separate process started by
``update/launch.py``. This module only starts it, waits for its first word, and watches the run files:

* every 2 s it reads the latest run (``Hello.update`` / ``Heartbeat.update``), marks a run whose runner is
  gone as ``interrupted``, and while the run is draining rejects new jobs and writes ``drain.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from homemesh.common.config import HomeMeshConfig
from homemesh.common.logging import get_logger
from homemesh.common.paths import AppPaths
from homemesh.protocol import messages as pm
from homemesh.protocol.update import (
    ACCEPT_TIMEOUT_S,
    CHECK_RATE_LIMIT_S,
    CHECK_TIMEOUT_S,
    MESSAGE_MAX,
    RESTART_PHASES,
    WATCH_INTERVAL_S,
    BuildInfo,
    UpdateApplyPayload,
    UpdateApplyResult,
    UpdateCheckPayload,
    UpdateIssue,
    format_update_error,
    installer_command,
    short_sha,
)
from homemesh.update import consent, files, launch, record
from homemesh.update.files import AgentState, DrainJob

log = get_logger(__name__)

UPDATING_REASON = "this computer is about to restart for an update"
CHECK_BUSY_PHASE = "checking for updates"  # update_busy while a check runner holds the update lock

_M = TypeVar("_M", bound=BaseModel)


class UpdateRefused(Exception):
    """An update job ends with ``JobFailed(issue.code, issue.message, issue.hint)``."""

    def __init__(self, issue: UpdateIssue) -> None:
        super().__init__(issue.message)
        self.issue = issue


class UpdateManager:
    """Update jobs, run reports and draining for one :class:`~homemesh.worker.agent.WorkerAgent`.

    ``jobs`` and ``job_types`` are the agent's own maps (job id → task / job type): update jobs live in
    them too, so hello, heartbeat and cancel work unchanged; their ids are also in :attr:`job_ids`, so
    capacity checks and draining can leave them out.
    """

    def __init__(self, cfg: HomeMeshConfig, paths: AppPaths, *, send: Callable[[Any], Awaitable[None]],
                 jobs: dict[str, asyncio.Task[None]], job_types: dict[str, str]) -> None:
        self.cfg = cfg
        self.paths = paths
        self.send = send
        self.jobs = jobs
        self.job_types = job_types
        self.job_ids: set[str] = set()
        self.draining = False  # the latest run is draining or restarting this computer: reject new jobs
        self.report: dict[str, Any] | None = None  # latest run report (wire form) for hello / heartbeat
        self.build_commit: str | None = None  # build_info().commit of this process (agent.json)
        self.agent_state: AgentState | None = None  # last state written to agent.json by this process
        self._last_check_launch: float | None = None  # monotonic time the last check runner was started
        self._watch_failed = False

    # ------------------------------------------------------------------ helpers for the agent
    @property
    def computer(self) -> str:
        return self.cfg.node.name

    def running_count(self) -> int:
        """Running jobs that count towards capacity (update jobs excluded)."""
        return sum(1 for job_id in self.jobs if job_id not in self.job_ids)

    def other_jobs(self) -> list[DrainJob]:
        """Running jobs other than update jobs, for ``drain.json``."""
        return [DrainJob(job_id=job_id, type=self.job_types.get(job_id, "unknown"))
                for job_id in list(self.jobs) if job_id not in self.job_ids]

    async def build_info(self) -> BuildInfo:
        """What this process runs (``Hello.build``); remembers the commit for ``agent.json``."""
        info = await asyncio.to_thread(record.build_info, self.paths)
        self.build_commit = info.commit
        return info

    async def set_agent_state(self, state: AgentState, detail: str = "") -> None:
        """Write ``agent.json`` (design 7.5): the runner's proof that a new version connects. Best effort.

        ``connecting`` is written once per process (before its first attempt); afterwards the file always
        holds the outcome of the latest attempt (``connected``, ``unreachable`` or ``rejected``)."""
        self.agent_state = state
        try:
            await asyncio.to_thread(files.write_agent_file, self.paths, state=state,
                                    build_commit=self.build_commit, detail=detail)
        except Exception as exc:  # never let bookkeeping break the connection loop
            log.debug("Could not write %s: %s", files.agent_file(self.paths), exc)

    # ------------------------------------------------------------------ dispatch
    async def on_dispatch(self, msg: pm.JobDispatch) -> None:
        """Accept an ``update.*`` job (also when paused or at capacity) and run it in the background."""
        if msg.job_id in self.jobs:
            return  # duplicate dispatch; already running
        await self.send(pm.JobAccepted(job_id=msg.job_id))
        self.job_ids.add(msg.job_id)
        self.job_types[msg.job_id] = msg.job_type
        self.jobs[msg.job_id] = asyncio.create_task(self._run(msg), name=f"update-job-{msg.job_id}")

    async def _run(self, msg: pm.JobDispatch) -> None:
        job_id = msg.job_id
        reply: Any
        try:
            if msg.job_type == "update.check":
                result = await self._check(msg)
            elif msg.job_type == "update.apply":
                result = await self._apply(msg)
            else:
                raise UpdateRefused(format_update_error("update_invalid_request", computer=self.computer))
            reply = pm.JobCompleted(job_id=job_id, result=result)
        except UpdateRefused as exc:
            i = exc.issue
            log.info("Update job %s (%s) refused: [%s] %s", job_id, msg.job_type, i.code, i.message)
            reply = pm.JobFailed(job_id=job_id, code=i.code, error=i.message, hint=i.hint)
        except asyncio.CancelledError:
            reply = pm.JobCancelled(job_id=job_id)
        except Exception as exc:  # a bug here must still end the job with a clear, persisted state
            log.exception("Update job %s (%s) failed", job_id, msg.job_type)
            i = format_update_error("update_launch_failed", computer=self.computer,
                                    detail=f"{type(exc).__name__}: {exc}")
            reply = pm.JobFailed(job_id=job_id, code=i.code, error=i.message, hint=i.hint)
        finally:
            self.jobs.pop(job_id, None)
            self.job_ids.discard(job_id)
            self.job_types.pop(job_id, None)
        with contextlib.suppress(Exception):
            await self.send(reply)

    # ------------------------------------------------------------------ gates
    def _validate(self, model: type[_M], payload: dict[str, Any]) -> _M:
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            log.warning("Invalid update job payload: %s", exc.errors()[:2])
            raise UpdateRefused(format_update_error("update_invalid_request", computer=self.computer)) from None

    def _gate(self) -> BuildInfo:
        """Consent, updatability and the update lock, checked live for every job (runs in a thread)."""
        if not consent.remote_allowed(self.paths):
            raise UpdateRefused(format_update_error("update_not_allowed", computer=self.computer))
        info = record.build_info(self.paths)
        if not info.can_update:
            raise UpdateRefused(format_update_error(
                "update_not_supported", computer=self.computer, reason=info.cannot_update_reason or info.reason,
                installer_command=installer_command(info.os, info.source_dir, pull=True)))
        if files.update_lock_held(self.paths):
            raise UpdateRefused(format_update_error("update_busy", computer=self.computer, phase=self._busy_phase()))
        return info

    def _busy_phase(self) -> str:
        latest = files.read_latest_run(self.paths)
        if latest is not None and latest[1].get("status") is None and isinstance(latest[1].get("phase"), str):
            return latest[1]["phase"]
        return CHECK_BUSY_PHASE

    def _launch_issue(self, exc: launch.UpdateLaunchError) -> UpdateIssue:
        """``UpdateLaunchError`` → issue; the redacted log tail goes into the message where it fits."""
        code = exc.code if isinstance(exc.code, str) and exc.code.startswith("update_") else "update_launch_failed"
        message = exc.message
        tail = str((exc.details or {}).get("log_tail") or "").strip()
        if tail:
            log.warning("Update helper failed [%s]: %s\n%s", code, message, tail)
            room = MESSAGE_MAX - len(message) - 1
            if tail not in message and room >= 80:
                message = f"{message}\n{tail[-room:]}"
        return UpdateIssue(code=code, message=message, hint=exc.hint)

    async def _kill_check(self, handle: launch.RunnerHandle) -> None:
        """Stop a check runner tree; finishes even if this task is cancelled again meanwhile."""
        kill = asyncio.ensure_future(asyncio.to_thread(launch.kill_check, handle))
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(kill)

    # ------------------------------------------------------------------ update.check (7.2)
    async def _check(self, msg: pm.JobDispatch) -> dict[str, Any]:
        self._validate(UpdateCheckPayload, msg.payload)
        await asyncio.to_thread(self._gate)
        now = time.monotonic()
        if self._last_check_launch is not None and now - self._last_check_launch < CHECK_RATE_LIMIT_S:
            raise UpdateRefused(format_update_error("update_rate_limited", computer=self.computer))
        self._last_check_launch = now
        try:
            try:
                handle = await asyncio.to_thread(launch.start_check, self.paths, self.cfg, origin="remote",
                                                 job_id=msg.job_id)
            except launch.UpdateLaunchError as exc:
                raise UpdateRefused(self._launch_issue(exc)) from None
            try:
                result = await launch.wait_for_check(handle, timeout_s=CHECK_TIMEOUT_S)
            except launch.UpdateLaunchError as exc:
                await self._kill_check(handle)
                raise UpdateRefused(self._launch_issue(exc)) from None
            except BaseException:  # cancelled (job cancel, disconnect, worker stopping): stop the runner
                await self._kill_check(handle)
                raise
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(files.prune_checks, self.paths)
        # The marker can change during a 5-minute check; report what is true now.
        result = result.model_copy(update={"allowed": await asyncio.to_thread(consent.remote_allowed, self.paths)})
        if not result.ok:
            err = result.error or format_update_error("update_launch_failed", computer=self.computer)
            raise UpdateRefused(UpdateIssue(code=err.code, message=err.message, hint=err.hint))
        return result.model_dump(mode="json")

    # ------------------------------------------------------------------ update.apply (7.3)
    async def _apply(self, msg: pm.JobDispatch) -> dict[str, Any]:
        payload = self._validate(UpdateApplyPayload, msg.payload)
        await asyncio.to_thread(self._gate)
        latest = await asyncio.to_thread(files.read_latest_run, self.paths)
        if (latest is not None and latest[1].get("status") == "rolled_back"
                and latest[1].get("to_commit") == payload.expected_commit):
            # Stops a break/undo loop driven from the hub; the owner can retry deliberately on the computer.
            raise UpdateRefused(format_update_error("update_rolled_back_commit", computer=self.computer,
                                                    short=short_sha(payload.expected_commit)))
        run_id = files.new_run_id()  # generated here, never taken from the hub
        try:
            handle = await asyncio.to_thread(
                launch.start_apply, self.paths, self.cfg, run_id=run_id, expected_commit=payload.expected_commit,
                force=payload.force, origin="remote", job_id=msg.job_id)
            await launch.wait_for_accept(handle, timeout_s=ACCEPT_TIMEOUT_S)
        except launch.UpdateLaunchError as exc:
            raise UpdateRefused(self._launch_issue(exc)) from None
        log.info("Update %s started (run %s, target %s)", msg.job_id, run_id, short_sha(payload.expected_commit))
        return UpdateApplyResult(run_id=run_id).model_dump(mode="json")

    # ------------------------------------------------------------------ watch loop (7.4)
    async def watch_loop(self, interval_s: float = WATCH_INTERVAL_S) -> None:
        """Runs for the agent's whole life (not per connection): reports, dead runs, draining."""
        while True:
            await self.refresh()
            await asyncio.sleep(interval_s)

    async def refresh(self) -> None:
        """One watch step. Never raises (except cancellation)."""
        try:
            run_id, run = await asyncio.to_thread(self._read_run)
            nonfinal = run is not None and run.get("status") is None
            report = files.report_from_run(run) if run is not None else None
            self.report = report.to_wire() if report is not None else None
            draining = bool(nonfinal and run is not None and run.get("phase") in RESTART_PHASES)
            if draining and not self.draining:
                log.info("An update is restarting this computer (run %s, %s): not accepting new jobs", run_id,
                         run.get("phase") if run else "?")
            elif self.draining and not draining:
                log.info("Accepting jobs again (update run %s ended)", run_id)
            self.draining = draining  # set before the job list is taken: nothing new starts after this
            if draining and run is not None and run_id is not None and run.get("phase") == "draining":
                running = self.other_jobs()
                await asyncio.to_thread(files.write_drain_file, self.paths, run_id, running)
            self._watch_failed = False
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._watch_failed:  # once, not every 2 s
                log.exception("Reading the update files failed")
            self._watch_failed = True

    def _read_run(self) -> tuple[str | None, dict[str, Any] | None]:
        latest = files.read_latest_run(self.paths)
        if latest is None:
            return None, None
        if latest[1].get("status") is None:
            # The runner holds the update lock for its whole life; if the lock is free its run is over
            # without a final word (killed, logoff, power loss): record that, with what runs now.
            info = record.build_info(self.paths)
            finalized = files.finalize_dead_run(self.paths, installed_commit=info.commit, computer=self.computer)
            if finalized is not None:
                log.warning("Update run %s was interrupted while %s; this computer runs %s", latest[0],
                            latest[1].get("phase"), short_sha(info.commit) or "an unknown commit")
                return latest[0], finalized
        return latest
