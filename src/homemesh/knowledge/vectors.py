"""Exact cosine-similarity vector index (numpy) and reciprocal-rank fusion.

Exact search over a normalised float32 matrix is simple, deterministic and fast enough for a
personal knowledge base: ~100k chunks x 768 dims is ~300 MB and a query takes tens of
milliseconds. Larger deployments can swap in an ANN index (e.g. sqlite-vec) behind the same
interface - see ROADMAP.md.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np


class VectorIndex:
    def __init__(self, ids: list[str], matrix: np.ndarray | None) -> None:
        self.ids = ids
        if matrix is None or matrix.size == 0:
            self.matrix = None
        else:
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self.matrix = (matrix / norms).astype(np.float32, copy=False)
        self._pos = {i: n for n, i in enumerate(ids)}

    @property
    def dim(self) -> int | None:
        return None if self.matrix is None else int(self.matrix.shape[1])

    def __len__(self) -> int:
        return len(self.ids)

    def search(self, query: list[float], k: int = 20, allowed: set[str] | None = None) -> list[tuple[str, float]]:
        if self.matrix is None:
            return []
        q = np.asarray(query, dtype=np.float32)
        if q.shape[0] != self.matrix.shape[1]:
            raise ValueError(f"query has {q.shape[0]} dimensions but the index has {self.matrix.shape[1]}; "
                             "the embedding model changed - re-embed the collection")
        n = np.linalg.norm(q)
        if n == 0:
            return []
        scores = self.matrix @ (q / n)
        if allowed is not None:
            mask = np.fromiter((i in allowed for i in self.ids), dtype=bool, count=len(self.ids))
            scores = np.where(mask, scores, -np.inf)
        k = min(k, len(self.ids))
        if k <= 0:
            return []
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(self.ids[i], float(scores[i])) for i in top if np.isfinite(scores[i])]


def rrf(rankings: Iterable[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion of several ranked id lists."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda kv: -kv[1])
