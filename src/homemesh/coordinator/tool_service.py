"""Tool orchestration on the coordinator: offering tools, approvals, execution and audit.

* Only enabled tools are offered, and only to models whose backend reports ``tools``.
* Write/destructive tools always wait for an explicit user approval (approve/deny in the UI,
  from any device). Read tools run without approval unless the owner sets ``approval=always``.
* Node tools run as ``tool.exec`` jobs on the chosen computer, whose worker re-checks its own
  allow-list. A node cannot be targeted unless it advertises the tool as allowed.
* Every call, approval decision and result is written to the tool execution log and the
  audit log.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from homemesh.common.errors import NotFoundError, ValidationFailed
from homemesh.common.logging import get_logger
from homemesh.common.timeutil import iso, utcnow
from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.events import EventBus
from homemesh.coordinator.jobs import JobService, JobSpec
from homemesh.coordinator.nodes import NodeManager
from homemesh.coordinator.settings_service import SettingsService
from homemesh.protocol.jobs import ToolCall, ToolSpec
from homemesh.scheduler.policy import TaskRequirements
from homemesh.tools.base import ToolContext, ToolDefinition
from homemesh.tools.registry import ToolArgumentError, ToolRegistry

log = get_logger(__name__)
MAX_RESULT_CHARS = 8000


class ToolService:
    def __init__(self, repos: Repositories, jobs: JobService, nodes: NodeManager, settings: SettingsService,
                 bus: EventBus, services: Any) -> None:
        self.repos = repos
        self.jobs = jobs
        self.nodes = nodes
        self.settings = settings
        self.bus = bus
        self.services = services
        self.registry = ToolRegistry()
        self._waiters: dict[str, asyncio.Future[bool]] = {}

    async def start(self) -> None:
        # Approvals cannot survive a restart (the waiting chat request is gone).
        n = await self.repos.tools.expire_stale()
        for a in await self.repos.tools.list_approvals("pending"):
            await self.repos.tools.expire(a["id"])
            n += 1
        if n:
            log.info("Expired %d pending tool approval(s) from a previous run", n)

    def _nodes_allowing(self, tool: str) -> list[tuple[str, str]]:
        out = []
        for conn in self.nodes.connections.values():
            if tool in conn.hello.allowed_tools and "tool.exec" in conn.hello.task_types:
                out.append((conn.node_id, conn.name))
        out.sort(key=lambda x: (not (self.nodes.get(x[0]) and self.nodes.get(x[0]).hello.embedded), x[1]))  # type: ignore[union-attr]
        return out

    async def list_tools(self) -> list[dict[str, Any]]:
        policies = await self.repos.tools.policies()
        out = []
        for t in self.registry.all():
            p = policies.get(t.name)
            d = t.describe()
            d["enabled"] = bool(p["enabled"]) if p else True
            d["approval"] = p["approval"] if p else "default"
            d["available_on"] = ([name for _, name in self._nodes_allowing(t.name)] if t.location == "node"
                                 else ["coordinator"])
            out.append(d)
        return out

    async def set_policy(self, name: str, enabled: bool, approval: str, actor: str | None) -> None:
        if not self.registry.get(name):
            raise NotFoundError(f"Unknown tool {name!r}.")
        if approval not in ("default", "always"):
            raise ValidationFailed("approval must be 'default' or 'always'.")
        await self.repos.tools.set_policy(name, enabled, approval)
        await self.repos.audit.add("user", actor, "tool.policy_changed", target_type="tool", target_id=name,
                                   details={"enabled": enabled, "approval": approval})

    async def offered(self) -> list[ToolSpec]:
        if not (await self.settings.section("tools"))["enabled"]:
            return []
        policies = await self.repos.tools.policies()
        specs = []
        for t in self.registry.all():
            p = policies.get(t.name)
            if p and not p["enabled"]:
                continue
            spec = t.spec()
            if t.location == "node":
                nodes = self._nodes_allowing(t.name)
                if not nodes:
                    continue
                params = json.loads(json.dumps(spec.parameters))
                params.setdefault("properties", {})["node"] = {
                    "type": "string", "enum": [n for _, n in nodes],
                    "description": "Which computer to run on (default: " + nodes[0][1] + ")"}
                spec = ToolSpec(name=spec.name, description=spec.description, parameters=params)
            specs.append(spec)
        return specs

    async def _await_approval(self, approval: dict[str, Any], timeout_s: float) -> bool | None:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._waiters[approval["id"]] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError:
            await self.repos.tools.expire(approval["id"])
            self.bus.publish("approval.expired", {"approval_id": approval["id"]})
            return None
        finally:
            self._waiters.pop(approval["id"], None)

    async def decide(self, approval_id: str, approved: bool, user_id: str, reason: str | None = None) -> dict[str, Any]:
        a = await self.repos.tools.get_approval(approval_id)
        if not a:
            raise NotFoundError("Approval request not found.")
        if a.get("user_id") and a["user_id"] != user_id:
            raise NotFoundError("Approval request not found.")
        if not await self.repos.tools.decide(approval_id, approved, user_id, reason):
            raise ValidationFailed(f"This request is already {a['status']}.", code="approval_closed")
        await self.repos.audit.add("user", user_id, "tool.approval_" + ("granted" if approved else "denied"),
                                   target_type="tool_approval", target_id=approval_id,
                                   details={"tool": a["tool_name"]})
        fut = self._waiters.get(approval_id)
        if fut and not fut.done():
            fut.set_result(approved)
        self.bus.publish("approval.decided", {"approval_id": approval_id, "approved": approved})
        return (await self.repos.tools.get_approval(approval_id)) or {}

    async def execute(self, call: ToolCall, *, user_id: str, conversation_id: str | None, message_id: str | None,
                      emit: Any = None) -> dict[str, Any]:
        """Run one model-requested tool call. Always returns a JSON-able result for the model."""
        tool = self.registry.get(call.name)
        policies = await self.repos.tools.policies()
        p = policies.get(call.name)
        if tool is None or (p and not p["enabled"]):
            return {"ok": False, "error": f"Tool '{call.name}' is not available."}
        args = dict(call.arguments or {})
        node_id = None
        if tool.location == "node":
            requested = args.pop("node", None)
            nodes = self._nodes_allowing(tool.name)
            match = [n for n in nodes if n[1] == requested] if requested else nodes[:1]
            if not match:
                return {"ok": False, "error": f"No online computer allows '{tool.name}'"
                                              + (f" named {requested!r}" if requested else "") + "."}
            node_id = match[0][0]
        try:
            parsed = self.registry.validate_args(tool, args)
        except ToolArgumentError as exc:
            return {"ok": False, "error": str(exc)}
        needs_approval = tool.requires_approval or (p is not None and p["approval"] == "always")
        approval_id = None
        if needs_approval:
            timeout_s = (await self.settings.section("tools"))["approval_timeout_s"]
            approval = await self.repos.tools.create_approval(
                user_id=user_id, tool_name=tool.name, arguments=args, node_id=node_id,
                conversation_id=conversation_id, message_id=message_id, side_effect=tool.side_effect,
                expires_at=iso(utcnow() + timedelta(seconds=timeout_s)))
            approval_id = approval["id"]
            event = {"approval_id": approval_id, "tool": tool.name, "arguments": args, "side_effect": tool.side_effect,
                     "description": tool.description, "node_id": node_id, "expires_at": approval["expires_at"],
                     "conversation_id": conversation_id}
            self.bus.publish("approval.requested", event, user_id=user_id)
            if emit:
                await emit("approval_required", event)
            decision = await self._await_approval(approval, timeout_s)
            if decision is not True:
                await self.repos.tools.start_execution(
                    approval_id=approval_id, user_id=user_id, tool_name=tool.name, arguments=args, node_id=node_id,
                    conversation_id=conversation_id, message_id=message_id, side_effect=tool.side_effect,
                    status="denied")
                why = "The user denied this action." if decision is False else "The approval request expired."
                return {"ok": False, "error": why}
        exec_id = await self.repos.tools.start_execution(
            approval_id=approval_id, user_id=user_id, tool_name=tool.name, arguments=args, node_id=node_id,
            conversation_id=conversation_id, message_id=message_id, side_effect=tool.side_effect)
        result = await self._run(tool, parsed, args, node_id, approval_id, user_id, conversation_id)
        await self.repos.tools.finish_execution(exec_id, result["ok"], result.get("output"), result.get("error"))
        if tool.audit:
            await self.repos.audit.add("model", user_id, "tool.executed", target_type="tool", target_id=tool.name,
                                       outcome="success" if result["ok"] else "failure",
                                       details={"execution_id": exec_id, "node_id": node_id,
                                                "side_effect": tool.side_effect})
        return result

    async def _run(self, tool: ToolDefinition, parsed: Any, raw_args: dict[str, Any], node_id: str | None,
                   approval_id: str | None, user_id: str, conversation_id: str | None) -> dict[str, Any]:
        if tool.location == "coordinator":
            ctx = ToolContext(user_id=user_id, conversation_id=conversation_id, services=self.services)
            try:
                out = await asyncio.wait_for(tool.handler(ctx, parsed), timeout=tool.timeout_s)
                return {"ok": True, "output": out}
            except TimeoutError:
                return {"ok": False, "error": f"Tool timed out after {tool.timeout_s:.0f}s."}
            except Exception as exc:
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        spec = JobSpec("tool.exec", {"tool": tool.name, "arguments": raw_args, "approval_id": approval_id},
                       TaskRequirements("tool.exec", "interactive", needs_model=False, pinned_node_id=node_id),
                       priority="interactive", user_id=user_id, conversation_id=conversation_id)
        try:
            handle = await self.jobs.run_to_completion(spec, timeout_s=tool.timeout_s + 15)
        except Exception as exc:
            return {"ok": False, "error": getattr(exc, "message", str(exc))}
        if handle.status != "completed" or not handle.result:
            return {"ok": False, "error": handle.error or f"tool job {handle.status}"}
        res = handle.result
        return {"ok": bool(res.get("ok")), "output": res.get("output"), "error": res.get("error"),
                "node": handle.node_name}


def tool_result_text(result: dict[str, Any]) -> str:
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "… (truncated)"
    return text
