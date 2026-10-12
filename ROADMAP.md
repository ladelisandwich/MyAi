# Roadmap

What exists today is listed with its validation status in [docs/MILESTONES.md](docs/MILESTONES.md).
The items below are planned, in rough priority order. Nothing here is implemented unless it says so.

## Next (0.2)

* **Validation on real hardware and platforms.** Run the documented procedures on an RTX 3080
  machine (VRAM detection, GPU placement, game-busy back-off), a real Bazzite install (native and
  Quadlet with CDI), a Windows 10/11 PC (installer, autostart entry, CIM fallback) and two physical
  machines on a LAN. Fix what that finds.
* **Prebuilt release archives** that include the web UI, so installation needs no Node.js.
* **Container image build in CI**, with a smoke test of the compose stack.
* **OCR** for scanned PDFs and images (Tesseract or a vision model as an optional job type), replacing
  the current `needs_ocr` status.
* **More document types:** DOCX, HTML, EPUB, email exports.
* **Vector index acceleration** (`sqlite-vec` or HNSW) for collections beyond ~100k chunks; exact search
  stays the default below that.
* **Model download policies:** allowed registries/models per node, disk-space checks before pulls.

## Later

* **Voice:** speech-to-text and text-to-speech as jobs on workers (design in
  [docs/VOICE_AND_VISION.md](docs/VOICE_AND_VISION.md)).
* **Splitting large models across computers** via llama.cpp RPC over authenticated tunnels, opt-in per
  model (design in [docs/DISTRIBUTED_INFERENCE.md](docs/DISTRIBUTED_INFERENCE.md)).
* **Multiple users / household profiles** with isolated memory and conversations (the schema already
  carries `user_id`).
* **Encryption at rest** for the coordinator database (SQLCipher or field-level encryption with a key
  unlocked at login).
* **Scheduled tasks and agents:** recurring jobs (e.g. "summarise new notes each morning") with the same
  approval model as tools.
* **More tools:** calendar/notes integrations through local files or CalDAV, a sandboxed code runner
  (container-isolated, approval-gated), web search via a self-hosted engine (opt-in).
* **Optional cloud models** behind the existing `[cloud]` switch with per-request confirmation and
  clear labelling.
* **Native mobile wrapper** if the PWA proves limiting (notifications, background audio).
* **Coordinator high availability:** a standby coordinator with database replication.
* **Wake-on-LAN** for sleeping workers when a job needs them (opt-in per node).

## Explicit non-goals

* Telemetry or usage reporting of any kind.
* Exposing HomeMesh or model servers directly to the internet.
* A shell/command tool that runs model-chosen commands without per-call review.
