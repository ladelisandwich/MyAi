// Remote updater, client side: mirrors of homemesh.protocol.update (phase words, patterns, the installer
// command) and the pure logic that decides what a computer's card says. No React here, so every state of
// the card can be tested as data. Kept in agreement with Python by src/__tests__/fixtures/update-mirror.json,
// which both tests/unit/test_update_web_mirror.py and src/__tests__/update-lib.test.ts check.

import type {
  BuildInfo, InvalidBuild, NodeSummary, NodeUpdate, StoredUpdateCheck, UpdateCheckResult, UpdateIssue, UpdateRunReport,
} from "../api/types";

// ---------------------------------------------------------------- mirrors of protocol/update.py

export const PHASES = ["accepted", "prechecks", "building", "draining", "stopping", "switching", "starting", "verifying",
  "rolling_back", "cleanup"] as const;

export const PHASE_LABELS: Readonly<Record<string, string>> = Object.freeze({
  accepted: "starting the update",
  prechecks: "checking",
  building: "building the new version",
  draining: "waiting for running jobs",
  stopping: "stopping HomeMesh",
  switching: "switching versions",
  starting: "starting the new version",
  verifying: "verifying the new version",
  rolling_back: "undoing the update",
  cleanup: "cleaning up",
});

/** Phases in which HomeMesh on that computer stops, is stopped or restarts. */
export const RESTART_PHASES: ReadonlySet<string> = new Set(["draining", "stopping", "switching", "starting", "verifying",
  "rolling_back"]);
export const FINAL_STATUSES: ReadonlySet<string> = new Set(["ok", "ok_unconfirmed", "already_up_to_date", "rolled_back",
  "failed", "interrupted"]);
export const SUCCESS_STATUSES: ReadonlySet<string> = new Set(["ok", "ok_unconfirmed", "already_up_to_date"]);
export const ACTIVE_ROW_STATES: ReadonlySet<string> = new Set(["requested", "running"]);

export const NO_WORD_AFTER_S = 600; // "No word from <name> for N min"
export const DISMISS_AFTER_S = 900; // Dismiss allowed after 15 minutes without a report
export const HEALTH_POLL_MS = 3000; // while the hub restarts

export const SHA_PATTERN = /^[0-9a-f]{40}([0-9a-f]{24})?$/;
export const RUN_ID_PATTERN = /^u[0-9]{14}-[0-9a-f]{8}$/;

export function isSha(v: unknown): v is string {
  return typeof v === "string" && SHA_PATTERN.test(v);
}

export function isRunId(v: unknown): v is string {
  return typeof v === "string" && RUN_ID_PATTERN.test(v);
}

/** The 7-character form shown to people (`a38a3fa`); null stays null. */
export function shortSha(sha: string | null | undefined): string | null {
  return typeof sha === "string" && sha ? sha.slice(0, 7) : null;
}

/** Words for a phase. A phase this version doesn't know (a newer runner) is shown as it is. Own keys only:
 * a phase word like "constructor" must not pick up Object.prototype members. */
export function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return "working";
  return Object.prototype.hasOwnProperty.call(PHASE_LABELS, phase) ? PHASE_LABELS[phase] : phase;
}

// Python's shlex.quote (re.ASCII: \w is [A-Za-z0-9_], as in JavaScript).
const UNSAFE = /[^\w@%+=:,./-]/;
export function shQuote(s: string): string {
  if (!s) return "''";
  if (!UNSAFE.test(s)) return s;
  return `'${s.replace(/'/g, `'"'"'`)}'`;
}

/** Same as protocol.update.installer_command(): the command that updates or repairs HomeMesh by hand.
 * `pull` first fast-forwards the checkout (a computer without the updater, a target that needs the
 * installer); without it the installer reinstalls what the checkout holds (recovery). */
export function installerCommand(osName: string | null | undefined, sourceDir: string | null | undefined, { pull = false } = {}): string {
  if (osName === "windows") {
    const where = sourceDir ? `cd "${sourceDir}"` : "cd <HomeMesh source folder>";
    const pullStep = pull ? "git pull --ff-only; " : "";
    return `${where}; ${pullStep}powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade`;
  }
  if (sourceDir) {
    const src = shQuote(sourceDir);
    if (pull) return `cd ${src} && git pull --ff-only && ./deploy/linux/install.sh --upgrade`;
    return `${shQuote(sourceDir.replace(/\/+$/, "") + "/deploy/linux/install.sh")} --upgrade`;
  }
  const pullStep = pull ? "git pull --ff-only && " : "";
  return `cd <HomeMesh source folder> && ${pullStep}./deploy/linux/install.sh --upgrade`;
}

// Inside PowerShell "double quotes" `$` and the backtick expand ("$(...)" runs code) and a double quote,
// typographic ones included, ends the string; `cd` (Set-Location -Path) treats [ ] as a wildcard.
const POWERSHELL_SPECIAL = /["`$[\]“-„]/;

/** The reported source folder if a command built from it does exactly what it shows, else null (the command
 * then says `<HomeMesh source folder>`). The folder comes from that computer's hello, i.e. from another
 * computer; installerCommand() shell-quotes it for sh, but its Windows form is `cd "<folder>"` (as in Python),
 * so anything PowerShell interprets there is refused, and on every OS so are hidden or bidi characters. */
export function commandSourceDir(osName: string | null | undefined, sourceDir: string | null | undefined): string | null {
  if (!sourceDir || HIDDEN_CHARS.test(sourceDir)) return null;
  if (osName === "windows" && POWERSHELL_SPECIAL.test(sourceDir)) return null;
  return sourceDir;
}

/** installerCommand() for a command the page offers with a Copy button. */
export function safeInstallerCommand(osName: string | null | undefined, sourceDir: string | null | undefined, opts: { pull?: boolean } = {}): string {
  return installerCommand(osName, commandSourceDir(osName, sourceDir), opts);
}

// ---------------------------------------------------------------- node facts

export function isBuildInfo(b: BuildInfo | InvalidBuild | null | undefined): b is BuildInfo {
  return !!b && typeof b === "object" && !("invalid" in b && b.invalid) && typeof (b as BuildInfo).version === "string";
}

export function isInvalidBuild(b: BuildInfo | InvalidBuild | null | undefined): b is InvalidBuild {
  return !!b && typeof b === "object" && "invalid" in b && b.invalid === true;
}

/** The node row's `os` is platform.system() ("Windows", "Linux"); BuildInfo.os is lower case. */
export function nodeOs(n: Pick<NodeSummary, "os" | "build">): string {
  if (isBuildInfo(n.build)) return n.build.os;
  const os = (n.os ?? "").toLowerCase();
  return os === "windows" ? "windows" : os === "linux" ? "linux" : "other";
}

export function isProtocolIncompatible(n: Pick<NodeSummary, "online" | "last_disconnect_reason">): boolean {
  return !n.online && (n.last_disconnect_reason ?? "").startsWith("protocol_incompatible");
}

export function activeUpdate(n: Pick<NodeSummary, "update">): NodeUpdate | null {
  return n.update && ACTIVE_ROW_STATES.has(n.update.state) ? n.update : null;
}

/** A run reported live that is not (yet) a final outcome and not the node's active remote update, e.g.
 * `homemesh update` started on that computer (origin "local"), or a remote run whose row the hub ended as a
 * guess while another update was active. Only trusted while the computer is connected. */
export function liveLocalRun(n: Pick<NodeSummary, "online" | "update" | "update_live">): UpdateRunReport | null {
  const live = n.update_live;
  if (!n.online || !live || live.status != null) return null;
  const act = activeUpdate(n);
  if (act && live.job_id === act.id) return null;
  return live;
}

/** True while an update runs on this computer (a remote one, or a local one it reports). */
export function isUpdating(n: NodeSummary): boolean {
  return !!activeUpdate(n) || !!liveLocalRun(n);
}

export function checkResult(c: StoredUpdateCheck | null | undefined): UpdateCheckResult | null {
  return c && "result" in c && c.result ? c.result : null;
}

export function checkError(c: StoredUpdateCheck | null | undefined): UpdateIssue | null {
  if (!c) return null;
  if ("error" in c && c.error) return c.error;
  const r = checkResult(c);
  return r && !r.ok ? r.error ?? { code: "update_error", message: "The check failed.", hint: null } : null;
}

/** The check result the Update button may act on, or null. */
export function updatableCheck(n: NodeSummary): UpdateCheckResult | null {
  const r = checkResult(n.update_check);
  if (!r || !r.ok || !r.can_update || !isSha(r.upstream_commit) || !(r.behind && r.behind > 0)) return null;
  if (r.problems.some((p) => p.blocking) || r.allowed === false) return null;
  return isBuildInfo(n.build) && n.build.can_update ? r : null;
}

// ---------------------------------------------------------------- time

export function parseTime(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const t = new Date(ts).getTime();
  return Number.isNaN(t) ? null : t;
}

/** "just now", "5 min ago", "2 h ago", "3 d ago". */
export function ago(ts: string | null | undefined, now: number): string | null {
  const t = parseTime(ts);
  if (t == null) return null;
  const s = Math.max(0, Math.round((now - t) / 1000));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ago`;
  return `${Math.floor(h / 24)} d ago`;
}

/** "3:12", or "1:03:12" after an hour. Elapsed time only; nothing here estimates what is left. */
export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${ss}` : `${m}:${ss}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "13 Oct" (with the year when it isn't this year's). */
export function shortDate(ts: string | null | undefined, now: number = Date.now()): string | null {
  const t = parseTime(ts);
  if (t == null) return null;
  const d = new Date(t);
  const sameYear = d.getFullYear() === new Date(now).getFullYear();
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${sameYear ? "" : ` ${d.getFullYear()}`}`;
}

// ---------------------------------------------------------------- what the card says

export type PanelState =
  /** A remote update (or one started on that computer) is running. */
  | { kind: "active"; step: "ready" | "draining" | "restarting" | "other"; phase: string | null; startedAt: string | null; local: boolean }
  /** Active, and nothing was heard of this run for NO_WORD_AFTER_S (offline, or online but not reporting it). */
  | { kind: "no_word"; minutes: number; canDismiss: boolean }
  | { kind: "protocol" }
  | { kind: "no_updater" }
  | { kind: "invalid_build" }
  | { kind: "checking" }
  | { kind: "outcome"; update: NodeUpdate }
  | { kind: "offline" }
  | { kind: "cannot_update"; reason: string | null; dev: boolean }
  | { kind: "not_allowed" }
  | { kind: "check_error"; error: UpdateIssue; checkedAt: string | null }
  | { kind: "up_to_date"; result: UpdateCheckResult; checkedAt: string | null }
  | { kind: "behind"; result: UpdateCheckResult; checkedAt: string | null; updatable: boolean }
  | { kind: "unchecked" };

function activeStep(phase: string | null): "ready" | "draining" | "restarting" | "other" {
  if (!phase || phase === "accepted" || phase === "prechecks" || phase === "building") return "ready";
  if (phase === "draining") return "draining";
  if (RESTART_PHASES.has(phase)) return "restarting";
  return "other";
}

/** One state per card, in this order: what runs now beats what is stored; a stored outcome beats an older
 * check; a check beats the advisory consent flag from the hello. */
export function panelState(n: NodeSummary, now: number,
  { checking = false, runStartedAt = null }: { checking?: boolean; runStartedAt?: string | null } = {}): PanelState {
  const act = activeUpdate(n);
  if (act) {
    const live = n.update_live && n.update_live.job_id === act.id ? n.update_live : null;
    // The hub's Dismiss rule (update_service.dismiss): a connected computer reporting this run right now is
    // never silent, however long a phase takes. Otherwise silence counts from the last report (or the
    // request) whether or not the computer is connected: one that is online but reports no run, or another
    // run, would leave this update active (and every Update button disabled) with nothing to click.
    const reporting = n.online && live != null && live.status == null;
    const last = parseTime(act.last_report_at) ?? parseTime(act.requested_at);
    const silentS = reporting || last == null ? 0 : Math.max(0, (now - last) / 1000);
    if (silentS >= NO_WORD_AFTER_S) {
      return { kind: "no_word", minutes: Math.floor(silentS / 60), canDismiss: silentS >= DISMISS_AFTER_S };
    }
    const phase = act.phase ?? live?.phase ?? null;
    // Elapsed counts from the run's start (report.started_at; `runStartedAt` is the one this page saw before
    // the computer went offline mid-restart and its live report went with it); never seen: from the request.
    const startedAt = live?.started_at ?? runStartedAt ?? act.requested_at;
    return { kind: "active", step: activeStep(phase), phase, startedAt, local: false };
  }
  const local = liveLocalRun(n);
  if (local) {
    return { kind: "active", step: activeStep(local.phase), phase: local.phase, startedAt: local.started_at, local: local.origin === "local" };
  }
  if (isProtocolIncompatible(n)) return { kind: "protocol" };
  if (n.build == null) return { kind: "no_updater" };
  if (!isBuildInfo(n.build)) return { kind: "invalid_build" };
  if (checking || n.update_check_pending) return { kind: "checking" };

  const check = n.update_check ?? null;
  const upd = n.update ?? null;
  if (upd) {
    const doneAt = parseTime(upd.finished_at) ?? parseTime(upd.last_report_at) ?? parseTime(upd.requested_at) ?? 0;
    const checkedAt = parseTime(check?.checked_at) ?? -Infinity;
    if (doneAt >= checkedAt) return { kind: "outcome", update: upd };
  }
  if (!n.online) return { kind: "offline" };

  const err = checkError(check);
  if (check && err) {
    return err.code === "update_not_allowed" ? { kind: "not_allowed" } : { kind: "check_error", error: err, checkedAt: check.checked_at };
  }
  const result = checkResult(check);
  if (check && result) {
    if (result.allowed === false) return { kind: "not_allowed" };
    if (!result.behind) return { kind: "up_to_date", result, checkedAt: check.checked_at };
    return { kind: "behind", result, checkedAt: check.checked_at, updatable: !!updatableCheck(n) };
  }
  if (!n.build.can_update) {
    return { kind: "cannot_update", reason: n.build.cannot_update_reason ?? n.build.reason, dev: n.build.source === "dev" };
  }
  if (!n.build.update_allowed) return { kind: "not_allowed" };
  return { kind: "unchecked" };
}

/** May the per-card "Check" button run? (The header button checks every computer for which this is true.) */
export function canCheck(n: NodeSummary, { checking = false } = {}): boolean {
  return n.online && isBuildInfo(n.build) && !checking && !n.update_check_pending && !isUpdating(n);
}

// ---------------------------------------------------------------- hub update recovery (localStorage)

export const HUB_UPDATE_KEY = "homemesh.hubUpdate";
const HUB_UPDATE_MAX_AGE_MS = 7 * 24 * 3600 * 1000;
/** How long help is kept for a request whose fate the hub may still learn (lost answer, launch timeout). */
const HUB_UPDATE_GRACE_MS = 10 * 60 * 1000;

/** What the page needs to help when the hub doesn't come back from its own update. Built from the hub's
 * `build` before the update is sent, so it is there even if the page is reloaded while the hub is down. */
export interface HubUpdateRecord {
  v: 1;
  node_id: string;
  node_name: string;
  update_id: string | null;
  expected_commit: string;
  os: string;
  source_dir: string | null;
  logs_dir: string | null;
  run_id: string | null;
  saved_at: string;
}

const str = (v: unknown, max = 1000): v is string => typeof v === "string" && v.length <= max;
const strOrNull = (v: unknown, max = 1000) => v === null || str(v, max);

export function hubUpdateRecord(n: NodeSummary, build: BuildInfo, expectedCommit: string, now: number = Date.now()): HubUpdateRecord {
  return {
    v: 1, node_id: n.id, node_name: n.name, update_id: null, expected_commit: expectedCommit, os: build.os,
    source_dir: build.source_dir, logs_dir: build.logs_dir, run_id: null, saved_at: new Date(now).toISOString(),
  };
}

/** The stored record, or null (absent, unreadable, malformed or older than a week). Never throws. */
export function readHubUpdate(now: number = Date.now()): HubUpdateRecord | null {
  try {
    const raw = window.localStorage.getItem(HUB_UPDATE_KEY);
    if (!raw) return null;
    const o = JSON.parse(raw) as Record<string, unknown>;
    const ok = o && typeof o === "object" && o.v === 1 && str(o.node_id, 200) && str(o.node_name, 200)
      && strOrNull(o.update_id, 200) && isSha(o.expected_commit) && str(o.os, 20) && strOrNull(o.source_dir)
      && strOrNull(o.logs_dir) && (o.run_id === null || isRunId(o.run_id)) && str(o.saved_at, 40);
    const saved = ok ? parseTime(o.saved_at as string) : null;
    if (!ok || saved == null || now - saved > HUB_UPDATE_MAX_AGE_MS) {
      clearHubUpdate();
      return null;
    }
    const r = o as unknown as HubUpdateRecord;
    return { v: 1, node_id: r.node_id, node_name: r.node_name, update_id: r.update_id, expected_commit: r.expected_commit,
      os: r.os, source_dir: r.source_dir, logs_dir: r.logs_dir, run_id: r.run_id, saved_at: r.saved_at };
  } catch {
    return null;
  }
}

export function writeHubUpdate(rec: HubUpdateRecord): boolean {
  try {
    window.localStorage.setItem(HUB_UPDATE_KEY, JSON.stringify(rec));
    return true;
  } catch {
    return false;
  }
}

export function clearHubUpdate(): void {
  try {
    window.localStorage.removeItem(HUB_UPDATE_KEY);
  } catch {
    /* storage unavailable: nothing to clear */
  }
}

/** What the page should do with a stored hub record given the latest node list: keep it, refresh it with the
 * run id the hub learned, or drop it because the outcome is known (or it belongs to another update). */
export function reconcileHubUpdate(rec: HubUpdateRecord, nodes: NodeSummary[], now: number = Date.now()):
  { action: "keep" } | { action: "clear"; outcome: string | null } | { action: "update"; record: HubUpdateRecord } {
  const hub = nodes.find((x) => x.id === rec.node_id);
  if (!hub) return { action: "clear", outcome: null };
  const upd = hub.update ?? null;
  const saved = parseTime(rec.saved_at) ?? 0;
  const young = now - saved <= HUB_UPDATE_GRACE_MS;
  if (rec.update_id === null) {
    // The apply request never answered: keep for a while in case it went through.
    if (upd && ACTIVE_ROW_STATES.has(upd.state) && upd.expected_commit === rec.expected_commit) {
      return { action: "update", record: { ...rec, update_id: upd.id, run_id: isRunId(upd.run_id) ? upd.run_id : null } };
    }
    return young ? { action: "keep" } : { action: "clear", outcome: null };
  }
  if (!upd || upd.id !== rec.update_id) return { action: "clear", outcome: null };
  // "abandoned" is the hub's guess and a late report can bring the update back: keep helping until then.
  // So is "failed" with update_launch_failed: the computer's 15 s wait for the helper ran out, but a helper that
  // was merely slow to start (a cold python.exe under Defender, a slow systemd-run) goes on, restarts the hub
  // and its reports make the row active again within seconds. Keep helping for a while, as for a lost answer.
  if (upd.state === "failed" && upd.error?.code === "update_launch_failed") {
    return young ? { action: "keep" } : { action: "clear", outcome: upd.state };
  }
  if (!ACTIVE_ROW_STATES.has(upd.state) && upd.state !== "abandoned") return { action: "clear", outcome: upd.state };
  if (!rec.run_id && isRunId(upd.run_id)) return { action: "update", record: { ...rec, run_id: upd.run_id } };
  return { action: "keep" };
}

/** `<logs_dir>/update-<run_id>.log` with the computer's own separator, or a description when not known yet. */
export function updateLogText(os: string, logsDir: string | null, runId: string | null): string {
  const sep = os === "windows" ? "\\" : "/";
  const dir = logsDir ? logsDir.replace(/[\\/]+$/, "") : null;
  if (dir && runId) return `${dir}${sep}update-${runId}.log`;
  if (dir) return `the newest update-u*.log in ${dir}`;
  return "the newest update-u*.log in HomeMesh's logs folder";
}

// ---------------------------------------------------------------- inline `code` in server texts

export type TextPart = { code: boolean; text: string };

/** Split a message or hint at `backticks` into text and code parts (rendered as React text, never HTML).
 * An unbalanced backtick leaves the text as it is. */
export function splitCode(text: string): TextPart[] {
  const parts = text.split("`");
  if (parts.length < 3 || parts.length % 2 === 0) return [{ code: false, text }];
  return parts.map((t, i) => ({ code: i % 2 === 1, text: t })).filter((p) => p.code || p.text);
}

/** Characters that are invisible or reorder text on screen: C0/C1 controls, soft hyphen, zero-width and
 * bidirectional formatting characters, the BOM. A command containing one can look different from what Copy
 * puts on the clipboard, so it gets no Copy button and is never built into a command (commandSourceDir). */
// eslint-disable-next-line no-control-regex
export const HIDDEN_CHARS = /[\u0000-\u001f\u007f-\u009f­؜ᅟᅠ᠎​-‏‪-‮⁠-⁩﻿]/;

// What chains, substitutes or redirects commands in bash or PowerShell: ; & | < > ( ) { } ! and $( ${. No
// command in the error table needs one (`<branch>` is a placeholder to edit, not something to paste).
const SHELL_CONTROL = /[;&|<>(){}!]|\$[({]/;

/** A code part that is one plain command someone would paste into a terminal (gets a Copy button). Callers
 * offer Copy only in hints, never in messages (they can carry git, pip or npm output). A hint is the error
 * table's text from an honest computer, but it arrives from that computer (JobFailed.hint, run.json): a
 * computer impersonated with stolen worker credentials could send `homemesh update allow; iwr …|iex`. So
 * code that would run more than the command it starts with gets no Copy button; it is still shown. */
export function isCommand(code: string): boolean {
  return /^(homemesh|git|gh|loginctl|winget|journalctl|powershell|cd) \S/.test(code) && !HIDDEN_CHARS.test(code)
    && !SHELL_CONTROL.test(code);
}
