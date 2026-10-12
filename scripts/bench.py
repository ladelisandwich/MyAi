#!/usr/bin/env python3
"""Benchmark a running HomeMesh coordinator through its public API.

Measures (only values that are actually reported or timed are shown):
  * chat: client-side end-to-end latency and time-to-first-delta, plus the worker-reported
    TTFT, tokens/s, model load time and coordinator dispatch overhead
  * knowledge: indexing wall time for a folder and retrieval (search) latency

Usage (on the coordinator computer, uses the local CLI token):
  python scripts/bench.py --runs 5 --model llama3.2:3b
  python scripts/bench.py --index ~/Documents/notes --queries "wifi password" "backup schedule"
Remote: add --url https://host:8765 --token hm_... [--ca-file ca.pem]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from homemesh.cli.client import CoordinatorClient
from homemesh.common.config import load_config
from homemesh.common.paths import default_paths

PROMPT = "In two sentences, explain why the sky is blue."


def _stats(values: list[float]) -> str:
    if not values:
        return "not reported"
    if len(values) == 1:
        return f"{values[0]:.1f}"
    return f"median {statistics.median(values):.1f} (min {min(values):.1f}, max {max(values):.1f}, n={len(values)})"


def bench_chat(client: CoordinatorClient, runs: int, model: str | None) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {k: [] for k in ("e2e_ms", "first_delta_ms", "ttft_ms", "tokens_per_second",
                                                   "load_ms", "dispatch_overhead_ms", "completion_tokens")}
    conv = client.post("/api/v1/conversations", {"title": "benchmark", **({"model": model} if model else {})})
    routes = set()
    for i in range(runs):
        t0 = time.perf_counter()
        first = None
        event = None
        with client._client.stream("POST", f"/api/v1/conversations/{conv['id']}/messages",
                                   json={"content": PROMPT, "use_memory": False, "use_knowledge": False,
                                         "use_tools": False}) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:])
                    if event == "route":
                        routes.add(f"{data['model']} on {data['node_name']} ({data['placement']})"
                                   + (" [MOCK]" if data["mock"] else ""))
                    elif event == "delta" and first is None:
                        first = (time.perf_counter() - t0) * 1000
                    elif event == "error":
                        print(f"run {i + 1}: error {data.get('code')}: {data.get('message')}", file=sys.stderr)
                    elif event == "done":
                        for k, v in (data.get("metrics") or {}).items():
                            if k in out and isinstance(v, int | float):
                                out[k].append(float(v))
        out["e2e_ms"].append((time.perf_counter() - t0) * 1000)
        if first is not None:
            out["first_delta_ms"].append(first)
    client.request("DELETE", f"/api/v1/conversations/{conv['id']}")
    print("Chat routed to: " + ", ".join(sorted(routes)))
    return out


def bench_knowledge(client: CoordinatorClient, folder: Path | None, queries: list[str], node: str | None) -> None:
    if folder:
        nodes = client.get("/api/v1/nodes")
        target = next((n for n in nodes if n["id"] == node or n["name"] == node), None) if node else \
            next((n for n in nodes if n["embedded"]), nodes[0] if nodes else None)
        if not target:
            print("No node available for indexing", file=sys.stderr)
            return
        t0 = time.perf_counter()
        col = client.post("/api/v1/knowledge/collections", {"name": f"bench-{int(t0)}", "node_id": target["id"],
                                                             "root_path": str(folder.expanduser().resolve())})["collection"]
        while True:
            d = client.get(f"/api/v1/knowledge/collections/{col['id']}")
            if d["status"] in ("indexed", "error") and (d["embedded_count"] >= d["chunk_count"] or not d["embedding_model"]):
                break
            time.sleep(0.5)
        took = time.perf_counter() - t0
        print(f"Indexing: {d['doc_count']} documents, {d['chunk_count']} chunks, {d['embedded_count']} embedded "
              f"in {took:.1f}s on {target['name']} (embedding model: {d['embedding_model'] or 'none - keyword only'})")
    lat = []
    for q in queries:
        for _ in range(3):
            res = client.post("/api/v1/knowledge/search", {"query": q, "limit": 5})
            lat.append(res["took_ms"])
        top = res["results"][0]["source"] if res["results"] else "no results"
        print(f"  search {q!r}: {res['mode']} -> {top}")
    if lat:
        print(f"Retrieval latency ms: {_stats(lat)} (includes the query embedding round-trip to a worker)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model")
    ap.add_argument("--index", type=Path, help="folder to index (must be allowed on the chosen node)")
    ap.add_argument("--node", help="node name/id for --index (default: the coordinator's own worker)")
    ap.add_argument("--queries", nargs="*", default=[])
    ap.add_argument("--skip-chat", action="store_true")
    ap.add_argument("--url")
    ap.add_argument("--token")
    ap.add_argument("--ca-file")
    a = ap.parse_args()
    paths = default_paths()
    client = CoordinatorClient.local(load_config(paths), paths, url=a.url, token=a.token, ca_file=a.ca_file)
    if not a.skip_chat:
        r = bench_chat(client, a.runs, a.model)
        print("\nChat metrics (ms unless noted):")
        labels = {"e2e_ms": "end-to-end (client)", "first_delta_ms": "first text at client",
                  "ttft_ms": "time to first token (worker)", "tokens_per_second": "tokens/second",
                  "load_ms": "model load", "dispatch_overhead_ms": "dispatch overhead", "completion_tokens": "output tokens"}
        for k, label in labels.items():
            print(f"  {label:<32} {_stats(r[k])}")
    if a.index or a.queries:
        print()
        bench_knowledge(client, a.index, a.queries or ["test"], a.node)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
