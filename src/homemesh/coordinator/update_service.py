"""Remote updates from the hub: the owner's "Check for updates" / "Update" (docs/UPDATING.md).

The hub only asks a computer to check, or to fast-forward to the commit a check showed; the worker and the
update runner on that computer re-check consent, the update lock, the tracked branch and the commit sha.
**Every rule in this module is UX**, never the security boundary.

* ``check`` and ``apply`` submit ``update.check`` / ``update.apply`` jobs placed directly on one node
  (``JobService.submit(placement=...)``): no scheduler, so paused or busy computers still get them, and they
  are never queued or retried.
* An ``update.apply`` creates a ``node_updates`` row whose id **is** the job id (at most one active row in the
  whole mesh, enforced by the repository). The job only says whether the runner started; what happened after
  comes from run reports (``Hello.update`` / ``Heartbeat.update``) that carry the job id, including after a
  restart of the hub itself.
* No timers and no deadline: a row the hub can't hear about stays active until a report arrives or the owner
  dismisses it after :data:`DISMISS_AFTER_S` without word ("outcome unknown").
* Rows the hub ends without word from the runner (``not_started``, ``abandoned``, ``failed`` before any
  report) are guesses: a later report for the same job id overrides them (``UpdateRepo``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import ValidationError

from homemesh.common.errors import ConflictError, HomeMeshError, NotFoundError, ValidationFailed
from homemesh.common.ids import new_id
from homemesh.common.logging import get_logger
from homemesh.common.timeutil import now_iso, parse_iso, utcnow
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.db.repositories.updates import ActiveUpdateExists
from homemesh.coordinator.events import EventBus
from homemesh.coordinator.jobs import JobService, JobSpec, JobTypeHandlers
from homemesh.coordinator.nodes import NodeManager
from homemesh.protocol.jobs import TERMINAL_JOB_STATUSES
from homemesh.protocol.update import (
    DISMISS_AFTER_S,
    SUCCESS_STATUSES,
    UPDATE_ERROR_CODES,
    UpdateApplyPayload,
    UpdateApplyResult,
    UpdateCheckPayload,
    UpdateCheckResult,
    UpdateIssue,
    UpdateRunReport,
    format_update_error,
    installer_command,
)
from homemesh.scheduler.policy import Candidate, TaskRequirements

log = get_logger(__name__)

# Apply-job endings that say nothing about whether the runner started: the row stays as it is and the run
# reports decide (or "not started" once the computer reports something else, see _maybe_not_started).
UNKNOWN_OUTCOME_CODES = frozenset({"worker_disconnected", "timeout", "coordinator_restarted", "worker_unresponsive",
                                   "cancelled"})

CHECK_UNREADABLE = "update_check_unreadable"  # coordinator-only: a check result this version can't parse
NO_UPDATER_REASON = "it runs a HomeMesh version without the updater"
UNREADABLE_BUILD_REASON = "the hub can't read its version information (different HomeMesh versions?)"


def _os_key(os_name: str | None) -> str | None:
    """Node row ``os`` (``platform.system()``) → the ``BuildInfo.os`` vocabulary."""
    low = (os_name or "").lower()
    return "windows" if low == "windows" else "linux" if low == "linux" else None


def _conflict(issue: UpdateIssue) -> ConflictError:
    return ConflictError(issue.message, code=issue.code, hint=issue.hint)


class UpdateService:
    def __init__(self, repos: Repositories, nodes: NodeManager, jobs: JobService, bus: EventBus) -> None:
        self.repos = repos
        self.nodes = nodes
        self.jobs = jobs
        self.bus = bus
        self._checks: dict[str, str] = {}  # node_id -> job id of the update.check in flight
        # Serialises the rule checks of check/apply/dismiss with the reservation that follows them (a check
        # id in _checks, an update row), so two clicks can't both pass. Never held while a job is submitted:
        # job handlers run inside submit() when the send fails.
        self._lock = asyncio.Lock()
        jobs.register_handlers("update.check", JobTypeHandlers(on_completed=self._check_completed,
                                                               on_failed=self._check_failed))
        jobs.register_handlers("update.apply", JobTypeHandlers(on_completed=self._apply_completed,
                                                               on_failed=self._apply_failed))
        nodes.on_update_report = self.on_update_report
        nodes.on_node_revoked = self.on_node_revoked

    # ------------------------------------------------------------------ helpers
    def check_pending(self, node_id: str) -> str | None:
        """Job id of the check running on this node (``update_check_pending`` in the nodes API)."""
        return self._checks.get(node_id)

    async def _node(self, node_id: str) -> dict[str, Any]:
        row = await self.repos.nodes.get(node_id)
        if not row or row["status"] != "active":
            raise NotFoundError("Node not found.")
        return row

    def _require_online(self, node: dict[str, Any]) -> None:
        if self.nodes.get(node["id"]) is None:
            raise ConflictError(f"{node['name']} is offline.", code="node_offline",
                                hint="Start HomeMesh on it (or wait until it is back on), then try again.")

    async def _name(self, node_id: str | None) -> str:
        row = await self.repos.nodes.get(node_id) if node_id else None
        return row["name"] if row else "this computer"

    @staticmethod
    def _can_update(node: dict[str, Any]) -> bool:
        build = node.get("build_json")
        return isinstance(build, dict) and not build.get("invalid") and bool(build.get("can_update"))

    def _not_supported(self, node: dict[str, Any]) -> ConflictError:
        """``update_not_supported`` naming why, with the installer command for that computer."""
        build = node.get("build_json")
        if not isinstance(build, dict):
            reason, os_name, source_dir = NO_UPDATER_REASON, _os_key(node.get("os")), None
        elif build.get("invalid"):
            reason, os_name, source_dir = UNREADABLE_BUILD_REASON, _os_key(node.get("os")), None
        else:
            reason = build.get("cannot_update_reason") or build.get("reason")
            os_name, source_dir = build.get("os"), build.get("source_dir")
        return _conflict(format_update_error(
            "update_not_supported", computer=node["name"], reason=reason,
            installer_command=installer_command(os_name, source_dir, pull=True)))

    async def _busy(self, node: dict[str, Any], *, mesh: bool) -> ConflictError | None:
        """``update_busy`` if this node (``mesh``: any computer) is updating, else None. A check in flight on
        this node counts as busy too (the check runner there holds the update lock)."""
        node_id, name = node["id"], node["name"]
        if node_id in self._checks:
            return _conflict(format_update_error("update_busy", computer=name, phase="checking for updates"))
        active = await (self.repos.updates.get_active() if mesh else self.repos.updates.active_for_node(node_id))
        if active is not None:
            return await self._busy_error(node, active["node_id"], active.get("phase"))
        # Runs the hub has no row for (a local "homemesh update") block too while they are not final.
        for conn in list(self.nodes.connections.values()):
            report = conn.update_live
            if report is not None and not report.final and (mesh or conn.node_id == node_id):
                return await self._busy_error(node, conn.node_id, report.phase)
        return None

    async def _busy_error(self, node: dict[str, Any], other_node_id: str | None, phase: str | None) -> ConflictError:
        if other_node_id == node["id"]:
            return _conflict(format_update_error("update_busy", computer=node["name"], phase=phase))
        return _conflict(format_update_error("update_busy", variant="mesh", computer=node["name"],
                                             name=await self._name(other_node_id)))

    def _job_spec(self, job_type: str, payload: dict[str, Any], node_id: str, actor: str | None) -> JobSpec:
        return JobSpec(job_type, payload, TaskRequirements(job_type, "interactive", needs_model=False,
                                                           pinned_node_id=node_id),
                       priority="interactive", user_id=actor)

    @staticmethod
    def _placement(node: dict[str, Any]) -> Candidate:
        return Candidate(node["id"], node["name"], None, None, None, 0.0, "n/a")

    @staticmethod
    def _offline_after_dispatch(node: dict[str, Any]) -> ConflictError:
        return ConflictError(f"{node['name']} went offline before the request could be sent.", code="node_offline",
                             hint="Try again when it is back on.")

    async def _audit(self, actor: str | None, action: str, node_id: str, *, outcome: str = "success",
                     ip: str | None = None, details: dict[str, Any] | None = None, actor_type: str = "user") -> None:
        await self.repos.audit.add(actor_type, actor, action, target_type="node", target_id=node_id,
                                   outcome=outcome, ip=ip, details=details)

    def _publish(self, node_id: str) -> None:
        self.bus.publish("node.updated", {"node_id": node_id})

    @staticmethod
    def _issue_from_job(code: str, info: dict[str, Any], name: str) -> UpdateIssue:
        """Worker text capped (``JobFailed.error`` / ``hint`` are uncapped on the wire); a table code without a
        message or hint gets the table's text."""
        issue = UpdateIssue(code=code or "update_failed", message=info.get("error") or "", hint=info.get("hint"))
        if issue.code in UPDATE_ERROR_CODES and (not issue.message or issue.hint is None):
            table = format_update_error(issue.code, computer=name)
            issue = UpdateIssue(code=issue.code, message=issue.message or table.message,
                                hint=issue.hint if issue.hint is not None else table.hint)
        return issue

    # ------------------------------------------------------------------ check
    async def check(self, node_id: str, actor: str | None, *, ip: str | None = None) -> dict[str, Any]:
        """Ask a computer to check its tracked branch. Returns ``{"job_id"}``; the result is stored on the node
        (``update_check``) when the job ends."""
        job_id = new_id("job")
        try:
            async with self._lock:
                node = await self._node(node_id)
                self._require_online(node)
                if node.get("build_json") is None:  # an invalid build may still have an updater: let it say
                    raise self._not_supported(node)
                busy = await self._busy(node, mesh=False)
                if busy is not None:
                    raise busy
                self._checks[node_id] = job_id  # one check per node; cleared by the job handlers
        except HomeMeshError as exc:
            await self._audit(actor, "node.update_check_requested", node_id, outcome="failure", ip=ip,
                              details={"code": exc.code})
            raise
        try:
            await self._audit(actor, "node.update_check_requested", node_id, ip=ip, details={"job_id": job_id})
            handle = await self.jobs.submit(
                self._job_spec("update.check", UpdateCheckPayload().model_dump(), node_id, actor),
                placement=self._placement(node), job_id=job_id)
        except BaseException:
            if self._checks.get(node_id) == job_id:
                del self._checks[node_id]
            raise
        self._publish(node_id)
        if handle.error_code == "dispatch_failed":
            raise self._offline_after_dispatch(node)
        return {"job_id": job_id}

    def _release_check(self, node_id: str | None, job_id: str) -> None:
        if node_id and self._checks.get(node_id) == job_id:
            del self._checks[node_id]

    async def _check_completed(self, job: dict[str, Any], result: Any) -> None:
        node_id = job.get("node_id") or job.get("requested_node_id")
        self._release_check(node_id, job["id"])
        if not node_id:
            return
        name = await self._name(node_id)
        try:
            parsed = UpdateCheckResult.model_validate(result)
        except Exception:
            log.warning("Update check %s from %s returned a result this version can't read", job["id"], name)
            parsed = None
        stored: dict[str, Any] = {"job_id": job["id"], "checked_at": now_iso()}
        if parsed is None:
            stored["error"] = {"code": CHECK_UNREADABLE,
                               "message": f"The check result from {name} could not be read.",
                               "hint": f"Update the hub and {name} to the same HomeMesh version, then check again."}
        elif not parsed.ok and parsed.error is not None:
            stored["error"] = parsed.error.model_dump(mode="json")
        else:
            stored["result"] = parsed.model_dump(mode="json")
        await self.repos.nodes.set_update_check(node_id, stored)
        self._publish(node_id)

    async def _check_failed(self, job: dict[str, Any], info: dict[str, Any]) -> None:
        node_id = job.get("node_id") or job.get("requested_node_id")
        self._release_check(node_id, job["id"])
        if not node_id:
            return
        if info.get("status") != "cancelled":  # cancelled by the owner: the previous check stays shown
            issue = self._issue_from_job(info.get("error_code") or "", info, await self._name(node_id))
            await self.repos.nodes.set_update_check(node_id, {"job_id": job["id"], "checked_at": now_iso(),
                                                              "error": issue.model_dump(mode="json")})
        self._publish(node_id)

    # ------------------------------------------------------------------ apply
    async def apply(self, node_id: str, expected_commit: str, force: bool, actor: str | None, *,
                    ip: str | None = None) -> dict[str, Any]:
        """Ask a computer to install ``expected_commit`` (the upstream tip its last check showed). Returns
        ``{"update_id", "state"}``; the update id is the ``update.apply`` job id."""
        try:
            payload = UpdateApplyPayload(expected_commit=expected_commit, force=force)
        except ValidationError as exc:
            raise ValidationFailed("expected_commit must be a full lower-case hex commit id and force true or "
                                   "false.") from exc
        update_id = new_id("job")
        try:
            async with self._lock:
                node = await self._node(node_id)
                self._require_online(node)
                if not self._can_update(node):
                    raise self._not_supported(node)
                stored = node.get("update_check_json")
                result = stored.get("result") if isinstance(stored, dict) and not stored.get("error") else None
                if not (isinstance(result, dict) and result.get("ok") and result.get("can_update")
                        and result.get("upstream_commit") == payload.expected_commit):
                    raise _conflict(format_update_error("update_check_required", computer=node["name"]))
                busy = await self._busy(node, mesh=True)
                if busy is not None:
                    raise busy
                try:
                    await self.repos.updates.create_active(
                        update_id=update_id, node_id=node_id, expected_commit=payload.expected_commit,
                        from_commit=(node.get("build_json") or {}).get("commit"), force=payload.force,
                        requested_by=actor)
                except ActiveUpdateExists as exc:
                    other = exc.existing
                    raise await self._busy_error(node, other["node_id"] if other else None,
                                                 other.get("phase") if other else None) from None
        except HomeMeshError as exc:
            await self._audit(actor, "node.update_requested", node_id, outcome="failure", ip=ip,
                              details={"code": exc.code, "expected_commit": payload.expected_commit})
            raise
        await self._audit(actor, "node.update_requested", node_id, ip=ip,
                          details={"update_id": update_id, "expected_commit": payload.expected_commit,
                                   "force": payload.force})
        try:
            handle = await self.jobs.submit(
                self._job_spec("update.apply", payload.model_dump(), node_id, actor),
                placement=self._placement(node), job_id=update_id)
        except BaseException:
            # The job was never sent: end the row so it can't block the mesh.
            await asyncio.shield(self.repos.updates.finish(
                update_id, state="failed", error_code="dispatch_failed",
                error=f"The update request could not be sent to {node['name']}."))
            self._publish(node_id)
            raise
        self._publish(node_id)
        if handle.error_code == "dispatch_failed":
            raise self._offline_after_dispatch(node)
        row = await self.repos.updates.get(update_id)
        return {"update_id": update_id, "state": row["state"] if row else "requested"}

    async def _apply_completed(self, job: dict[str, Any], result: Any) -> None:
        try:
            started = UpdateApplyResult.model_validate(result)
        except Exception:
            log.warning("Update job %s returned a result this version can't read; waiting for run reports",
                        job["id"])
            return
        if await self.repos.updates.set_running(job["id"], started.run_id):
            row = await self.repos.updates.get(job["id"])
            if row:
                self._publish(row["node_id"])

    async def _apply_failed(self, job: dict[str, Any], info: dict[str, Any]) -> None:
        row = await self.repos.updates.get(job["id"])
        if row is None:
            return
        node_id = row["node_id"]
        code = info.get("error_code") or ""
        if code in UNKNOWN_OUTCOME_CODES or info.get("status") in ("cancelled", "interrupted"):
            # The runner may have started; its reports decide. If the computer is connected right now (it never
            # answered, or it already reconnected: its new hello can be handled before the old connection's
            # drop ends this job), what it reports now tells whether this run exists. Waiting for the next
            # report instead would wait forever: unchanged heartbeat reports are not handled again.
            conn = self.nodes.get(node_id)
            if conn is not None:
                await self._maybe_not_started(node_id, conn.update_live, unreadable=conn.update_unreadable)
            return
        # Refused before the runner was launched (update_* codes), never sent (dispatch_failed), or rejected.
        issue = self._issue_from_job(code, info, await self._name(node_id))
        if await self.repos.updates.finish(row["id"], state="failed", error_code=issue.code, error=issue.message,
                                           hint=issue.hint):
            await self._audit(node_id, "node.update_finished", node_id, outcome="failure", actor_type="node",
                              details={"update_id": row["id"], "state": "failed", "error_code": issue.code})
            self._publish(node_id)

    # ------------------------------------------------------------------ reports
    async def on_update_report(self, node_id: str, report: UpdateRunReport | None, *, hello: bool,
                               unreadable: bool = False) -> None:
        """A run report from ``node_id`` (every hello, and heartbeats whose report changed). Reports of local
        runs (``job_id`` null) are only shown live (``update_live``)."""
        if report is not None and report.job_id:
            row = await self.repos.updates.get(report.job_id)
            if row is not None and row["node_id"] != node_id:
                log.warning("Ignoring an update report from node %s for an update of another computer", node_id)
            elif row is not None:
                await self._apply_report(row, report)
        await self._maybe_not_started(node_id, report, unreadable=unreadable)

    async def _apply_report(self, row: dict[str, Any], report: UpdateRunReport) -> None:
        node_id = row["node_id"]
        wire = report.to_wire()
        if not report.final:
            if await self.repos.updates.update_from_report(row["id"], phase=report.phase, report=wire):
                self._publish(node_id)
            return
        err = report.error
        status = report.status
        assert status is not None
        if await self.repos.updates.finish(row["id"], state=status, error_code=err.code if err else None,
                                           error=err.message if err else None, hint=err.hint if err else None,
                                           report=wire, phase=report.phase):
            await self._audit(node_id, "node.update_finished", node_id, actor_type="node",
                              outcome="success" if status in SUCCESS_STATUSES else "failure",
                              details={"update_id": row["id"], "state": status, "run_id": report.run_id,
                                       "to_commit": report.to_commit, "error_code": err.code if err else None})
            self._publish(node_id)

    async def _maybe_not_started(self, node_id: str, report: UpdateRunReport | None, *, unreadable: bool) -> None:
        """End the node's active update as ``not_started`` when the computer is online, the apply job ended
        without confirming that the runner started, and the computer reports no run or another one.

        A *completed* apply job proves the runner accepted the run, so it never ends this way: a report about
        another run then is an agent whose report cache is a moment old (or a later local run); the row waits
        for its own reports or for Dismiss. Either way ``not_started`` is a guess a later report overrides.
        """
        if unreadable or self.nodes.get(node_id) is None:
            return
        row = await self.repos.updates.active_for_node(node_id)
        if row is None or (report is not None and report.job_id == row["id"]):
            return
        job = await self.repos.jobs.get(row["id"])
        if job is not None and (job["status"] not in TERMINAL_JOB_STATUSES or job["status"] == "completed"):
            return
        issue = format_update_error("update_not_started", computer=await self._name(node_id))
        if await self.repos.updates.finish(row["id"], state="not_started", error_code=issue.code,
                                           error=issue.message, hint=issue.hint):
            await self._audit(node_id, "node.update_finished", node_id, actor_type="node", outcome="failure",
                              details={"update_id": row["id"], "state": "not_started",
                                       "reported_run": report.run_id if report else None})
            self._publish(node_id)

    # ------------------------------------------------------------------ removed computers
    async def on_node_revoked(self, node_id: str, actor: str | None) -> None:
        """A removed computer can never report again, and its card (with Dismiss) is gone: end its active
        update as ``abandoned`` so it doesn't block updates on every other computer (one active update in the
        mesh) forever."""
        self._checks.pop(node_id, None)
        row = await self.repos.updates.active_for_node(node_id)
        if row is None:
            return
        issue = format_update_error("update_abandoned", computer=await self._name(node_id))
        if await self.repos.updates.abandon(row["id"], error=issue.message, hint=issue.hint):
            await self._audit(actor, "node.update_finished", node_id, outcome="failure",
                              details={"update_id": row["id"], "state": "abandoned", "reason": "computer removed"})
            self._publish(node_id)

    # ------------------------------------------------------------------ dismiss
    async def dismiss(self, node_id: str, actor: str | None, *, ip: str | None = None) -> dict[str, Any]:
        """The owner gives up waiting: the node's active update becomes ``abandoned`` ("outcome unknown"),
        allowed only after :data:`DISMISS_AFTER_S` without a report. A later report still records the real
        outcome."""
        try:
            node = await self._node(node_id)
            row = await self.repos.updates.active_for_node(node_id)
            if row is None:
                raise NotFoundError(f"No update is in progress on {node['name']}.")
            conn = self.nodes.get(node_id)
            live = conn.update_live if conn else None
            if live is not None and live.job_id == row["id"] and not live.final:
                age_s = 0.0  # connected and still reporting this run right now
            else:
                ref = parse_iso(row.get("last_report_at") or row["requested_at"])
                age_s = max(0.0, (utcnow() - ref).total_seconds()) if ref else DISMISS_AFTER_S
            if age_s < DISMISS_AFTER_S:
                raise _conflict(format_update_error("update_still_reporting", computer=node["name"],
                                                    n=int(age_s // 60)))
            issue = format_update_error("update_abandoned", computer=node["name"])
            if not await self.repos.updates.abandon(row["id"], error=issue.message, hint=issue.hint):
                raise NotFoundError(f"The update on {node['name']} already finished.")
        except HomeMeshError as exc:
            await self._audit(actor, "node.update_dismissed", node_id, outcome="failure", ip=ip,
                              details={"code": exc.code})
            raise
        await self._audit(actor, "node.update_dismissed", node_id, ip=ip, details={"update_id": row["id"]})
        self._publish(node_id)
        return {"update_id": row["id"], "state": "abandoned"}
