"""Durable job tracking, placement, dispatch, streaming, cancellation and recovery.

Lifecycle::

    queued -> dispatched -> running -> completed | failed | cancelled
                    \\-> (worker rejected / disconnected) -> queued again (idempotent jobs only)
    any non-terminal state -> interrupted (coordinator restarted, non-idempotent job)

Rules:

* A job row is written *before* dispatch, so every job is visible and auditable.
* Only the node a job was dispatched to may report on it; messages from other nodes are
  ignored and logged.
* Non-idempotent jobs (chat, tool execution, model management) are never retried
  automatically. Idempotent jobs (embeddings, document scans) are re-queued after a worker
  disconnect or coordinator restart, up to ``max_attempts``.
* Interactive jobs wait for capacity at most ``server.queue_timeout_s``; jobs that can never
  be placed (model not installed anywhere, missing capability) fail immediately with the
  scheduler's reasons.
* ``update.check`` / ``update.apply`` (``coordinator/update_service.py``) are placed directly on one
  node, never queued or requeued, don't count against the node's concurrency (``control_jobs``), and
  ``update.apply`` can't be cancelled here (the runner on that computer can't be stopped safely midway).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from homemesh.common.config import HomeMeshConfig
from homemesh.common.errors import ConflictError, NotFoundError, SchedulingError, ValidationFailed
from homemesh.common.ids import new_id
from homemesh.common.logging import get_logger
from homemesh.common.timeutil import now_iso
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.events import EventBus
from homemesh.coordinator.nodes import NodeManager
from homemesh.protocol import messages as pm
from homemesh.protocol.jobs import IDEMPOTENT_JOB_TYPES, TERMINAL_JOB_STATUSES, validate_payload
from homemesh.protocol.models import CAP_COMPLETION, CAP_EMBEDDING
from homemesh.protocol.update import (
    APPLY_JOB_TIMEOUT_S,
    CHECK_JOB_TIMEOUT_S,
    HINT_MAX,
    UPDATE_JOB_TYPES,
    format_update_error,
)
from homemesh.scheduler.policy import Candidate, Decision, TaskRequirements, schedule

log = get_logger(__name__)

# Reasons that may clear up by themselves (wait for capacity instead of failing at once).
TRANSIENT_REJECTIONS = {"at_capacity", "gpu_busy_external", "insufficient_memory", "insufficient_vram",
                        "stale_heartbeat", "updating"}
MAX_STREAM_EVENTS = 200_000


@dataclass
class JobSpec:
    type: str
    payload: dict[str, Any]
    requirements: TaskRequirements
    priority: str = "normal"
    user_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    collection_id: str | None = None
    parent_job_id: str | None = None
    timeout_s: float | None = None
    max_attempts: int | None = None


@dataclass
class JobHandle:
    job_id: str
    type: str
    priority: str
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    status: str = "queued"
    node_id: str | None = None
    node_name: str | None = None
    model: str | None = None
    backend: str | None = None
    backend_type: str | None = None
    mock: bool = False
    placement: str | None = None
    decision: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    metrics: dict[str, Any] | None = None
    dispatched_mono: float | None = None
    dispatch_overhead_ms: float | None = None
    events_pushed: int = 0
    excluded_nodes: set[str] = field(default_factory=set)
    requirements: TaskRequirements | None = None
    reserved_mb: int = 0

    def push(self, event: dict[str, Any]) -> None:
        self.events_pushed += 1
        if self.events_pushed > MAX_STREAM_EVENTS:
            return
        self.queue.put_nowait(event)

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events until a terminal event (completed/failed/cancelled)."""
        while True:
            ev = await self.queue.get()
            yield ev
            if ev["type"] in ("completed", "failed", "cancelled"):
                return

    async def wait(self, timeout_s: float | None = None) -> JobHandle:
        await asyncio.wait_for(self.done.wait(), timeout_s)
        return self


Handler = Callable[[dict[str, Any], Any], Awaitable[None]]


@dataclass
class JobTypeHandlers:
    """Callbacks for job types whose output is consumed by coordinator services
    (e.g. document scans and embeddings) rather than by a waiting client."""

    on_output: Handler | None = None
    on_completed: Handler | None = None
    on_failed: Handler | None = None


def requirements_from_row(row: dict[str, Any], allow_mock: bool = True) -> TaskRequirements:
    """Rebuild placement requirements for a queued job (e.g. after a coordinator restart)."""
    t = row["type"]
    payload = row.get("payload_json") or {}
    pinned = row.get("requested_node_id")
    if t == "chat":
        return TaskRequirements("chat", row.get("priority", "interactive"), model=payload.get("model") or None,
                                backend=payload.get("backend") or None, required_capabilities=frozenset({CAP_COMPLETION}),
                                pinned_node_id=pinned, allow_mock=allow_mock)
    if t == "embed":
        return TaskRequirements("embed", row.get("priority", "background"), model=payload.get("model"),
                                backend=payload.get("backend") or None,
                                required_capabilities=frozenset({CAP_EMBEDDING}), pinned_node_id=pinned,
                                allow_mock=allow_mock)
    return TaskRequirements(t, row.get("priority", "normal"), needs_model=False, pinned_node_id=pinned)


class JobService:
    def __init__(self, cfg: HomeMeshConfig, repos: Repositories, nodes: NodeManager, bus: EventBus) -> None:
        self.cfg = cfg
        self.repos = repos
        self.nodes = nodes
        self.bus = bus
        self.handles: dict[str, JobHandle] = {}
        self.handlers: dict[str, JobTypeHandlers] = {}
        self._wake = asyncio.Event()
        self._dispatcher: asyncio.Task[None] | None = None
        self._timers: dict[str, list[asyncio.Task[None]]] = {}
        self._progress_written: dict[str, float] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self.allow_mock = cfg.inference.mock_enabled
        self.allow_remote_models = cfg.cloud.enabled
        nodes.on_job_message = self.on_worker_message
        nodes.on_node_connected = self._on_node_connected
        nodes.on_node_disconnected = self.on_node_disconnected

    def timeout_for(self, job_type: str) -> float:
        return {
            "chat": self.cfg.inference.request_timeout_s,
            "embed": 600.0,
            "knowledge.scan": 4 * 3600.0,
            "tool.exec": 300.0,
            "model.manage": 6 * 3600.0,
            # The worker answers a check once the check runner is done (it waits up to 300 s) and an apply
            # once the runner accepted the run (15 s); the update itself is followed through run reports.
            "update.check": CHECK_JOB_TIMEOUT_S,
            "update.apply": APPLY_JOB_TIMEOUT_S,
        }.get(job_type, 600.0)

    def register_handlers(self, job_type: str, handlers: JobTypeHandlers) -> None:
        self.handlers[job_type] = handlers

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.recover()
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="job-dispatcher")

    async def stop(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher
        for tasks in self._timers.values():
            for t in tasks:
                t.cancel()

    async def recover(self) -> None:
        """Reconcile jobs left unfinished by a previous coordinator process."""
        for row in await self.repos.jobs.unfinished():
            if row["type"] in IDEMPOTENT_JOB_TYPES and row["attempts"] < row["max_attempts"]:
                if row["status"] != "queued":
                    await self.repos.jobs.update(row["id"], status="queued", node_id=None)
                    await self.repos.jobs.add_event(row["id"], "requeued", "re-queued after coordinator restart")
            else:
                await self.repos.jobs.update(row["id"], status="interrupted", error_code="coordinator_restarted",
                                             error="The coordinator restarted before this job finished.",
                                             finished_at=now_iso())
                await self.repos.jobs.add_event(row["id"], "interrupted", "coordinator restarted", level="warning")
        n = await self.repos.messages.mark_stale_streaming()
        if n:
            log.info("Marked %d interrupted streaming message(s) after restart", n)

    def wake(self) -> None:
        self._wake.set()

    async def _on_node_connected(self, node_id: str) -> None:
        conn = self.nodes.get(node_id)
        if conn and conn.hello.running_jobs:
            # Jobs the worker still runs from before a reconnect: keep the ones we still track
            # on this node, cancel the rest (their results can no longer be delivered).
            for job_id in conn.hello.running_jobs:
                row = await self.repos.jobs.get(job_id)
                if row and row["node_id"] == node_id and row["status"] in ("dispatched", "running"):
                    conn.assigned_jobs[job_id] = (row.get("model"), 0)
                    if row["type"] in UPDATE_JOB_TYPES:
                        conn.control_jobs.add(job_id)
                else:
                    await self.nodes.send(node_id, pm.JobCancel(job_id=job_id, reason="job no longer tracked"))
        self.wake()

    # ------------------------------------------------------------------ placement
    async def plan(self, req: TaskRequirements) -> Decision:
        req = replace(req, allow_mock=req.allow_mock and self.allow_mock,
                      allow_remote_models=req.allow_remote_models or self.allow_remote_models)
        return schedule(req, await self.nodes.snapshots(), heartbeat_timeout_s=self.cfg.server.heartbeat_timeout_s)

    async def place_interactive(self, req: TaskRequirements, timeout_s: float | None = None) -> Decision:
        """Find a placement now, or wait for capacity if the obstacles are temporary."""
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self.cfg.server.queue_timeout_s)
        while True:
            decision = await self.plan(req)
            if decision.chosen:
                return decision
            transient = [r for r in decision.rejections if r.code in TRANSIENT_REJECTIONS]
            if not transient or time.monotonic() >= deadline:
                raise self._scheduling_error(req, decision, waited=bool(transient))
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=min(2.0, max(0.1, deadline - time.monotonic())))

    def _scheduling_error(self, req: TaskRequirements, decision: Decision, waited: bool) -> SchedulingError:
        if not decision.rejections:
            return SchedulingError("No worker nodes are connected, so there is nowhere to run this request.",
                                   code="no_workers",
                                   hint="Start a worker ('homemesh start' runs one locally) or check the Nodes page.",
                                   details=decision.to_dict())
        what = req.model or ("a model with " + ", ".join(sorted(req.required_capabilities))
                             if req.required_capabilities else req.task_type)
        msg = f"No worker can run {what} right now: {decision.summary()}."
        hint = None
        codes = {r.code for r in decision.rejections}
        if "model_not_on_node" in codes or "no_suitable_model" in codes:
            hint = "Install the model on a worker (Models page or 'ollama pull <model>') or pick another model."
        elif "insufficient_memory" in codes or "insufficient_vram" in codes:
            hint = "Pick a smaller model, free GPU memory, or allow CPU inference on a node."
        elif "updating" in codes:
            hint = "A computer is restarting for an update; try again in a minute or two."
        elif waited:
            hint = "All suitable workers stayed busy; try again in a moment."
        elif "paused" in codes:
            hint = "Resume a paused node on the Nodes page."
        return SchedulingError(msg, code="no_eligible_worker", hint=hint, details=decision.to_dict())

    # ------------------------------------------------------------------ submission
    async def submit(self, spec: JobSpec, *, placement: Candidate | None = None,
                     decision: Decision | None = None, job_id: str | None = None) -> JobHandle:
        """Create a durable job. With ``placement`` it is dispatched immediately; otherwise it is
        queued and the dispatcher places it when a suitable node has capacity.

        ``job_id`` lets a caller generate the id first (``new_id("job")``) and record it before the job
        exists, e.g. an update row whose id *is* the ``update.apply`` job id: the worker may answer before
        this method returns."""
        try:
            validate_payload(spec.type, {**spec.payload, **({"model": spec.payload.get("model") or "pending"}
                                                           if spec.type in ("chat", "embed") else {})})
        except ValueError as exc:
            raise ValidationFailed(f"Invalid {spec.type} job payload: {exc}") from exc
        if placement is None and await self.repos.jobs.count_queued() >= self.cfg.server.max_queued_jobs:
            raise SchedulingError("The job queue is full.", code="queue_full",
                                  hint="Wait for queued jobs to finish or cancel some on the Jobs page.")
        idempotent = spec.type in IDEMPOTENT_JOB_TYPES
        job_id = job_id or new_id("job")
        await self.repos.jobs.create({
            "id": job_id, "type": spec.type, "priority": spec.priority, "user_id": spec.user_id,
            "conversation_id": spec.conversation_id, "message_id": spec.message_id,
            "collection_id": spec.collection_id, "requested_node_id": spec.requirements.pinned_node_id,
            "model": spec.payload.get("model"), "backend": spec.payload.get("backend"), "payload": spec.payload,
            "max_attempts": spec.max_attempts or (3 if idempotent else 1), "idempotent": idempotent,
            "parent_job_id": spec.parent_job_id,
        })
        handle = JobHandle(job_id=job_id, type=spec.type, priority=spec.priority, requirements=spec.requirements)
        self.handles[job_id] = handle
        await self.repos.jobs.add_event(job_id, "created", f"{spec.type} job created ({spec.priority})")
        self.bus.publish("job.created", {"job_id": job_id, "type": spec.type, "priority": spec.priority})
        if placement is not None:
            ok = await self._dispatch(job_id, handle, placement, decision, spec.payload)
            if not ok:
                await self._finish(job_id, handle, "failed", error_code="dispatch_failed",
                                   error=f"Could not send the job to {placement.node_name}; it went offline.")
        else:
            self.wake()
        return handle

    async def _dispatch(self, job_id: str, handle: JobHandle, cand: Candidate, decision: Decision | None,
                        payload: dict[str, Any]) -> bool:
        conn = self.nodes.get(cand.node_id)
        if conn is None:
            return False
        payload = dict(payload)
        if cand.model:
            payload["model"] = cand.model
            payload["backend"] = cand.backend
        row = await self.repos.jobs.get(job_id)
        attempts = (row["attempts"] if row else 0) + 1
        ok = await self.repos.jobs.transition(
            job_id, ("queued",), "dispatched", node_id=cand.node_id, model=cand.model, backend=cand.backend,
            mock=cand.mock, payload=payload, decision=decision.to_dict() if decision else None,
            dispatched_at=now_iso(), attempts=attempts)
        if not ok:
            return False
        reserved = (cand.estimated_memory_mb or 0) if cand.placement in ("gpu", "partial", "cpu") else 0
        conn.assigned_jobs[job_id] = (cand.model, reserved)
        if handle.type in UPDATE_JOB_TYPES:
            conn.control_jobs.add(job_id)  # tracked like any job, but never counted against capacity
        handle.status = "dispatched"
        handle.node_id, handle.node_name = cand.node_id, cand.node_name
        handle.model, handle.backend, handle.backend_type = cand.model, cand.backend, cand.backend_type
        handle.mock, handle.placement = cand.mock, cand.placement
        handle.decision = decision.to_dict() if decision else None
        handle.dispatched_mono = time.monotonic()
        handle.reserved_mb = reserved
        timeout = self.timeout_for(handle.type)
        sent = await self.nodes.send(cand.node_id, pm.JobDispatch(
            job_id=job_id, job_type=handle.type, priority=handle.priority, payload=payload, timeout_s=timeout))
        if not sent:
            conn.assigned_jobs.pop(job_id, None)
            conn.control_jobs.discard(job_id)
            await self.repos.jobs.update(job_id, status="queued", node_id=None)
            return False
        await self.repos.jobs.add_event(job_id, "dispatched", f"sent to {cand.node_name}"
                                        + (f" with model {cand.model} ({cand.placement})" if cand.model else ""),
                                        {"node_id": cand.node_id, "score": cand.score, "notes": cand.notes})
        handle.push({"type": "dispatched", "node_id": cand.node_id, "node_name": cand.node_name,
                     "model": cand.model, "backend": cand.backend, "backend_type": cand.backend_type,
                     "mock": cand.mock, "placement": cand.placement, "decision": handle.decision})
        self.bus.publish("job.updated", {"job_id": job_id, "status": "dispatched", "node_id": cand.node_id})
        self._start_timers(job_id, handle, timeout)
        return True

    def _start_timers(self, job_id: str, handle: JobHandle, timeout: float) -> None:
        async def ack_watch() -> None:
            await asyncio.sleep(self.cfg.server.dispatch_ack_timeout_s)
            if handle.status == "dispatched":
                await self._handle_lost(job_id, handle, "worker_unresponsive",
                                        f"{handle.node_name} did not acknowledge the job within "
                                        f"{self.cfg.server.dispatch_ack_timeout_s:.0f}s")

        async def total_watch() -> None:
            await asyncio.sleep(timeout)
            if handle.status not in TERMINAL_JOB_STATUSES:
                if handle.node_id:
                    await self.nodes.send(handle.node_id, pm.JobCancel(job_id=job_id, reason="timeout"))
                await self._finish(job_id, handle, "failed", error_code="timeout",
                                   error=f"The job exceeded its time limit of {timeout:.0f}s.")

        self._timers[job_id] = [asyncio.create_task(ack_watch()), asyncio.create_task(total_watch())]

    def _release(self, job_id: str, handle: JobHandle) -> None:
        if handle.node_id:
            conn = self.nodes.get(handle.node_id)
            if conn:
                conn.assigned_jobs.pop(job_id, None)
                conn.control_jobs.discard(job_id)
        for t in self._timers.pop(job_id, []):
            if t is not asyncio.current_task():
                t.cancel()
        self._progress_written.pop(job_id, None)

    async def _finish(self, job_id: str, handle: JobHandle, status: str, *, result: dict[str, Any] | None = None,
                      error: str | None = None, error_code: str | None = None,
                      metrics: dict[str, Any] | None = None, partial: dict[str, Any] | None = None,
                      hint: str | None = None) -> None:
        if handle.status in TERMINAL_JOB_STATUSES:
            return
        hint = hint[:HINT_MAX] if hint else None  # worker text, uncapped on the wire
        self._release(job_id, handle)
        handle.status = status
        handle.result, handle.error, handle.error_code, handle.metrics = result, error, error_code, metrics
        stored_result = result
        if result and handle.type == "chat" and isinstance(result.get("content"), str):
            stored_result = {**result, "content": result["content"][:4000]}
        await self.repos.jobs.update(job_id, status=status, result=stored_result, error=error, error_code=error_code,
                                     metrics=metrics, finished_at=now_iso())
        await self.repos.jobs.add_event(job_id, status, error or status, {"hint": hint} if hint else None,
                                        level="error" if status == "failed" else "info")
        handle.push({"type": status, "result": result, "error": error, "error_code": error_code,
                     "metrics": metrics, "partial": partial, "hint": hint})
        handle.done.set()
        self.bus.publish("job.updated", {"job_id": job_id, "status": status, "error_code": error_code})
        h = self.handlers.get(handle.type)
        row = await self.repos.jobs.get(job_id)
        if h and row:
            try:
                if status == "completed" and h.on_completed:
                    await h.on_completed(row, result)
                elif status in ("failed", "cancelled", "interrupted") and h.on_failed:
                    await h.on_failed(row, {"status": status, "error": error, "error_code": error_code,
                                            "hint": hint})
            except Exception:
                log.exception("Job handler for %s failed", handle.type)
        # Keep finished handles briefly for late readers, then forget them.
        asyncio.get_running_loop().call_later(300, self.handles.pop, job_id, None)
        self.wake()

    async def _handle_lost(self, job_id: str, handle: JobHandle, code: str, message: str) -> None:
        """Worker rejected, vanished or never acknowledged a job."""
        row = await self.repos.jobs.get(job_id)
        if row is None or handle.status in TERMINAL_JOB_STATUSES:
            return
        failed_node = handle.node_id
        self._release(job_id, handle)
        if failed_node:
            handle.excluded_nodes.add(failed_node)
        retryable = (row["type"] in IDEMPOTENT_JOB_TYPES and row["attempts"] < row["max_attempts"]) or (
            code == "rejected" and handle.status == "dispatched")
        await self.repos.jobs.add_event(job_id, "lost", message, {"code": code, "node_id": failed_node},
                                        level="warning")
        if retryable and not (handle.requirements and handle.requirements.pinned_node_id):
            handle.status = "queued"
            await self.repos.jobs.update(job_id, status="queued", node_id=None)
            handle.push({"type": "requeued", "reason": message})
            if handle.priority == "interactive" and handle.requirements is not None:
                await self._retry_interactive(job_id, handle, row)
            else:
                self.wake()
            return
        await self._finish(job_id, handle, "failed", error_code=code, error=message)

    async def _retry_interactive(self, job_id: str, handle: JobHandle, row: dict[str, Any]) -> None:
        req = handle.requirements
        assert req is not None
        if handle.model:
            req = replace(req, model=handle.model)
        decision = await self.plan(req)
        cand = next((c for c in decision.candidates if c.node_id not in handle.excluded_nodes), None)
        if cand is None or not await self._dispatch(job_id, handle, cand, decision, row.get("payload_json") or {}):
            await self._finish(job_id, handle, "failed", error_code="no_eligible_worker",
                               error="The assigned worker could not run the job and no other worker is available.")

    # ------------------------------------------------------------------ dispatcher for queued jobs
    async def _dispatch_loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            self._wake.clear()
            try:
                await self._dispatch_queued()
            except Exception:
                log.exception("Dispatcher iteration failed")

    async def _dispatch_queued(self) -> None:
        for row in await self.repos.jobs.queued(limit=200):
            if row["type"] in UPDATE_JOB_TYPES:
                continue  # placed directly by UpdateService, never through the queue (a failed send ends it)
            handle = self.handles.get(row["id"])
            if handle is None:
                handle = JobHandle(job_id=row["id"], type=row["type"], priority=row["priority"],
                                   requirements=requirements_from_row(row, self.allow_mock))
                self.handles[row["id"]] = handle
            if handle.requirements is None:
                handle.requirements = requirements_from_row(row, self.allow_mock)
            decision = await self.plan(handle.requirements)
            cand = next((c for c in decision.candidates if c.node_id not in handle.excluded_nodes), None)
            if cand is None:
                permanent = decision.rejections and not any(r.code in TRANSIENT_REJECTIONS | {"offline"}
                                                            for r in decision.rejections)
                if permanent and row["priority"] != "background":
                    await self._finish(row["id"], handle, "failed", error_code="no_eligible_worker",
                                       error=f"No worker can run this job: {decision.summary()}")
                continue
            await self._dispatch(row["id"], handle, cand, decision, row.get("payload_json") or {})

    # ------------------------------------------------------------------ worker messages
    async def on_worker_message(self, node_id: str, msg: Any) -> None:
        job_id = getattr(msg, "job_id", None)
        if job_id is None:
            return
        handle = self.handles.get(job_id)
        if handle is None or handle.node_id != node_id or handle.status in TERMINAL_JOB_STATUSES:
            row = await self.repos.jobs.get(job_id)
            if row is None or row["node_id"] != node_id or row["status"] in TERMINAL_JOB_STATUSES:
                log.warning("Ignoring %s for job %s from node %s (not assigned to it or already finished)",
                            msg.type, job_id, node_id)
                if row is not None and row["status"] in TERMINAL_JOB_STATUSES and isinstance(
                        msg, pm.JobOutput | pm.JobProgress | pm.JobAccepted):
                    await self.nodes.send(node_id, pm.JobCancel(job_id=job_id, reason="job already finished"))
                return
            if handle is None:
                handle = JobHandle(job_id=job_id, type=row["type"], priority=row["priority"], status=row["status"],
                                   node_id=node_id, model=row.get("model"), backend=row.get("backend"))
                self.handles[job_id] = handle

        if isinstance(msg, pm.JobAccepted):
            if await self.repos.jobs.transition(job_id, ("dispatched",), "running", started_at=now_iso()):
                handle.status = "running"
                if handle.dispatched_mono:
                    handle.dispatch_overhead_ms = round((time.monotonic() - handle.dispatched_mono) * 1000, 2)
                handle.push({"type": "accepted", "dispatch_overhead_ms": handle.dispatch_overhead_ms})
                self.bus.publish("job.updated", {"job_id": job_id, "status": "running", "node_id": node_id})
        elif isinstance(msg, pm.JobRejected):
            await self._handle_lost(job_id, handle, "rejected", f"{handle.node_name or node_id} rejected the job: "
                                                                f"{msg.reason} ({msg.code})")
        elif isinstance(msg, pm.JobProgress):
            handle.push({"type": "progress", "progress": msg.progress, "message": msg.message, "data": msg.data})
            now = time.monotonic()
            if now - self._progress_written.get(job_id, 0) > 1.0:
                self._progress_written[job_id] = now
                await self.repos.jobs.update(job_id, progress=msg.progress, progress_message=msg.message)
                self.bus.publish("job.progress", {"job_id": job_id, "progress": msg.progress, "message": msg.message})
        elif isinstance(msg, pm.JobOutput):
            handle.push({"type": "output", "kind": msg.kind, "delta": msg.delta, "data": msg.data, "seq": msg.seq})
            h = self.handlers.get(handle.type)
            if h and h.on_output:
                row = await self.repos.jobs.get(job_id)
                try:
                    await h.on_output(row or {"id": job_id}, msg)
                except Exception:
                    log.exception("Output handler for %s failed", handle.type)
        elif isinstance(msg, pm.JobCompleted):
            metrics = msg.metrics.model_dump(exclude_none=True) if msg.metrics else {}
            if handle.dispatch_overhead_ms is not None:
                metrics["dispatch_overhead_ms"] = handle.dispatch_overhead_ms
            await self._finish(job_id, handle, "completed", result=msg.result, metrics=metrics or None)
        elif isinstance(msg, pm.JobFailed):
            row = await self.repos.jobs.get(job_id)
            if (msg.retryable and row and row["type"] in IDEMPOTENT_JOB_TYPES
                    and row["attempts"] < row["max_attempts"]):
                await self._handle_lost(job_id, handle, msg.code, msg.error)
            else:
                await self._finish(job_id, handle, "failed", error_code=msg.code, error=msg.error, partial=msg.partial,
                                   hint=msg.hint)
        elif isinstance(msg, pm.JobCancelled):
            await self._finish(job_id, handle, "cancelled", error_code="cancelled", error="Cancelled.",
                               partial=msg.partial)

    async def on_node_disconnected(self, node_id: str, reason: str) -> None:
        for row in await self.repos.jobs.active_on_node(node_id):
            handle = self.handles.get(row["id"]) or JobHandle(job_id=row["id"], type=row["type"],
                                                              priority=row["priority"], status=row["status"],
                                                              node_id=node_id)
            self.handles[row["id"]] = handle
            name = handle.node_name or node_id
            if row["type"] in IDEMPOTENT_JOB_TYPES and row["attempts"] < row["max_attempts"]:
                await self._handle_lost(row["id"], handle, "worker_disconnected",
                                        f"{name} disconnected ({reason}); job re-queued")
            else:
                await self._finish(row["id"], handle, "failed", error_code="worker_disconnected",
                                   error=f"Worker {name} disconnected while running this job ({reason}).")
        self.wake()

    # ------------------------------------------------------------------ control
    async def cancel(self, job_id: str, actor: str | None = None, reason: str = "cancelled by user") -> dict[str, Any]:
        row = await self.repos.jobs.get(job_id)
        if not row:
            raise NotFoundError("Job not found.")
        if row["type"] == "update.apply":
            # Once launched, the runner on that computer must finish or roll back by itself; stopping it
            # halfway could leave HomeMesh stopped. (update.check may be cancelled: the worker kills it.)
            issue = format_update_error("update_not_cancellable")
            raise ConflictError(issue.message, code=issue.code, hint=issue.hint)
        handle = self.handles.get(job_id) or JobHandle(job_id=job_id, type=row["type"], priority=row["priority"],
                                                       status=row["status"], node_id=row.get("node_id"))
        self.handles[job_id] = handle
        if row["status"] in TERMINAL_JOB_STATUSES:
            return {"job_id": job_id, "status": row["status"]}
        await self.repos.jobs.update(job_id, cancel_requested=True)
        await self.repos.jobs.add_event(job_id, "cancel_requested", reason)
        if row["status"] == "queued":
            await self._finish(job_id, handle, "cancelled", error_code="cancelled", error="Cancelled before it started.")
            return {"job_id": job_id, "status": "cancelled"}
        node_id = row.get("node_id")
        sent = bool(node_id) and await self.nodes.send(node_id, pm.JobCancel(job_id=job_id, reason=reason))

        async def grace() -> None:
            await asyncio.sleep(10.0 if sent else 0)
            if handle.status not in TERMINAL_JOB_STATUSES:
                await self._finish(job_id, handle, "cancelled", error_code="cancelled",
                                   error="Cancelled (worker did not confirm in time).")

        t = asyncio.create_task(grace())
        self._background.add(t)
        t.add_done_callback(self._background.discard)
        return {"job_id": job_id, "status": "cancelling"}

    async def run_to_completion(self, spec: JobSpec, timeout_s: float | None = None) -> JobHandle:
        """Submit an interactive job (placing it now) and wait for the result. Used for short
        internal jobs such as query embeddings and tool execution on a node."""
        decision = await self.place_interactive(spec.requirements)
        assert decision.chosen is not None
        handle = await self.submit(spec, placement=decision.chosen, decision=decision)
        await handle.wait(timeout_s or self.timeout_for(spec.type) + 5)
        return handle
