# Vision and voice

## Vision (images): implemented, routing tested with the mock backend

* Attach an image (PNG, JPEG, WebP, GIF, recognised by its content) in the chat composer, or `POST /api/v1/attachments` and pass
  `attachment_ids` when sending a message.
* The scheduler then requires the **`vision`** capability. Only models whose backend reports it
  (Ollama's `capabilities` list) or that are declared with it (`declared_models` for OpenAI-compatible
  servers) are eligible. The **Vision** model role picks the default.
* If no vision-capable model is online, the request fails with a clear error. HomeMesh never strips
  the image and sends the text to a text-only model.
* Images are sent base64-encoded inside the job to the chosen worker and stored on the coordinator in
  `data/attachments/`.
* **Verification status:** routing (only to vision models), storage and the error path are covered by
  integration tests using the mock backend (`test_image_routes_only_to_vision_model`). A real vision
  model (e.g. `ollama pull llava` or `qwen2.5vl`) has **not** been run end-to-end in this project's
  CPU-only development environment. To check it yourself: pull a vision model, attach a photo, and
  confirm the reply describes it and the route shows that model.

Not yet: image generation, OCR of scanned PDFs (they're marked `needs_ocr`), video.

## Voice: designed, not implemented

The model roles **Speech-to-text** and **Text-to-speech** exist in settings and the Models page
(shown as "not available yet") so the data model is ready. No STT or TTS engine is integrated in
v0.2.0.

### Planned design

| Piece | Plan |
|---|---|
| Capture | The PWA records audio with `MediaRecorder` (Opus/WebM) after an explicit tap; the CSP and `Permissions-Policy: microphone=(self)` already allow this for the HomeMesh origin only. No always-listening wake word in the first version. |
| Transport | Upload as an attachment, or stream chunks over a WebSocket for lower latency. |
| STT job | New job type `audio.transcribe` with capability `stt`. Worker adapter options: **whisper.cpp** `whisper-server` (OpenAI-compatible `/v1/audio/transcriptions`) or **faster-whisper**; both run fully locally on CPU or GPU. The scheduler places it like any job (VRAM-aware). |
| Chat | The transcript becomes the user message (shown and editable before sending, in a "review" mode). |
| TTS job | New job type `audio.speak` with capability `tts`. Local engines such as **Piper** (fast on CPU) produce audio that streams back to the browser. Sentence-level chunking lets speech start before the whole reply finishes. |
| Privacy | Audio never leaves your computers. Recordings are deleted after transcription unless the user chooses to keep them. |
| Capabilities | Declared per backend like OpenAI-compatible models (`declared_models = { "whisper-small" = ["stt"] }`), since these servers don't self-describe. |

**Acceptance criteria:** a spoken question from a phone is transcribed on a worker with an STT model,
answered, and spoken back; the route shows which computer did each step; with no STT model online the
microphone button explains why it's unavailable instead of failing silently.
