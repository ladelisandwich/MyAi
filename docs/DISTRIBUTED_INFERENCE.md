# Distributed inference: what HomeMesh does and doesn't do

"Use all my computers" can mean two different things:

1. **Request-level distribution:** each request runs entirely on *one* computer, and different
   requests (or different kinds of work) run on different computers. **This is implemented**: chat on
   the gaming PC's GPU, document embedding on the home server, scanning on the laptop that holds the
   files.
2. **Model splitting:** one model too large for any single computer runs *across* several, with layers
   or tensors split over the network. **This is not implemented** in v0.1.0. This document explains why
   and how it could be added.

## What's implemented (request-level)

* The scheduler places each job on the best single computer that can run it (see
  [ARCHITECTURE.md](../ARCHITECTURE.md#scheduler)): free VRAM after reserves, loaded models, mode,
  concurrency, GPU busy with games, capabilities.
* Different work runs in parallel on different computers. A long indexing job on one machine doesn't
  slow chat on another.
* Embeddings for a collection can run on a different computer from the one that scanned it.
* Failover between computers is *request-level*: if a worker disappears, the next request goes
  elsewhere. Idempotent jobs (embedding, scanning) are retried automatically; chat replies are not,
  because that could duplicate tool side effects, and the user sees a clear error instead.

## Why model splitting is not implemented (yet)

| Concern | Detail |
|---|---|
| Backend support | Ollama (the primary backend) can't split a model across machines. llama.cpp has an **RPC backend** (`rpc-server` + `--rpc host:port`) that offloads layers to remote machines, but it's documented upstream as experimental/proof-of-concept, has **no authentication or encryption**, and must not be exposed on a network. vLLM supports pipeline/tensor parallelism across nodes via Ray, aimed at datacenter interconnects. |
| Network | Pipeline-parallel inference sends activations between machines for every generated token. On gigabit Ethernet (~110 MB/s, ~0.2–1 ms latency) and especially Wi-Fi, per-token latency rises sharply. For a 2–3 GPU home setup, a model that fits on one GPU (quantised) is usually faster than a larger model split over the LAN. |
| Reliability | If any participating machine sleeps, starts a game or reboots, the whole generation fails. Home PCs aren't dedicated servers. |
| Security | The RPC protocols involved assume a trusted, isolated network. HomeMesh would have to tunnel them through its authenticated TLS channel. |
| Scheduling | It would need *co-scheduling*: reserving memory on several nodes at once, deciding layer splits from measured VRAM and bandwidth, and handling partial failures. |

## Design for a future implementation

The codebase is structured so this can be added without changing the user-facing model:

1. **New backend type `llamacpp_rpc`** on the *coordinating* worker (the node with the most VRAM).
   It starts `llama-server --rpc <peers>` with the model and talks to it like any OpenAI-compatible
   backend.
2. **New job type `rpc.serve`** dispatched to helper workers: start `rpc-server` bound to
   **127.0.0.1 only**, with a memory cap derived from the scheduler's reservation. The worker reports
   readiness and actual memory.
3. **Transport:** helper ↔ primary traffic goes through a mutually authenticated TLS tunnel
   established by the workers (each already holds a coordinator-issued identity; the coordinator would
   issue short-lived peer certificates for the session). The RPC port is never exposed directly.
4. **Scheduler extension:** `schedule_group(request, nodes)` → a primary plus helpers, chosen when no
   single node fits the model, with a penalty for network hops, and only when the owner has allowed
   multi-node models for that model (opt-in per model, off by default).
5. **Lifecycle:** the group is a parent job with child jobs. If any child fails, the parent fails
   cleanly and all helpers release memory. Groups are torn down after an idle timeout.
6. **Measurement first:** a benchmark mode (`scripts/bench.py --split`) would measure tokens/s split
   vs. single-node so the UI can show the trade-off honestly before the user enables it.

**Acceptance criteria for the feature:** a model that doesn't fit on any single node runs across two
nodes; traffic between nodes is encrypted and authenticated; killing a helper fails the request
within the ack/heartbeat timeouts and frees memory on the others; measured tokens/s and TTFT are
shown next to the single-node alternative.
