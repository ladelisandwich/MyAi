"""Model capability identification and conservative memory estimation.

Rules (see ARCHITECTURE.md, "Model capability registry"):

* Capabilities reported by the backend (Ollama ``capabilities``) are trusted as-is.
* Without backend metadata we only infer ``completion`` or ``embedding`` from strong
  signals (model family / name). We **never** infer ``tools`` or ``vision`` - a model
  is only advertised with those if the backend reports them or the owner declares them.
* Memory estimates are deliberately pessimistic: a model file that fits in VRAM does not
  mean the loaded model does (KV cache, compute buffers, CUDA context all add overhead).
"""

from __future__ import annotations

from typing import Any

from homemesh.protocol.models import (
    CAP_COMPLETION,
    CAP_EMBEDDING,
    KNOWN_CAPABILITIES,
)

MIB = 1024 * 1024
GIB = 1024 * MIB

EMBEDDING_FAMILIES = {"bert", "nomic-bert", "nomic-bert-moe", "xlm-roberta", "jina-bert-v2", "gte", "e5"}
EMBEDDING_NAME_HINTS = ("embed", "minilm", "bge-", "e5-", "gte-", "arctic-embed")

# Overhead added to the on-disk size when estimating resident memory for a model that is not
# loaded yet. Measured example (Ollama 0.40, smollm2:135m F16, ctx 4096): 270 MB file -> 382 MB
# resident (1.41x). Large quantized models are typically 1.1-1.3x plus KV cache.
SIZE_FACTOR = 1.25
FIXED_OVERHEAD_BYTES = 512 * MIB


def normalize_capabilities(raw: list[str] | None) -> list[str]:
    if not raw:
        return []
    out = []
    for c in raw:
        c = str(c).strip().lower()
        if c in KNOWN_CAPABILITIES and c not in out:
            out.append(c)
    return out


def infer_capabilities(name: str, family: str | None) -> list[str]:
    """Fallback when the backend reports nothing. Only completion/embedding are ever inferred."""
    lname = name.lower()
    fam = (family or "").lower()
    if fam in EMBEDDING_FAMILIES or any(h in lname for h in EMBEDDING_NAME_HINTS):
        return [CAP_EMBEDDING]
    return [CAP_COMPLETION]


def context_length_from_model_info(model_info: dict[str, Any] | None) -> int | None:
    if not model_info:
        return None
    for k, v in model_info.items():
        if k.endswith(".context_length") and isinstance(v, int | float):
            return int(v)
    return None


def embedding_length_from_model_info(model_info: dict[str, Any] | None) -> int | None:
    if not model_info:
        return None
    for k, v in model_info.items():
        if k.endswith(".embedding_length") and isinstance(v, int | float):
            return int(v)
    return None


def estimate_memory_bytes(size_bytes: int | None, loaded_size_bytes: int | None = None) -> int | None:
    """Conservative resident-memory estimate for scheduling.

    If the model is loaded, the backend-reported resident size is used (no guessing).
    Otherwise: ``size * 1.25 + 512 MiB``. Returns ``None`` when the size is unknown, in which
    case the scheduler treats memory fit as unverifiable and says so.
    """
    if loaded_size_bytes:
        return int(loaded_size_bytes)
    if not size_bytes:
        return None
    return int(size_bytes * SIZE_FACTOR + FIXED_OVERHEAD_BYTES)


def parameter_size_to_billions(ps: str | None) -> float | None:
    """'8.0B' -> 8.0, '134.52M' -> 0.13452, '23M' -> 0.023."""
    if not ps:
        return None
    s = ps.strip().upper()
    try:
        if s.endswith("B"):
            return float(s[:-1])
        if s.endswith("M"):
            return float(s[:-1]) / 1000.0
        if s.endswith("K"):
            return float(s[:-1]) / 1_000_000.0
        return float(s) / 1e9
    except ValueError:
        return None
