// Remote updates on the Computers page (docs/UPDATING.md): the HomeMesh line of each card, the confirm
// dialogs (worker and hub variants) and the help shown while the hub restarts itself.
//
// Everything here is plain React text. Commit subjects, branch names, paths, messages and hints come from
// other computers and git: they are never rendered as HTML (no lib/markdown, no dangerouslySetInnerHTML);
// `backticks` in server texts become <code> elements built by React.

import { Fragment, type ReactNode, useEffect, useRef, useState } from "react";
import { ApiError, checkHealth, updatesApi } from "../api/client";
import type { BuildInfo, NodeSummary, NodeUpdate, UpdateCheckResult, UpdateIssue } from "../api/types";
import {
  HEALTH_POLL_MS, type HubUpdateRecord, type PanelState, canCheck, clearHubUpdate, formatElapsed, ago, hubUpdateRecord,
  installerCommand, isBuildInfo, isCommand, isUpdating, nodeOs, panelState, parseTime, phaseLabel, readHubUpdate,
  shortDate, shortSha, splitCode, updatableCheck, updateLogText, writeHubUpdate,
} from "../lib/update";
import { CopyButton, Modal, Spinner } from "./ui";
import "../styles/updates.css";

// ---------------------------------------------------------------- small pieces

/** A server message or hint: text, with `backtick` spans as <code>. With `copy` (hints only: they come from
 * the error table, while messages and reasons can carry git, pip or npm output) commands get a Copy button. */
export function RichText({ text, copy = false }: { text: string | null | undefined; copy?: boolean }) {
  if (!text) return null;
  return (
    <>
      {splitCode(text).map((p, i) => p.code ? (
        <Fragment key={i}>
          <code className="code-inline">{p.text}</code>
          {copy && isCommand(p.text) && <CopyButton text={p.text} className="btn ghost sm update-copy-inline" />}
        </Fragment>
      ) : <Fragment key={i}>{p.text}</Fragment>)}
    </>
  );
}

/** A command to run on a computer, with a Copy button. */
export function CommandBlock({ cmd }: { cmd: string }) {
  return (
    <div className="update-cmd">
      <pre className="block wrap">{cmd}</pre>
      <CopyButton text={cmd} className="btn sm" />
    </div>
  );
}

function IssueText({ issue }: { issue: UpdateIssue }) {
  return (
    <>
      <RichText text={issue.message} />
      {issue.hint && <> <span className="muted"><RichText text={issue.hint} copy /></span></>}
    </>
  );
}

function Warnings({ items }: { items: UpdateIssue[] }) {
  if (!items.length) return null;
  return (
    <ul className="update-warnings">
      {items.map((w, i) => <li key={`${w.code}-${i}`} data-code={w.code}><span aria-hidden="true">⚠ </span><IssueText issue={w} /></li>)}
    </ul>
  );
}

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

function errorIssue(e: unknown): UpdateIssue {
  if (e instanceof ApiError) return { code: e.code, message: e.message, hint: e.hint ?? null };
  const o = (e && typeof e === "object" ? e : {}) as { code?: unknown; message?: unknown; hint?: unknown };
  return {
    code: typeof o.code === "string" ? o.code : "error",
    message: typeof o.message === "string" ? o.message : String(e),
    hint: typeof o.hint === "string" ? o.hint : null,
  };
}

export function UpdateErrorAlert({ error }: { error: unknown }) {
  if (!error) return null;
  return <div className="alert danger small" role="alert"><IssueText issue={errorIssue(error)} /></div>;
}

/** `0.3.0 · a38a3fa · 13 Oct`: what the computer runs, from its hello. Nothing is guessed. */
export function VersionLine({ node, now }: { node: NodeSummary; now: number }) {
  const b = node.build;
  if (!isBuildInfo(b)) {
    return <div className="update-version">{node.software_version ?? "version not reported"}</div>;
  }
  const commit = shortSha(b.commit);
  const date = shortDate(b.commit_date, now);
  const title = [
    b.commit ? `commit ${b.commit}` : null, b.branch ? `branch ${b.branch}` : null,
    b.commit_date ? `committed ${b.commit_date}` : null, b.installed_at ? `installed ${b.installed_at}` : null,
    node.protocol_version != null ? `protocol v${node.protocol_version}` : null,
  ].filter(Boolean).join(" · ");
  return (
    <div className="update-version" title={title || undefined}>
      <span>{b.version}</span>
      {" · "}
      {commit ? <span className="mono">{commit}</span> : <span className="faint">{b.source === "dev" ? "development install" : "commit unknown"}</span>}
      {date && <>{" · "}<span>{date}</span></>}
      {b.dirty && <>{" · "}<span className="warn-text" title="Installed from a checkout with local changes">modified</span></>}
    </div>
  );
}

// ---------------------------------------------------------------- the card's HomeMesh line

export interface UpdatePanelProps {
  node: NodeSummary;
  /** Name of another computer that is updating right now (Update is disabled while one is). */
  busyElsewhere?: string | null;
  /** A check request from this page is on its way (until the node list shows it). */
  checking?: boolean;
  /** The last check/dismiss request for this computer failed. */
  requestError?: unknown;
  onCheck: () => void;
  onUpdate: () => void;
  onDismiss: () => void;
  /** Fixed clock for tests. */
  now?: number;
}

function outcomeLine(u: NodeUpdate, node: NodeSummary) {
  const err = u.error;
  const installed = shortSha(u.installed_commit);
  switch (u.state) {
    case "ok":
      return <>Updated to <span className="mono">{installed ?? shortSha(u.expected_commit)}</span></>;
    case "ok_unconfirmed":
      return <>Updated to <span className="mono">{installed ?? shortSha(u.expected_commit)}</span> (the hub couldn't confirm it at the time)</>;
    case "already_up_to_date":
      return <>Already up to date</>;
    case "rolled_back": {
      const still = installed ?? shortSha(u.from_commit);
      return (
        <>Update failed and was undone, still on {still ? <span className="mono">{still}</span> : "the previous version"}
          {err ? <>: <IssueText issue={err} /></> : "."}</>
      );
    }
    case "failed":
      return <>Update failed{err ? <>: <IssueText issue={err} /></> : "."}</>;
    case "interrupted": {
      // What runs now: the commit the runner (or the agent finalizing the run) recorded.
      const lv = node.update_live;
      const live = lv && (lv.job_id === u.id || (u.run_id != null && lv.run_id === u.run_id)) ? lv : null;
      const commit = shortSha(live?.installed_commit ?? u.installed_commit);
      if (commit) return <>Interrupted while {phaseLabel(u.phase)}; now running <span className="mono">{commit}</span>. Check again.</>;
      return <>Interrupted while {phaseLabel(u.phase)}.{err && <> <IssueText issue={err} /></>}</>;
    }
    case "not_started":
      return <>The update did not start.{err?.hint && <> <span className="muted"><RichText text={err.hint} copy /></span></>}</>;
    case "abandoned":
      return <>Outcome unknown.{err?.hint && <> <span className="muted"><RichText text={err.hint} copy /></span></>}</>;
    default:
      // A state this page doesn't know (a newer hub): say what is stored, nothing more.
      return <>Last update: {String(u.state)}{err ? <>: <IssueText issue={err} /></> : ""}</>;
  }
}

function stateTone(s: PanelState): string {
  if (s.kind === "outcome") {
    const st = s.update.state;
    if (st === "ok" || st === "already_up_to_date") return "ok";
    if (st === "ok_unconfirmed" || st === "interrupted" || st === "not_started" || st === "abandoned") return "warn";
    return "danger";
  }
  if (s.kind === "up_to_date") return "ok";
  if (s.kind === "check_error" || s.kind === "protocol" || s.kind === "no_word") return "warn";
  return "";
}

const NEEDS_INSTALLER = new Set(["update_not_supported", "update_needs_installer"]);

export function UpdatePanel({ node, busyElsewhere, checking = false, requestError, onCheck, onUpdate, onDismiss, now: fixedNow }: UpdatePanelProps) {
  const tick = useNow(isUpdating(node) ? 1000 : 30000);
  const now = fixedNow ?? tick;
  const s = panelState(node, now, { checking });
  const name = node.name;
  const build = isBuildInfo(node.build) ? node.build : null;
  const checkedSuffix = (at: string | null) => {
    const a = ago(at, now);
    return a ? ` · checked ${a}` : "";
  };

  let line: ReactNode = null;
  let detail: ReactNode = null;
  switch (s.kind) {
    case "active": {
      if (s.step === "ready") line = <>Getting ready on {name}…</>;
      else if (s.step === "draining") line = <>Waiting for running jobs on {name}…</>;
      else if (s.step === "restarting") {
        const start = parseTime(s.startedAt);
        const elapsed = start != null ? ` · ${formatElapsed(now - start)} elapsed` : "";
        line = <>{name} is restarting{s.phase === "rolling_back" ? " (undoing the update)" : ""}{elapsed}</>;
      } else line = <>Updating {name}: {phaseLabel(s.phase)}…</>;
      if (s.local) detail = <div className="faint tiny">Started on that computer (homemesh update).</div>;
      break;
    }
    case "no_word":
      line = <>No word from {name} for {s.minutes} min. It may still be working. On it, run:</>;
      detail = <CommandBlock cmd="homemesh update status" />;
      break;
    case "protocol":
      line = <>Can't connect: different HomeMesh version. Update it by hand:</>;
      detail = <CommandBlock cmd={installerCommand(nodeOs(node), build?.source_dir, { pull: true })} />;
      break;
    case "no_updater":
      line = <>This version has no updater. Update it by hand once:</>;
      detail = <CommandBlock cmd={installerCommand(nodeOs(node), null, { pull: true })} />;
      break;
    case "invalid_build":
      line = <>This computer reported its version in a form the hub can't read. Update it by hand:</>;
      detail = <CommandBlock cmd={installerCommand(nodeOs(node), null, { pull: true })} />;
      break;
    case "checking":
      line = <Spinner label="Checking for updates…" />;
      break;
    case "outcome":
      line = outcomeLine(s.update, node);
      detail = (
        <>
          {s.update.error?.code === "update_rollback_failed" && build && <CommandBlock cmd={installerCommand(build.os, build.source_dir)} />}
          {(s.update.state === "not_started" || s.update.state === "abandoned" || s.update.state === "failed" || s.update.state === "rolled_back") && s.update.log
            && <div className="faint tiny">Log on {name}: <span className="mono">{s.update.log}</span></div>}
          <Warnings items={s.update.warnings ?? []} />
        </>
      );
      break;
    case "offline":
      line = <>Offline. Update it when it's back on.</>;
      break;
    case "cannot_update":
      line = <>Can't be updated remotely{s.reason ? <>: <RichText text={s.reason} /></> : ""}.{s.dev ? "" : " Update it by hand once:"}</>;
      if (!s.dev) detail = <CommandBlock cmd={installerCommand(build?.os, build?.source_dir, { pull: true })} />;
      break;
    case "not_allowed":
      line = <>Remote updates are off on this computer. On it, run:</>;
      detail = <CommandBlock cmd="homemesh update allow" />;
      break;
    case "check_error":
      if (s.error.code === "update_not_supported" && build?.source === "dev") {
        line = <RichText text={s.error.message} />; // the reason already says: update it with git pull
      } else if (NEEDS_INSTALLER.has(s.error.code)) {
        line = <><RichText text={s.error.message} /> Update it by hand once:</>;
        detail = <CommandBlock cmd={installerCommand(build?.os, build?.source_dir, { pull: true })} />;
      } else {
        line = <IssueText issue={s.error} />;
      }
      detail = <>{detail}{s.checkedAt && <div className="faint tiny">Checked {ago(s.checkedAt, now)}</div>}</>;
      break;
    case "up_to_date":
      line = <>✓ Up to date{checkedSuffix(s.checkedAt)}</>;
      detail = <Warnings items={s.result.problems} />;
      break;
    case "behind": {
      const n = s.result.behind ?? 0;
      line = <>{n} new change{n === 1 ? "" : "s"}{checkedSuffix(s.checkedAt)}</>;
      const blocking = s.result.problems.filter((p) => p.blocking);
      const other = s.result.problems.filter((p) => !p.blocking);
      detail = (
        <>
          {blocking.map((p, i) => <div key={`${p.code}-${i}`} className="update-blocking" data-code={p.code}><IssueText issue={p} /></div>)}
          {!blocking.length && !s.updatable && build && !build.can_update && (
            <div className="muted small">Can't be updated remotely{build.cannot_update_reason ? <>: <RichText text={build.cannot_update_reason} /></> : ""}.</div>
          )}
          <Warnings items={other} />
        </>
      );
      break;
    }
    case "unchecked":
      line = <span className="faint">Not checked yet.</span>;
      break;
  }

  const showCheck = !["active", "no_word", "checking", "no_updater", "invalid_build", "protocol"].includes(s.kind);
  const checkLabel = node.update_check || node.update ? "Check again" : "Check";
  const offerUpdate = (s.kind === "behind" && s.updatable) || (s.kind === "offline" && !!updatableCheck(node));
  const updateDisabled = !node.online || !!busyElsewhere || s.kind === "offline";

  return (
    <div className="update-panel" data-testid={`update-panel-${node.id}`}>
      <VersionLine node={node} now={now} />
      <div className={`update-state ${stateTone(s)}`} data-state={s.kind === "outcome" ? `outcome:${s.update.state}` : s.kind}>{line}</div>
      {detail}
      {requestError ? <UpdateErrorAlert error={requestError} /> : null}
      {(showCheck || offerUpdate || (s.kind === "no_word" && s.canDismiss)) && (
        <div className="update-actions">
          {showCheck && <button className="btn sm" onClick={onCheck} disabled={!canCheck(node, { checking })}>{checkLabel}</button>}
          {offerUpdate && <button className="btn sm primary" onClick={onUpdate} disabled={updateDisabled} title={busyElsewhere ? `${busyElsewhere} is updating` : undefined}>Update…</button>}
          {offerUpdate && busyElsewhere && <span className="faint small">Another computer is updating</span>}
          {s.kind === "no_word" && s.canDismiss && <button className="btn sm" onClick={onDismiss}>Dismiss</button>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- recovery help for the hub

export function RecoverySteps({ name, os, sourceDir, logsDir, runId }: { name: string; os: string; sourceDir: string | null; logsDir: string | null; runId: string | null }) {
  const log = updateLogText(os, logsDir, runId);
  return (
    <div className="update-recovery">
      <div className="small"><strong>If the hub doesn't come back:</strong></div>
      <ol className="small">
        <li>On {name}, run:<CommandBlock cmd="homemesh update status" /></li>
        <li>If that fails, open <span className="mono break">{log}</span> and run the installer:
          <CommandBlock cmd={installerCommand(os, sourceDir)} /></li>
      </ol>
    </div>
  );
}

/** Poll the hub's health every 3 s while `active`. Once it has failed at least once and answers again,
 * the hub is back (probably with a new version and new web files): do a full reload. Returns whether the
 * last poll failed. */
export function useHubUpdateWatch(active: boolean, { reload, intervalMs = HEALTH_POLL_MS }: { reload?: () => void; intervalMs?: number } = {}): boolean {
  const [down, setDown] = useState(false);
  const sawDown = useRef(false);
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let inFlight = false;
    const tick = async () => {
      if (inFlight) return;
      inFlight = true;
      const ok = await checkHealth(Math.min(5000, intervalMs));
      inFlight = false;
      if (stopped) return;
      setDown(!ok);
      if (!ok) sawDown.current = true;
      else if (sawDown.current) {
        stopped = true;
        (reloadRef.current ?? (() => window.location.reload()))();
      }
    };
    const t = setInterval(() => void tick(), intervalMs);
    return () => {
      stopped = true;
      clearInterval(t);
    };
  }, [active, intervalMs]);
  return down;
}

export function HubUpdateNotice({ record, down, onHide }: { record: HubUpdateRecord; down: boolean; onHide: () => void }) {
  return (
    <div className="alert warn update-hub-notice" role="status" data-testid="hub-update-notice">
      <div className="row">
        <strong>Updating the hub ({record.node_name}) to <span className="mono">{shortSha(record.expected_commit)}</span>.</strong>
        <span className="spacer" />
        <button className="btn ghost sm" onClick={onHide}>Hide</button>
      </div>
      <div className="small">{down ? "The hub isn't answering right now. " : ""}This page reloads by itself when the hub is back.</div>
      <RecoverySteps name={record.node_name} os={record.os} sourceDir={record.source_dir} logsDir={record.logs_dir} runId={record.run_id} />
    </div>
  );
}

/** For the app's "can't reach HomeMesh" screen: the hub-update help saved before the hub went down. */
export function StoredHubUpdateNotice({ reload }: { reload?: () => void }) {
  const [record, setRecord] = useState(() => readHubUpdate());
  const down = useHubUpdateWatch(!!record, { reload });
  if (!record) return null;
  return <HubUpdateNotice record={record} down={down} onHide={() => { clearHubUpdate(); setRecord(null); }} />;
}

// ---------------------------------------------------------------- confirm dialog

export interface UpdateDialogProps {
  node: NodeSummary;
  /** The check result the owner looked at (snapshotted when the dialog opened). */
  result: UpdateCheckResult;
  /** The hub's card (embedded node), for the version comparison. */
  hub: NodeSummary | null;
  onClose: () => void;
  /** The update was requested. */
  onDone: () => void;
  /** Hub only: the recovery record was saved (before sending) or updated/cleared afterwards. */
  onHubRecord?: (rec: HubUpdateRecord | null) => void;
}

const MAX_SUBJECTS = 10;

export function UpdateDialog({ node, result, hub, onClose, onDone, onHubRecord }: UpdateDialogProps) {
  const isHub = node.embedded;
  const build: BuildInfo | null = isBuildInfo(node.build) ? node.build : null;
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const target = result.upstream_commit ?? "";
  const hubCommit = !isHub && hub && isBuildInfo(hub.build) ? hub.build.commit : null;
  const mismatch = !!hubCommit && hubCommit !== target;
  const shown = result.commits.slice(0, MAX_SUBJECTS);
  const behind = result.behind ?? result.commits.length;
  const more = Math.max(0, behind - shown.length);

  const send = async () => {
    setBusy(true);
    setError(null);
    let rec: HubUpdateRecord | null = null;
    if (isHub && build) {
      // Saved before sending: from the moment the hub accepts, it may go away at any time.
      rec = hubUpdateRecord(node, build, target);
      writeHubUpdate(rec);
      onHubRecord?.(rec);
    }
    try {
      const res = await updatesApi.apply(node.id, target, force);
      if (rec) {
        rec = { ...rec, update_id: res.update_id };
        writeHubUpdate(rec);
        onHubRecord?.(rec);
      }
      onDone();
    } catch (e) {
      // A clear refusal (4xx) means nothing was started; keep the help when the outcome is unknown.
      if (rec && e instanceof ApiError && e.status >= 400 && e.status < 500) {
        clearHubUpdate();
        onHubRecord?.(null);
      }
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={isHub ? `Update the hub (${node.name})` : `Update ${node.name}`} onClose={onClose} wide>
      <div className="stack update-dialog">
        <div>
          {behind} new change{behind === 1 ? "" : "s"}
          {result.upstream_ref ? <> from <span className="mono">{result.upstream_ref}</span></> : null}:
        </div>
        <ul className="update-commits" aria-label="New changes">
          {shown.map((c, i) => (
            <li key={`${c.sha}-${i}`}><span className="mono faint">{shortSha(c.sha)}</span><span className="update-subject">{c.subject || "(no subject)"}</span></li>
          ))}
        </ul>
        {more > 0 && <div className="small muted">and {more} more</div>}
        <div className="small">Target commit: <span className="mono update-sha" data-testid="update-target">{target}</span></div>
        <p className="update-restart-note">HomeMesh on {node.name} restarts; running jobs there finish first (up to 2 minutes).</p>
        <label className="check"><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />Update anyway (stop running jobs)</label>
        {result.migrations && <div className="alert info small">The database will be upgraded (a copy is kept).</div>}
        {mismatch && (
          <div className="alert warn small" data-testid="version-mismatch">
            This puts {node.name} on a version the hub doesn't run yet (hub: <span className="mono">{shortSha(hubCommit)}</span>). Update the hub first.
          </div>
        )}
        {isHub && (
          <>
            <div className="alert warn small" data-testid="hub-restart-warning">
              The hub restarts. All computers disconnect briefly, chats in progress on any computer stop, and this page is offline until the hub is back.
            </div>
            <RecoverySteps name={node.name} os={build?.os ?? nodeOs(node)} sourceDir={build?.source_dir ?? null} logsDir={build?.logs_dir ?? null} runId={null} />
          </>
        )}
        <UpdateErrorAlert error={error} />
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>Cancel</button>
          <button className="btn primary" disabled={busy || !target} onClick={() => void send()}>{busy ? "Sending…" : mismatch ? "Update anyway" : "Update"}</button>
        </div>
      </div>
    </Modal>
  );
}
