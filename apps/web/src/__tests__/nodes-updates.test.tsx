import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { NodeSummary } from "../api/types";
import { StoredHubUpdateNotice } from "../components/UpdatePanel";
import { HUB_UPDATE_KEY, hubUpdateRecord, readHubUpdate, writeHubUpdate } from "../lib/update";
import NodesPage from "../pages/Nodes";
import { NOW, SHA_B, build, hubNode, iso, node, result, row, stored } from "./updateFixtures";

type Handler = (method: string, body: unknown) => Response | Promise<Response>;

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** fetch stub routed by path; records every call. A missing route is a network failure. */
function stubFetch(routes: Record<string, Handler>) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path, body });
    const h = routes[`${method} ${path}`];
    if (!h) throw new TypeError("Failed to fetch");
    return h(method, body);
  });
  vi.stubGlobal("fetch", fn);
  return { fn, calls };
}

const card = (name: string) => screen.getByRole("heading", { name }).closest(".node-card") as HTMLElement;

beforeEach(() => window.localStorage.clear());
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.localStorage.clear();
});

describe("Computers page with updates", () => {
  // Only Date is faked (timers stay real): the fixtures' timestamps are relative to NOW.
  beforeEach(() => vi.useFakeTimers({ now: NOW, toFake: ["Date"] }));

  it("lists the hub first and checks every online computer with the updater in parallel", async () => {
    let pendingAfterCheck = false;
    const nodes: NodeSummary[] = [
      node({ id: "w1", name: "gaming-pc" }),
      node({ id: "w2", name: "old-laptop", build: null }),
      node({ id: "w3", name: "nas", online: false }),
      hubNode(),
    ];
    const { calls } = stubFetch({
      "GET /api/v1/nodes": () => json(200, pendingAfterCheck ? nodes.map((n) => (n.id === "w1" || n.id === "n-hub" ? { ...n, update_check_pending: "job" } : n)) : nodes),
      "POST /api/v1/nodes/w1/update/check": () => { pendingAfterCheck = true; return json(202, { job_id: "job-1" }); },
      "POST /api/v1/nodes/n-hub/update/check": () => json(202, { job_id: "job-2" }),
    });
    render(<NodesPage />);
    const headings = await screen.findAllByRole("heading", { level: 2 });
    expect(headings.map((h) => h.textContent)).toEqual(["hub-pc", "gaming-pc", "old-laptop", "nas"]);

    fireEvent.click(screen.getByRole("button", { name: "Check for updates" }));
    await waitFor(() => expect(within(card("gaming-pc")).getByText("Checking for updates…")).toBeInTheDocument());
    const posts = calls.filter((c) => c.method === "POST").map((c) => c.path).sort();
    expect(posts).toEqual(["/api/v1/nodes/n-hub/update/check", "/api/v1/nodes/w1/update/check"]);
    expect(within(card("hub-pc")).getByText("Checking for updates…")).toBeInTheDocument();
    expect(within(card("old-laptop")).getByText("This version has no updater. Update it by hand once:")).toBeInTheDocument();
  });

  it("shows a refused check on that computer's card", async () => {
    stubFetch({
      "GET /api/v1/nodes": () => json(200, [node()]),
      "POST /api/v1/nodes/n-worker/update/check": () => json(409, { error: { code: "update_busy", message: "Another update or check is running on gaming-pc (building the new version).", hint: "Wait for it to finish; on gaming-pc: `homemesh update status`." } }),
    });
    render(<NodesPage />);
    fireEvent.click(await within(await screen.findByTestId("update-panel-n-worker")).findByRole("button", { name: "Check" }));
    expect(await within(card("gaming-pc")).findByRole("alert")).toHaveTextContent("Another update or check is running on gaming-pc");
  });

  it("hides the Disconnected alert while an update is active and disables Update elsewhere", async () => {
    stubFetch({
      "GET /api/v1/nodes": () => json(200, [
        node({ id: "w1", name: "gaming-pc", online: false, last_disconnect_reason: "update restart", update: row({ phase: "starting" }) }),
        node({ id: "w2", name: "office-pc", online: false, last_disconnect_reason: "connection lost" }),
        node({ id: "w3", name: "studio-pc", update_check: stored(result()) }),
        hubNode({ build: build({ os: "windows", commit: SHA_B }) }),
      ]),
    });
    render(<NodesPage />);
    await screen.findByText(/gaming-pc is restarting/);
    expect(within(card("gaming-pc")).queryByText(/Disconnected:/)).toBeNull();
    expect(within(card("office-pc")).getByText("Disconnected: connection lost")).toBeInTheDocument();
    const studio = card("studio-pc");
    expect(within(studio).getByRole("button", { name: "Update…" })).toBeDisabled();
    expect(within(studio).getByText("Another computer is updating")).toBeInTheDocument();
  });

  it("opens the confirm dialog from Update… and sends the commit the check showed", async () => {
    const { calls } = stubFetch({
      "GET /api/v1/nodes": () => json(200, [hubNode({ build: build({ os: "windows", commit: SHA_B }) }), node({ update_check: stored(result()) })]),
      "POST /api/v1/nodes/n-worker/update/apply": () => json(202, { update_id: "job-apply-1", state: "requested" }),
    });
    render(<NodesPage />);
    fireEvent.click(await within(await screen.findByTestId("update-panel-n-worker")).findByRole("button", { name: "Update…" }));
    const dialog = screen.getByRole("dialog", { name: "Update gaming-pc" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Update" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/api/v1/nodes/n-worker/update/apply", body: { expected_commit: SHA_B, force: false } });
  });

  it("shows the saved hub-update help when the hub doesn't answer", async () => {
    writeHubUpdate({ ...hubUpdateRecord(hubNode(), hubNode().build as ReturnType<typeof build>, SHA_B), update_id: "job-apply-1", run_id: "u20261016115500-0a1b2c3d" });
    stubFetch({}); // everything fails: the hub is restarting
    render(<NodesPage />);
    const notice = await screen.findByTestId("hub-update-notice");
    expect(notice.textContent).toContain("Update of the hub (hub-pc) to b12c3d4, requested just now.");
    expect(notice.textContent).toContain("If that fails, open C:\\Users\\Ann\\AppData\\Local\\HomeMesh\\logs\\update-u20261016115500-0a1b2c3d.log and run the installer:");
    expect([...notice.querySelectorAll("pre")].map((p) => p.textContent)).toEqual([
      "homemesh update status", 'cd "C:\\Users\\Ann\\src\\HomeMesh"; powershell -ExecutionPolicy Bypass -File deploy\\windows\\install.ps1 -Upgrade',
    ]);
    fireEvent.click(within(notice).getByRole("button", { name: "Hide" }));
    expect(screen.queryByTestId("hub-update-notice")).toBeNull();
    expect(window.localStorage.getItem(HUB_UPDATE_KEY)).toBeNull();
  });

  it("forgets the hub-update help once the hub reports the outcome", async () => {
    writeHubUpdate({ ...hubUpdateRecord(hubNode(), hubNode().build as ReturnType<typeof build>, SHA_B), update_id: "job-apply-1" });
    stubFetch({ "GET /api/v1/nodes": () => json(200, [hubNode({ update: row({ state: "ok", finished_at: iso(0) }) })]) });
    render(<NodesPage />);
    await within(await screen.findByTestId("update-panel-n-hub")).findByText(/Updated to/);
    await waitFor(() => expect(window.localStorage.getItem(HUB_UPDATE_KEY)).toBeNull());
    expect(screen.queryByTestId("hub-update-notice")).toBeNull();
  });

  it("keeps the help and learns the run id while the hub update runs", async () => {
    writeHubUpdate({ ...hubUpdateRecord(hubNode(), hubNode().build as ReturnType<typeof build>, SHA_B), update_id: "job-apply-1" });
    stubFetch({ "GET /api/v1/nodes": () => json(200, [hubNode({ update: row({ phase: "building" }) })]) });
    render(<NodesPage />);
    await screen.findByText("Getting ready on hub-pc…");
    await waitFor(() => expect(readHubUpdate()).toMatchObject({ run_id: "u20261016115500-0a1b2c3d" }));
    expect(screen.getByTestId("hub-update-notice")).toBeInTheDocument();
  });
});

describe("health poll while the hub restarts", () => {
  const save = () => writeHubUpdate({ ...hubUpdateRecord(hubNode(), hubNode().build as ReturnType<typeof build>, SHA_B), update_id: "job-apply-1" });

  it("reloads once the hub answers again after being down, polling every 3 s", async () => {
    vi.useFakeTimers();
    save();
    let up = false;
    const { calls } = stubFetch({ "GET /api/v1/system/health": () => (up ? json(200, { status: "ok" }) : Promise.reject(new TypeError("down"))) });
    const reload = vi.fn();
    render(<StoredHubUpdateNotice reload={reload} />);
    expect(screen.getByTestId("hub-update-notice")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2999); });
    expect(calls).toHaveLength(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(calls.map((c) => c.path)).toEqual(["/api/v1/system/health"]);
    expect(screen.getByTestId("hub-update-notice").textContent).toContain("The hub isn't answering right now.");
    expect(reload).not.toHaveBeenCalled();
    up = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(reload).toHaveBeenCalledOnce();
    await act(async () => { await vi.advanceTimersByTimeAsync(9000); });
    expect(reload).toHaveBeenCalledOnce(); // stops polling after the reload
  });

  it("never reloads when it was never seen down (no reload loop)", async () => {
    vi.useFakeTimers();
    save();
    stubFetch({ "GET /api/v1/system/health": () => json(200, { status: "ok" }) });
    const reload = vi.fn();
    render(<StoredHubUpdateNotice reload={reload} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(reload).not.toHaveBeenCalled();
  });

  it("does nothing without a saved record", async () => {
    vi.useFakeTimers();
    const { calls } = stubFetch({});
    render(<StoredHubUpdateNotice reload={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(calls).toHaveLength(0);
    expect(screen.queryByTestId("hub-update-notice")).toBeNull();
  });
});

describe("app start while the hub is down", () => {
  it("shows the saved hub-update help on the can't-connect screen", async () => {
    writeHubUpdate({ ...hubUpdateRecord(hubNode(), hubNode().build as ReturnType<typeof build>, SHA_B), update_id: "job-apply-1" });
    stubFetch({});
    render(<App />);
    expect(await screen.findByText(/Cannot reach the HomeMesh coordinator/)).toBeInTheDocument();
    expect(screen.getByTestId("hub-update-notice").textContent).toContain("Update of the hub (hub-pc) to b12c3d4, requested just now.");
  });

  it("says when the update was requested: the help is kept for a week", async () => {
    const threeDaysAgo = Date.now() - 3 * 24 * 3600 * 1000;
    writeHubUpdate({ ...hubUpdateRecord(hubNode(), hubNode().build as ReturnType<typeof build>, SHA_B, threeDaysAgo), update_id: "job-apply-1" });
    stubFetch({});
    render(<App />);
    expect(await screen.findByTestId("hub-update-notice")).toHaveTextContent("Update of the hub (hub-pc) to b12c3d4, requested 3 d ago.");
    expect(screen.getByTestId("hub-update-notice").textContent).not.toContain("Updating");
  });

  it("shows only the usual error without a saved record", async () => {
    stubFetch({});
    render(<App />);
    expect(await screen.findByText(/Cannot reach the HomeMesh coordinator/)).toBeInTheDocument();
    expect(screen.queryByTestId("hub-update-notice")).toBeNull();
  });
});
