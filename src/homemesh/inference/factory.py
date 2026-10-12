"""Build backend instances from configuration."""

from __future__ import annotations

from homemesh.common.config import HomeMeshConfig
from homemesh.inference.base import InferenceBackend
from homemesh.inference.mock import MockBackend
from homemesh.inference.ollama import OllamaBackend
from homemesh.inference.openai_compat import OpenAICompatBackend


def build_backends(cfg: HomeMeshConfig) -> dict[str, InferenceBackend]:
    backends: dict[str, InferenceBackend] = {}
    for b in cfg.inference.backends:
        if not b.enabled:
            continue
        if b.type == "ollama":
            backends[b.name] = OllamaBackend(
                b.name, b.url, timeout_s=b.timeout_s,
                allow_remote_models=b.allow_remote_models and cfg.cloud.enabled,
            )
        elif b.type == "openai_compat":
            backends[b.name] = OpenAICompatBackend(
                b.name, b.url, declared_models=b.declared_models, declared_memory_mb=b.declared_memory_mb,
                api_key_env=b.api_key_env, timeout_s=b.timeout_s,
            )
        elif b.type == "mock":
            if cfg.inference.mock_enabled:
                backends[b.name] = MockBackend(b.name)
    if cfg.inference.mock_enabled and not any(isinstance(x, MockBackend) for x in backends.values()):
        backends["mock"] = MockBackend("mock")
    return backends
