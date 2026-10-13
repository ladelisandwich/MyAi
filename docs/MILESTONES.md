# Milestones, acceptance criteria and validation

This is the honest status of HomeMesh v0.1.0: what's implemented, how each acceptance criterion was
validated, and what has **not** been verified yet. Re-run the procedures below after changes.

**Legend:**
* ✅ **auto**: covered by automated tests that run in CI (Linux and Windows).
* ✅ **real**: additionally verified against real software (a real Ollama, real processes and sockets, a real browser).
* ⚠️ **unverified**: implemented, but not run in the conditions that matter (no hardware or platform available).
* ❌: not implemented (designed only).

## Development environment and its limits

All work was done in a Linux container: 4 vCPU, 15 GB RAM, **no GPU**, no container daemon,
no Windows machine, one host. Real inference used the official **Ollama 0.40.3 CPU build** with
`smollm2:135m` (chat) and `all-minilm` (embeddings). CI runs on GitHub-hosted Ubuntu (Python 3.11,
3.13, 3.14) and **Windows** (Python 3.13, 3.14) runners. A second workflow (`install.yml`) runs the real
installers: `install.ps1` under Windows PowerShell 5.1, `install.sh` and `install-ollama.sh` as a normal
user inside the official `bazzite-nvidia-open` image (with a Quadlet dry-run using its Podman), and the
container image build/run.

Consequences:
* GPU behaviour (VRAM detection, GPU placement, game-busy back-off) is verified with captured
  `nvidia-smi`/sysfs/CIM outputs and scheduler unit tests, **not on a real RTX 3080**.
* Multi-node is verified with real processes and real TLS on **one host** (separate data folders and
  identities), not across physical machines on a LAN.
* Windows: the test suite, the PowerShell installer (install, autostart entry, setup, start, web UI,
  chat, upgrade while running, stop, uninstall) run on GitHub's Windows Server runners. Not yet on a
  Windows 11 desktop: the firewall prompt, Smart App Control, the CIM GPU query with real AMD/NVIDIA cards.
* Containers: the image builds and runs (health, non-root, UI, chat via API) in CI. The NVIDIA compose
  overlay and the Quadlets with a real GPU have not been run.

## Validation commands

```bash
ruff check . && pytest -q                       # unit + integration (mock backend, real sockets): 170 tests
pytest -m smoke                                 # real Ollama (see tests/smoke/test_real_ollama.py)
cd apps/web && npm run typecheck && npm test && npm run build
python scripts/bench.py --runs 5 --model <model>     # against a running coordinator
```

---

## M1: Single computer (standalone)

| Acceptance criterion | Status | Evidence |
|---|---|---|
| Install as a normal user; `homemesh setup` creates config and owner; `homemesh start` runs it | ✅ real | `deploy/linux/install.sh` run under a throw-away HOME; the installed wheel contains the web UI and migrations. CI runs non-interactive setup → start → status → stop on Linux **and Windows**. |
| Web UI: sign in, chat with streaming, see which model/computer answered | ✅ real | Playwright (headless Chromium): login, chat, Dashboard/Nodes/Models screenshots, mobile viewport without horizontal overflow, no console errors. |
| Real local inference via Ollama with recorded provenance | ✅ real | `tests/smoke`: `test_real_local_inference_streams_and_is_recorded`. The stored message has `backend=ollama`, `mock=false`, the node, and real token counts. Last run 2026-10-12: 2/2 passed. Measured on 4 vCPU (smollm2:135m): TTFT 3.3 s incl. 2.8 s model load, 48.5 tokens/s, dispatch overhead 3.9 ms. |
| Mock backend never presented as real | ✅ auto | `test_mock_backend_is_clearly_labelled`, `test_real_model_beats_mock`, `test_remote_and_mock_models_excluded_unless_allowed`; UI shows a MOCK badge and the status page a warning. |
| Conversations persist across restarts; interrupted replies are marked, not lost | ✅ auto + real | `test_restart_preserves_data_and_workers_reconnect`; real restart verified with Ollama (reply survived). |
| Cancel mid-stream keeps partial text | ✅ auto | `test_cancel_mid_stream_keeps_partial_text`, `test_job_cancel_via_jobs_api` |
| Backend failure is reported clearly and doesn't corrupt history | ✅ auto | `test_backend_failure_is_reported_without_corrupting_history`, `test_ollama_unreachable_is_actionable`, `test_ollama_errors_are_classified` |
| Hardware detection never invents values (e.g. RTX 3080 VRAM read from the driver) | ✅ auto (captured output) · ⚠️ unverified on real GPU | `test_nvidia_rtx3080_parsing_uses_reported_vram`, `test_windows_gpu_parsing_uses_registry_memory_not_capped_wmi`, `test_sysfs_*`, `test_live_detection_never_fabricates` |

## M2: Multiple computers

| Acceptance criterion | Status | Evidence |
|---|---|---|
| Pair a worker with a one-time code; bad, reused and expired codes rejected | ✅ auto | `test_enrollment_rejects_bad_reused_and_expired_codes`, `test_pairing_codes_are_random_and_normalised` |
| Worker verifies coordinator identity by CA fingerprint; mismatch aborts | ✅ auto + real | `test_tls_ca_pinning_and_hostname_verification`; two-process check: wrong fingerprint refused with impersonation warning, missing fingerprint refused, correct one accepted. |
| No plain-HTTP credentials to a remote host by default | ✅ auto | `test_insecure_http_join_to_remote_host_is_refused`, `test_lan_without_tls_is_refused` |
| Invalid, revoked and duplicate worker identities are rejected/disconnected | ✅ auto | `test_invalid_node_credentials_rejected`, `test_revoked_node_is_disconnected_and_cannot_return` |
| A worker can't report on another worker's job | ✅ auto | `test_worker_cannot_inject_output_into_another_nodes_job` |
| Heartbeat loss marks a node offline; the worker reconnects | ✅ auto | `test_missed_heartbeats_mark_node_offline_then_worker_reconnects`, `test_worker_restart_rejoins_with_same_identity` |
| Worker crash mid-reply → clear error; next request goes to another computer | ✅ auto + real | `test_worker_crash_mid_stream_fails_clearly_and_next_request_reroutes`; real: SIGKILL of a TLS-joined worker process → node offline immediately, clear error, next chat routed to the other worker. |
| Routing respects models, capabilities, manual node/model choice (never silently re-routed) | ✅ auto | `test_routing_respects_models_and_manual_node_choice`, `test_manual_node_pin_is_honoured_or_fails_without_rerouting`, `test_requested_model_that_does_not_exist_fails_clearly` |
| Different machines on a real LAN | ⚠️ unverified | Same code path as the one-host test; needs two physical machines. Procedure V2 below. |

## M3: Memory and documents

| Acceptance criterion | Status | Evidence |
|---|---|---|
| "Remember that…" saves a memory with provenance, used in later conversations | ✅ auto | `test_explicit_memory_is_saved_and_used_across_conversations`, `test_explicit_memory_detection` |
| Memories are viewable, editable, deletable; automatic suggestions need approval | ✅ auto | `test_memory_management_api`, `test_candidate_parser_rejects_guesses_and_secrets` |
| Duplicates aren't stored twice | ✅ auto | `test_normalisation_and_dedupe` |
| Index a folder on one computer, embed on another, answers cite file and lines | ✅ auto | `test_scan_on_one_node_embed_on_another_and_cited_answers` |
| Real embeddings power semantic search | ✅ real | `test_real_embeddings_power_document_search` (all-minilm via Ollama; hybrid mode, correct top result) |
| Search works (keyword) without an embedding model, and says so | ✅ auto | `test_keyword_search_works_without_embedding_model` |
| Incremental re-scan; scanned PDFs flagged `needs_ocr` | ✅ auto | `test_scanner_incremental_and_safe`, `test_pdf_text_and_image_only` |
| No access outside allowed folders, no symlink escape, sensitive files refused | ✅ auto | `test_scanner_refuses_unauthorised_root`, `test_traversal_and_symlink_escape_blocked` (skipped where the OS forbids symlinks), `test_sensitive_paths`, `test_refuses_disk_root_and_home`, `test_collection_root_must_be_inside_allowed` |
| Retrieved content is treated as untrusted data in the prompt | ✅ auto | `test_system_prompt_contains_rules_preferences_and_marks_data_untrusted`, `test_documents_and_memories_have_budgets` |

## M4: Models, scheduling, resource modes, metrics

| Acceptance criterion | Status | Evidence |
|---|---|---|
| A model is only placed where it fits in free VRAM minus reserve (or CPU when allowed) | ✅ auto | `test_model_bigger_than_free_vram_is_not_assumed_to_fit`, `test_partial_offload_when_cpu_allowed`, `test_insufficient_memory_everywhere_gives_reasons`, `test_memory_estimate_is_conservative` |
| Loaded models preferred; GPU nodes preferred over CPU | ✅ auto | `test_loaded_model_wins_and_needs_no_memory`, `test_prefers_gpu_node_with_room_over_cpu_node` |
| Modes: paused gets nothing; background only as a last resort for chat; balanced backs off when the GPU is busy with other programs | ✅ auto · ⚠️ GPU-busy unverified with a real game | `test_paused_node_gets_no_work_and_pinned_request_explains`, `test_background_mode_only_last_resort_for_chat`, `test_external_gpu_busy_backs_off_in_balanced_but_not_performance`, `test_profile_concurrency_caps` |
| Capacity limits give a clear "busy" error instead of hanging | ✅ auto | `test_capacity_limits_and_clear_busy_error` |
| Capabilities are never guessed | ✅ auto | `test_missing_capability_is_never_faked`, `test_capability_inference_never_adds_tools_or_vision`, `test_openai_compat_declared_capabilities_and_tool_fragments` |
| Model pull/load/unload/delete run as jobs on the target computer | ✅ auto | `test_model_management_job_runs_on_target_node` |
| Metrics: TTFT, tokens/s, load time, dispatch overhead, indexing and retrieval latency | ✅ auto + real | `test_streaming_chat_is_persisted_with_true_provenance`, `test_status_metrics_and_settings`; `scripts/bench.py` (real numbers in M1) |
| Image attachments only go to vision models | ✅ auto · ⚠️ no real vision model run | `test_image_routes_only_to_vision_model` |

## M5: Tools, security, operations

| Acceptance criterion | Status | Evidence |
|---|---|---|
| Write tools need approval; denied tools don't run; node tools re-checked on the node | ✅ auto | `test_write_tool_requires_approval_and_runs_on_node`, `test_denied_tool_does_not_run`, `test_tools_not_allowed_on_any_node_are_not_offered`, `test_local_policy_enforces_owner_rules` |
| Tool arguments validated; read-only tools need no approval | ✅ auto | `test_tool_arguments_are_validated`, `test_read_only_coordinator_tool_runs_without_approval`, `test_calculator_rejects_unsafe_or_huge` |
| Auth: setup token, sessions, CSRF, scoped tokens, rate limits, security headers, audit | ✅ auto | `tests/integration/test_api_security.py` (10 tests), `test_password_hashing_argon2id` |
| Backups: consistent, checksummed, optionally encrypted; restore is non-destructive; tampering detected | ✅ auto (Linux + Windows) | `test_backup_restore_round_trip`, `test_encrypted_backup_requires_correct_passphrase`, `test_tampered_or_truncated_archives_detected`. Windows CI found and drove the fix for an open-handle bug here. |
| Migrations: backup first, atomic failure, refuse downgrade | ✅ auto | `tests/unit/test_migrations.py` |
| CLI: status/doctor/stop work on every OS, including redirected output on Windows | ✅ auto | CI lifecycle step; `test_stop_request_file_triggers_graceful_stop`, `test_cli_output_survives_legacy_console_encoding`, `test_worker_link_reports_protocol_mismatch` |
| Linux user install + systemd user service | ✅ real (install) · ⚠️ systemd unit not run (no systemd in the container) | Procedure V3 |
| Bazzite native and Quadlet with NVIDIA CDI | ✅ auto in the real `bazzite-nvidia-open` image (Python 3.14.8): install.sh, install-ollama.sh, a real CPU chat through Ollama, upgrade while running, uninstall; Quadlet dry-run with its Podman 5.8.7 · ⚠️ not yet on a Bazzite PC with a GPU or under systemd | `install.yml` job *Bazzite*; Procedure V4 |
| Windows installer and autostart entry | ✅ auto (Windows Server CI, PowerShell 5.1, Python 3.13 + 3.14) · ✅ real: Windows 11 desktop (2026-10-12, Python 3.13): install, setup, HTTPS on the LAN, autostart, chats through Ollama. That run found the flashing-console bug (fixed) and an Ollama install missing `llama-server.exe` (Ollama-side) | `install.yml` job *Windows installer*; Procedure V5 |
| Container image | ✅ auto (build, run, health, non-root, UI, chat) · ⚠️ GPU overlay not run | `install.yml` job *Container image*; Procedure V6 |
| Package layering rules | ✅ auto | `test_layering`, `test_os_specific_code_isolated` |
| Windows desktop robustness: one instance per data folder, PID reuse, log rotation with readers, sleep/wake, OneDrive placeholders and junctions, GPU query encoding, ACLs outside the profile, port check, console code page | ✅ auto (most also on the Windows CI runners: real junction, real ACL check under `RUNNER_TEMP`, real port conflict) · ⚠️ logoff/sleep and OneDrive behaviour simulated in tests, not observed on the user's PC | `test_only_one_homemesh_per_data_folder`, `test_second_instance_exits_before_touching_the_database`, `test_a_reused_pid_is_never_taken_for_homemesh`, `test_log_rotation_waits_while_the_file_cannot_be_renamed`, `test_sleep_gap_gives_workers_a_fresh_timeout_but_dead_workers_still_go`, `test_scanner_skips_cloud_only_files_and_links_out_of_the_folder`, `test_windows_gpu_query_survives_non_ascii_output`, `test_windows_acl_really_excludes_other_users`, `test_doctor_port_check_sees_a_busy_port`, `test_piped_output_on_windows_uses_the_console_code_page` |
| Internet tools: private/local addresses, the computer's own networks (incl. global IPv6) and non-web ports refused (incl. redirects, mixed DNS answers, IPv4-in-IPv6, numeric IPv4 forms), compressed pages inflated within the size limit, HTTPS verified against the host name while connecting to the checked IP, size limit, SearXNG JSON and error handling, approval for addresses not supplied by the user or a search result, per-answer limits, page links always asking, one deadline per page, `open_url` approval and worker re-check | ✅ auto | `tests/unit/test_web_tools.py` (incl. a gzip-bomb memory check), `tests/integration/test_web_tools.py` |
| Web search against a real SearXNG, real page fetches | ✅ real (dev environment, 2026-10-13): SearXNG built from the same source revision as the pinned image, started with the installer's `settings.yml` (`/healthz` 200, JSON on); `homemesh web test` returned real results; `fetch_url` read python.org, docs.python.org, Wikipedia and Hacker News (that environment intercepts HTTPS, so its CA was trusted for that one check); cloud-metadata and localhost addresses refused | — |
| SearXNG Quadlet with rootless Podman | ✅ auto: `install.yml` job *SearXNG* runs `install-searxng.sh` under a systemd user manager on Ubuntu (Podman 4.9), searches through it, checks loopback-only, non-root and file ownership; Quadlet dry-run with Bazzite's Podman · ⚠️ not yet on the Bazzite desktop | `install.yml` |
| `open_url` opens the browser on a real Windows desktop | ⚠️ unit-tested with the Windows code path simulated; not yet run on the Windows hub | — |

## Not implemented (designed)

* ❌ Voice (STT/TTS): [VOICE_AND_VISION.md](VOICE_AND_VISION.md)
* ❌ Splitting one model across computers: [DISTRIBUTED_INFERENCE.md](DISTRIBUTED_INFERENCE.md)
* ❌ OCR, multi-user accounts, encryption at rest: [ROADMAP.md](../ROADMAP.md)

---

## Manual validation procedures

### V1: Real GPU (e.g. RTX 3080)

1. `nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv`. Note the values.
2. `homemesh doctor` → the GPU line must show the same name and total VRAM.
3. Nodes page → the computer shows the GPU, free VRAM, utilisation and temperature updating every ~10 s.
4. `ollama pull llama3.1:8b`; chat with it. The route shows placement `gpu`; `ollama ps` shows `100% GPU`;
   Models shows the model as loaded with its VRAM.
5. Start a GPU-heavy game with the node in **balanced** mode. Once utilisation exceeds the threshold, a
   new chat on that node fails over to another node or reports `gpu_busy_external`. In **performance**
   mode it's accepted.
6. Pull a model larger than free VRAM (e.g. a 14B+ at Q8). With `allow_cpu_inference=false` the request
   is refused with the size comparison; with it enabled the placement is `partial`.
7. `python scripts/bench.py --runs 5 --model llama3.1:8b` and record TTFT and tokens/s.

### V2: Two physical computers

1. Coordinator: `homemesh setup --mode coordinator` (TLS), note URL and fingerprint; `homemesh node pair`.
2. Second computer: install, `homemesh worker join --coordinator https://<ip>:8765 --code … --fingerprint …`,
   `homemesh start`. It appears online within seconds with its hardware.
3. Enter a wrong fingerprint on a third attempt: join must refuse.
4. In a chat, choose the second computer in the "Computer" selector (instead of Auto). The route shows it.
5. Unplug the second computer's network mid-reply: the reply ends with a clear error within the heartbeat
   timeout; the next request goes to the coordinator's worker.
6. Remove the computer on the Computers page (this revokes its credential): its connection drops and it can't reconnect.

### V3: Linux service

`homemesh service install && homemesh start`, `systemctl --user status homemesh`, reboot (with
`loginctl enable-linger` for headless use), check that `homemesh status` reports it running, then
`homemesh stop` and `homemesh service uninstall`.

### V4: Bazzite

Follow [deploy/bazzite/README.md](../deploy/bazzite/README.md) "Validating the NVIDIA GPU" for the
native install and for the Quadlets (`systemctl --user status ollama homemesh`, `podman exec ollama nvidia-smi`).

### V5: Windows

1. Fresh user account, Python from python.org, Ollama for Windows, `ollama pull llama3.2:3b`.
2. `powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -AutoStart`; open a new terminal.
3. `homemesh setup` (standalone), `homemesh start`, open the UI, chat.
4. `homemesh doctor`: GPU and VRAM match Task Manager → Performance → GPU (dedicated memory).
5. Sign out and in: HomeMesh starts automatically (`homemesh status`).
6. `homemesh stop` stops it within seconds. Run `uninstall.ps1`: program and PATH entry removed, data kept.

### V6: Containers

`docker compose -f deploy/containers/compose.yaml up -d --build`, create the owner, pull a model inside
the Ollama container (`docker compose exec ollama ollama pull llama3.2:3b`), chat. Verify that port 11434 is
**not** reachable from another machine and 8765 only on 127.0.0.1. Repeat with the NVIDIA overlay on a GPU host.
