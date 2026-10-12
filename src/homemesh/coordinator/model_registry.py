"""Model capability registry: what is installed where, and which model serves which role.

Models are *not* treated as interchangeable. Each instance carries the capabilities its
backend reported (or the owner declared); a model is never advertised with tools/vision
support it does not have. Model roles (chat, embedding, vision, ...) are owner settings.
"""

from __future__ import annotations

from typing import Any

from homemesh.coordinator.db.repositories import Repositories
from homemesh.coordinator.nodes import NodeManager
from homemesh.coordinator.settings_service import MODEL_ROLES, SettingsService
from homemesh.protocol.models import CAP_COMPLETION, CAP_EMBEDDING, CAP_VISION
from homemesh.scheduler.policy import model_names_match

ROLE_CAPABILITY = {"chat": CAP_COMPLETION, "reasoning": CAP_COMPLETION, "coding": CAP_COMPLETION,
                   "fast": CAP_COMPLETION, "embedding": CAP_EMBEDDING, "vision": CAP_VISION}


class ModelRegistry:
    def __init__(self, repos: Repositories, nodes: NodeManager, settings: SettingsService, *,
                 allow_mock: bool, allow_remote: bool) -> None:
        self.repos = repos
        self.nodes = nodes
        self.settings = settings
        self.allow_mock = allow_mock
        self.allow_remote = allow_remote

    async def list(self) -> list[dict[str, Any]]:
        node_rows = {n["id"]: n for n in await self.repos.nodes.list()}
        roles = await self.settings.section("model_roles")
        groups: dict[str, dict[str, Any]] = {}
        for row in await self.repos.nodes.list_models():
            node = node_rows.get(row["node_id"])
            if node is None:
                continue
            info = row["info_json"]
            conn = self.nodes.get(row["node_id"])
            live = next((m for m in conn.models if m.name == row["name"] and m.backend == row["backend"]), None) \
                if conn else None
            if live is not None:
                info = live.model_dump()
            g = groups.setdefault(row["name"], {
                "name": row["name"], "capabilities": info.get("capabilities", []),
                "capability_source": info.get("capability_source"), "family": info.get("family"),
                "parameter_size": info.get("parameter_size"), "quantization": info.get("quantization"),
                "context_length": info.get("context_length"), "embedding_length": info.get("embedding_length"),
                "size_bytes": info.get("size_bytes"), "remote": info.get("remote", False),
                "mock": info.get("mock", False), "instances": [], "warnings": [],
                "roles": [r for r in MODEL_ROLES if roles.get(r) and model_names_match(roles[r], row["name"])],
            })
            if sorted(g["capabilities"]) != sorted(info.get("capabilities", [])):
                g["warnings"].append(f"Capabilities differ between nodes (node {node['name']} reports "
                                     f"{info.get('capabilities')}).")
            for w in info.get("warnings", []):
                if w not in g["warnings"]:
                    g["warnings"].append(w)
            g["instances"].append({
                "node_id": row["node_id"], "node_name": node["name"], "online": conn is not None,
                "backend": row["backend"], "backend_type": row["backend_type"], "loaded": info.get("loaded", False),
                "loaded_size_bytes": info.get("loaded_size_bytes"), "loaded_vram_bytes": info.get("loaded_vram_bytes"),
                "loaded_context_length": info.get("loaded_context_length"),
                "estimated_memory_bytes": info.get("estimated_memory_bytes"),
            })
        out = sorted(groups.values(), key=lambda g: (g["mock"], g["name"]))
        for g in out:
            g["available"] = any(i["online"] for i in g["instances"]) and (self.allow_mock or not g["mock"]) and (
                self.allow_remote or not g["remote"])
            if g["remote"] and not self.allow_remote:
                g["warnings"].insert(0, "Cloud model: disabled because cloud use is off (inference would leave "
                                        "your network).")
        return out

    async def models_with(self, capability: str) -> list[dict[str, Any]]:
        return [m for m in await self.list() if capability in m["capabilities"] and m["available"]]

    async def role_model(self, role: str) -> str | None:
        """The configured model for a role, or an automatic choice for roles that need one."""
        roles = await self.settings.section("model_roles")
        if roles.get(role):
            return roles[role]
        if role == "embedding":
            candidates = await self.models_with(CAP_EMBEDDING)
            real = [m for m in candidates if not m["mock"]]
            pick = (real or candidates or [None])[0]
            return pick["name"] if pick else None
        return None

    async def model_info(self, name: str) -> dict[str, Any] | None:
        for m in await self.list():
            if model_names_match(name, m["name"]):
                return m
        return None
