// Types mirroring the coordinator API (see docs/API.md).

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: string;
  auth?: string;
  scopes?: string[];
}

export interface AuthStatus {
  setup_required: boolean;
  authenticated: boolean;
  user: User | null;
}

export interface Metrics {
  ttft_ms?: number | null;
  total_ms?: number | null;
  load_ms?: number | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  prompt_eval_ms?: number | null;
  eval_ms?: number | null;
  tokens_per_second?: number | null;
  dispatch_overhead_ms?: number | null;
  rounds?: number;
}

export interface Source {
  id: string;
  source: string;
  path?: string;
  collection?: string;
  page?: number | null;
  start_line?: number | null;
  end_line?: number | null;
  score?: number;
  chunk_id?: string;
  retrieval_mode?: string;
}

export interface ToolLogEntry {
  name: string;
  arguments: Record<string, unknown>;
  result: { ok: boolean; output?: unknown; error?: string | null };
}

export interface Message {
  id: string;
  conversation_id: string;
  role: "user" | "assistant" | "system" | "tool";
  content: string;
  thinking?: string | null;
  status: "complete" | "streaming" | "cancelled" | "failed" | "interrupted";
  parent_id?: string | null;
  model?: string | null;
  backend?: string | null;
  backend_type?: string | null;
  node_id?: string | null;
  job_id?: string | null;
  mock?: boolean;
  metrics_json?: Metrics | null;
  sources_json?: Source[] | null;
  context_json?: { memories?: { id: string; content: string; source: string }[]; notes?: string[]; tools_offered?: string[]; history_dropped?: number; num_ctx?: number; memory_saved?: { memory_id: string; content: string; outcome: string } } | null;
  tool_calls_json?: ToolLogEntry[] | null;
  attachments_json?: { id: string; filename: string; mime: string; kind: string; size: number }[] | null;
  error_code?: string | null;
  error?: string | null;
  created_at: string;
}

export interface Conversation {
  id: string;
  title: string;
  project_id?: string | null;
  settings_json?: { model?: string; node_id?: string };
  pinned?: boolean;
  archived?: boolean;
  created_at: string;
  updated_at: string;
  message_count?: number;
  messages?: Message[];
  active_run?: string | null;
}

export interface GPUInfo {
  index: number;
  vendor: string;
  name: string;
  vram_total_mb?: number | null;
  vram_used_mb?: number | null;
  vram_free_mb?: number | null;
  utilization_percent?: number | null;
  temperature_c?: number | null;
  integrated?: boolean | null;
  compute_apis?: string[];
  driver_version?: string | null;
  source?: string;
  unavailable?: Record<string, string>;
  ignored?: boolean;
}

export interface HardwareReport {
  os: { system: string; release: string; distro_name?: string | null; distro_id?: string | null; immutable?: boolean; in_container?: boolean; hostname: string; arch: string; python?: string };
  cpu: { model?: string | null; physical_cores?: number | null; logical_cores?: number | null; max_freq_mhz?: number | null; flags?: string[] };
  memory: { total_mb?: number | null; available_mb?: number | null };
  gpus: GPUInfo[];
  disks?: { path: string; total_mb?: number | null; free_mb?: number | null }[];
  warnings: string[];
  collected_at?: string;
}

export interface Telemetry {
  cpu_percent?: number | null;
  ram_total_mb?: number | null;
  ram_available_mb?: number | null;
  load_avg_1m?: number | null;
  gpus: { index: number; utilization_percent?: number | null; vram_used_mb?: number | null; vram_free_mb?: number | null; vram_total_mb?: number | null; temperature_c?: number | null }[];
  external_gpu_busy?: boolean;
  sampled_at?: string;
}

export interface BackendStatus {
  name: string;
  type: string;
  url?: string;
  ok: boolean;
  version?: string | null;
  error?: string | null;
  mock?: boolean;
  supports_pull?: boolean;
  supports_load_unload?: boolean;
}

export interface ModelInfo {
  name: string;
  backend: string;
  backend_type: string;
  capabilities: string[];
  loaded?: boolean;
  size_bytes?: number | null;
  parameter_size?: string | null;
  mock?: boolean;
  remote?: boolean;
}

export interface NodeSummary {
  id: string;
  name: string;
  status: string;
  embedded: boolean;
  mode: string;
  weight: number;
  os?: string | null;
  arch?: string | null;
  hostname?: string | null;
  software_version?: string | null;
  protocol_version?: number | null;
  created_at: string;
  last_connected_at?: string | null;
  last_seen_at?: string | null;
  last_disconnect_reason?: string | null;
  task_types: string[];
  allowed_roots: string[];
  allowed_tools: string[];
  hardware?: HardwareReport | null;
  limits?: { max_concurrent_jobs?: number; allow_cpu_inference?: boolean; ram_reserve_mb?: number; vram_reserve_percent?: number; gpu_busy_threshold_percent?: number } | null;
  backends?: BackendStatus[] | null;
  online: boolean;
  telemetry?: Telemetry | null;
  running_jobs: string[];
  heartbeat_age_s?: number | null;
  allow_remote_mode_change?: boolean | null;
  models: ModelInfo[];
}

export interface ModelGroup {
  name: string;
  capabilities: string[];
  capability_source?: string;
  family?: string | null;
  parameter_size?: string | null;
  quantization?: string | null;
  context_length?: number | null;
  embedding_length?: number | null;
  size_bytes?: number | null;
  remote: boolean;
  mock: boolean;
  available: boolean;
  roles: string[];
  warnings: string[];
  instances: {
    node_id: string;
    node_name: string;
    online: boolean;
    backend: string;
    backend_type: string;
    loaded: boolean;
    loaded_size_bytes?: number | null;
    loaded_vram_bytes?: number | null;
    loaded_context_length?: number | null;
    estimated_memory_bytes?: number | null;
  }[];
}

export interface Job {
  id: string;
  type: string;
  status: string;
  priority: string;
  node_id?: string | null;
  node_name?: string | null;
  model?: string | null;
  backend?: string | null;
  mock?: boolean;
  error_code?: string | null;
  error?: string | null;
  progress?: number | null;
  progress_message?: string | null;
  attempts: number;
  max_attempts: number;
  metrics_json?: Metrics | null;
  decision_json?: Decision | null;
  created_at: string;
  dispatched_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  conversation_id?: string | null;
  collection_id?: string | null;
  events?: { id: number; ts: string; level: string; kind: string; message: string; data_json?: unknown }[];
  payload_json?: unknown;
  result_json?: unknown;
}

export interface Decision {
  chosen: { node_id: string; node_name: string; model?: string | null; placement: string; score: number; notes: string[] } | null;
  candidates: { node_id: string; node_name: string; model?: string | null; placement: string; score: number; notes: string[] }[];
  rejections: { node_id: string; node_name: string; code: string; message: string; model?: string | null }[];
}

export interface Memory {
  id: string;
  project_id?: string | null;
  kind: string;
  content: string;
  source: "user_explicit" | "user_edited" | "model_inferred" | "imported";
  status: "active" | "candidate" | "rejected" | "archived";
  conversation_id?: string | null;
  pinned: boolean;
  use_count: number;
  last_used_at?: string | null;
  expires_at?: string | null;
  created_at: string;
  updated_at: string;
  score?: number;
}

export interface Project {
  id: string;
  name: string;
  description: string;
  category: string;
  archived: boolean;
  created_at: string;
}

export interface Collection {
  id: string;
  name: string;
  node_id: string;
  node_name?: string;
  node_online?: boolean;
  root_path: string;
  include_globs: string[];
  exclude_dirs: string[];
  status: string;
  error?: string | null;
  doc_count: number;
  chunk_count: number;
  embedded_count: number;
  last_indexed_at?: string | null;
  created_at: string;
  status_counts?: Record<string, number>;
  embedding_model?: string | null;
  documents?: { id: string; rel_path: string; size: number; status: string; error?: string | null; chunk_count: number; page_count?: number | null; indexed_at: string; mime?: string | null }[];
}

export interface SearchHit {
  chunk_id: string;
  collection: string;
  path: string;
  abs_path: string;
  page?: number | null;
  start_line?: number | null;
  end_line?: number | null;
  content: string;
  score: number;
  similarity?: number | null;
  source: string;
  retrieval_mode: string;
}

export interface ToolInfo {
  name: string;
  description: string;
  side_effect: string;
  location: string;
  requires_approval: boolean;
  url_argument?: string | null;
  enabled: boolean;
  approval: string;
  available_on: string[];
  permissions: string[];
  timeout_s: number;
}

export interface Approval {
  id: string;
  tool_name: string;
  arguments_json: Record<string, unknown>;
  details_json?: { node_name?: string | null; reason?: string; note?: string } | null;
  node_id?: string | null;
  conversation_id?: string | null;
  side_effect: string;
  status: string;
  requested_at: string;
  expires_at: string;
  decided_at?: string | null;
}

export interface FeatureFlag {
  available: boolean;
  reason: string;
}

export type SettingsMap = Record<string, Record<string, unknown>>;
