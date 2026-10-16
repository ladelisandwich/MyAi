// Builders for node rows with update fields, shaped like GET /api/v1/nodes (interfaces section 8).
import type { BuildInfo, NodeSummary, NodeUpdate, StoredUpdateCheck, UpdateCheckResult, UpdateRunReport } from "../api/types";

export const NOW = Date.parse("2026-10-16T12:00:00.000Z");
export const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();
export const MIN = 60_000;

export const SHA_A = "a38a3fa0123456789abcdef0123456789abcdef0".slice(0, 40);
export const SHA_B = "b12c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e";
export const SHA_C = "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee00";

export function build(over: Partial<BuildInfo> = {}): BuildInfo {
  return {
    v: 1, version: "0.3.0", commit: SHA_A, commit_date: "2026-10-13T09:00:00+02:00", branch: "main", dirty: false,
    source: "installer", reason: null, installed_at: "2026-10-13T10:00:00Z", os: "linux", can_update: true,
    cannot_update_reason: null, update_allowed: true, source_dir: "/home/ann/src/HomeMesh", logs_dir: "/home/ann/.local/state/homemesh/logs",
    ...over,
  };
}

export function result(over: Partial<UpdateCheckResult> = {}): UpdateCheckResult {
  const commits = over.commits ?? [
    { sha: SHA_B, subject: "Fix the scheduler", date: "2026-10-15T10:00:00Z" },
    { sha: SHA_C, subject: "Add a button", date: "2026-10-14T10:00:00Z" },
    { sha: "d".repeat(40), subject: "Docs", date: "2026-10-14T09:00:00Z" },
  ];
  return {
    v: 1, ok: true, checked_at: iso(5 * MIN), allowed: true, installed_commit: SHA_A, installed_dirty: false, head_commit: SHA_A,
    branch: "main", remote: "origin", upstream_ref: "origin/main", upstream_commit: SHA_B, upstream_date: "2026-10-15T10:00:00Z",
    behind: commits.length, ahead: 0, commits, commits_truncated: false, web_changed: false, node_found: true, migrations: false,
    can_update: true, problems: [], error: null, ...over,
  };
}

export function stored(r: UpdateCheckResult, msAgo = 5 * MIN): StoredUpdateCheck {
  return { job_id: "job-check-1", checked_at: iso(msAgo), result: r };
}

export function storedError(code: string, message: string, hint: string | null, msAgo = 5 * MIN): StoredUpdateCheck {
  return { job_id: "job-check-1", checked_at: iso(msAgo), error: { code, message, hint } };
}

export function row(over: Partial<NodeUpdate> = {}): NodeUpdate {
  return {
    id: "job-apply-1", state: "running", phase: null, run_id: "u20261016115500-0a1b2c3d", from_commit: SHA_A, expected_commit: SHA_B,
    requested_at: iso(5 * MIN), last_report_at: iso(1 * MIN), finished_at: null, error: null, warnings: [], log: null,
    installed_commit: null, ...over,
  };
}

export function report(over: Partial<UpdateRunReport> = {}): UpdateRunReport {
  return {
    v: 1, run_id: "u20261016115500-0a1b2c3d", job_id: "job-apply-1", origin: "remote", phase: "building", status: null,
    expected_commit: SHA_B, from_commit: SHA_A, to_commit: null, installed_commit: null, started_at: iso(3 * MIN + 12_000),
    updated_at: iso(10_000), finished_at: null, error: null, warnings: [], log: "/home/ann/.local/state/homemesh/logs/update-u20261016115500-0a1b2c3d.log",
    ...over,
  };
}

export function node(over: Partial<NodeSummary> = {}): NodeSummary {
  return {
    id: "n-worker", name: "gaming-pc", status: "online", embedded: false, mode: "balanced", weight: 0, os: "Linux", arch: "x86_64",
    hostname: "gaming-pc", software_version: "0.3.0", protocol_version: 1, created_at: "2026-10-01T00:00:00Z",
    last_connected_at: iso(60 * MIN), last_seen_at: iso(0), last_disconnect_reason: null, task_types: ["chat"], allowed_roots: [],
    allowed_tools: [], hardware: null, limits: null, backends: [], online: true, telemetry: null, running_jobs: [], heartbeat_age_s: 2,
    allow_remote_mode_change: true, models: [], build: build(), update_check: null, update_check_pending: null, update: null,
    update_live: null, ...over,
  };
}

export function hubNode(over: Partial<NodeSummary> = {}): NodeSummary {
  return node({
    id: "n-hub", name: "hub-pc", embedded: true, os: "Windows", hostname: "hub-pc",
    build: build({ os: "windows", source_dir: "C:\\Users\\Ann\\src\\HomeMesh", logs_dir: "C:\\Users\\Ann\\AppData\\Local\\HomeMesh\\logs" }),
    ...over,
  });
}
