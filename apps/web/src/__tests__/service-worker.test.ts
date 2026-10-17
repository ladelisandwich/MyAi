// public/sw.js run against fake service-worker globals: what it caches, and what a page opened while the
// coordinator is down (e.g. the hub restarting for its own update) gets.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

const SW = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "..", "..", "public", "sw.js"), "utf-8");

type Handler = (event: unknown) => void;

function loadWorker(network: (url: string) => Promise<Response>) {
  const handlers: Record<string, Handler> = {};
  const store = new Map<string, Response>();
  const cache = {
    put: vi.fn(async (key: string | Request, res: Response) => { store.set(typeof key === "string" ? key : new URL(key.url).pathname, res); }),
    addAll: vi.fn(async () => undefined),
  };
  const caches = {
    open: vi.fn(async () => cache),
    match: vi.fn(async (key: string | Request) => store.get(typeof key === "string" ? key : new URL(key.url).pathname)),
    keys: vi.fn(async () => []),
    delete: vi.fn(async () => true),
  };
  const self = {
    location: { origin: "https://hub.example" },
    addEventListener: (type: string, fn: Handler) => { handlers[type] = fn; },
    skipWaiting: vi.fn(), clients: { claim: vi.fn() },
  };
  const fetchFn = vi.fn((req: Request) => network(req.url));
  new Function("self", "caches", "fetch", SW)(self, caches, fetchFn);

  /** Dispatch a fetch event; resolves to the response the worker answered with, or null if it didn't. */
  async function request(path: string, mode: RequestMode = "navigate"): Promise<Response | null> {
    let answer: Promise<Response> | null = null;
    const req = { url: `https://hub.example${path}`, method: "GET", mode } as unknown as Request;
    handlers.fetch({ request: req, respondWith: (p: Promise<Response>) => { answer = p; }, waitUntil: () => undefined });
    if (!answer) return null;
    const res = await answer;
    await new Promise((r) => setTimeout(r, 0)); // let the background cache write finish
    return res;
  }
  return { request, store, cache };
}

const html = (body: string, status = 200) => new Response(body, { status, headers: { "Content-Type": "text/html; charset=utf-8" } });

describe("service worker", () => {
  it("serves the shell the network served last when the coordinator is down", async () => {
    let up = true;
    let version = "new";
    const sw = loadWorker(async () => {
      if (!up) throw new TypeError("Failed to fetch");
      return html(`<html>${version}</html>`);
    });
    sw.store.set("/index.html", html("<html>installed long ago</html>")); // cached when the worker was installed
    expect(await (await sw.request("/"))!.text()).toBe("<html>new</html>");
    version = "newer";
    await sw.request("/");
    up = false;
    expect(await (await sw.request("/"))!.text()).toBe("<html>newer</html>");
  });

  it("keeps the cached shell when the network answers with an error", async () => {
    const sw = loadWorker(async () => html("<html>bad gateway</html>", 502));
    sw.store.set("/index.html", html("<html>good</html>"));
    expect((await sw.request("/"))!.status).toBe(502);
    expect(await sw.store.get("/index.html")!.text()).toBe("<html>good</html>");
  });

  it("never touches API traffic", async () => {
    const sw = loadWorker(async () => html("x"));
    expect(await sw.request("/api/v1/nodes", "cors")).toBeNull();
    expect(await sw.request("/api/v1/system/health", "navigate")).toBeNull();
    expect(sw.cache.put).not.toHaveBeenCalled();
  });
});
