// Thin, typed wrapper around the coordinator HTTP API.
// - Cookies carry the session (same origin); every state-changing request sends the CSRF header.
// - Errors are normalised to ApiError with the server's stable `code`, message and hint.
// - Chat streaming uses fetch + ReadableStream to parse Server-Sent Events from a POST response.

import type { UpdateApplyStarted, UpdateCheckStarted, UpdateDismissed } from "./types";

export class ApiError extends Error {
  code: string;
  hint?: string;
  status: number;
  details?: unknown;
  constructor(status: number, code: string, message: string, hint?: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.hint = hint;
    this.details = details;
  }
}

const CSRF = { "X-HomeMesh-CSRF": "1" };

async function parseError(res: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const err = (body as { error?: { code?: string; message?: string; hint?: string; details?: unknown } })?.error;
  if (err) return new ApiError(res.status, err.code ?? "error", err.message ?? res.statusText, err.hint, err.details);
  if (res.status === 502 || res.status === 503 || res.status === 504)
    return new ApiError(res.status, "coordinator_unreachable", "The HomeMesh coordinator is not responding.");
  return new ApiError(res.status, `http_${res.status}`, res.statusText || "Request failed");
}

export type Listener = () => void;
const authListeners = new Set<Listener>();
export function onUnauthorized(fn: Listener): () => void {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}

export async function request<T>(method: string, path: string, body?: unknown, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...(method !== "GET" ? CSRF : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      ...init,
    });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the HomeMesh coordinator. Check that it is running and that this device is on the same network.");
  }
  if (!res.ok) {
    const err = await parseError(res);
    if (res.status === 401 && !path.startsWith("/api/v1/auth/")) authListeners.forEach((fn) => fn());
    throw err;
  }
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? res.json() : res.text()) as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, body ?? {}),
  del: <T>(path: string) => request<T>("DELETE", path),
  async upload<T>(path: string, file: Blob): Promise<T> {
    let res: Response;
    try {
      res = await fetch(path, { method: "POST", credentials: "same-origin", headers: { ...CSRF, "Content-Type": file.type || "application/octet-stream" }, body: file });
    } catch {
      throw new ApiError(0, "network_error", "Upload failed: coordinator unreachable.");
    }
    if (!res.ok) throw await parseError(res);
    return res.json() as Promise<T>;
  },
};

const nodePath = (nodeId: string, action: string) => `/api/v1/nodes/${encodeURIComponent(nodeId)}/update/${action}`;

/** Remote updates of one computer (owner only; see docs/API.md). Errors are ApiError with the server's code. */
export const updatesApi = {
  /** Start a check on that computer; the result arrives later in the node list (`update_check`). */
  check: (nodeId: string) => api.post<UpdateCheckStarted>(nodePath(nodeId, "check")),
  /** Install exactly `expectedCommit` (the commit the last check showed). */
  apply: (nodeId: string, expectedCommit: string, force: boolean) =>
    api.post<UpdateApplyStarted>(nodePath(nodeId, "apply"), { expected_commit: expectedCommit, force }),
  /** Give up waiting for an update that stopped reporting (allowed after 15 minutes without word). */
  dismiss: (nodeId: string) => api.post<UpdateDismissed>(nodePath(nodeId, "dismiss")),
};

/** True if the coordinator answers `GET /api/v1/system/health` (unauthenticated) within `timeoutMs`.
 * Never throws; used while the hub restarts for an update. */
export async function checkHealth(timeoutMs = 5000): Promise<boolean> {
  const ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : undefined;
  try {
    const res = await fetch("/api/v1/system/health", { credentials: "same-origin", cache: "no-store", signal: ctrl?.signal });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export interface SSEEvent {
  event: string;
  data: unknown;
}

/** Parse a text/event-stream body. Yields {event, data} with JSON-decoded data. */
export async function* readSSE(res: Response, signal?: AbortSignal): AsyncGenerator<SSEEvent> {
  if (!res.body) return;
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      if (signal?.aborted) return;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.search(/\r?\n\r?\n/)) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx).replace(/^\r?\n\r?\n/, "");
        let event = "message";
        const dataLines: string[] = [];
        for (const line of raw.split(/\r?\n/)) {
          if (line.startsWith(":")) continue;
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
        }
        if (!dataLines.length) continue;
        const text = dataLines.join("\n");
        let data: unknown = text;
        try {
          data = JSON.parse(text);
        } catch {
          /* keep text */
        }
        yield { event, data };
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function streamPost(path: string, body: unknown, signal?: AbortSignal): Promise<Response> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...CSRF },
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw e;
    throw new ApiError(0, "network_error", "Cannot reach the HomeMesh coordinator.");
  }
  if (!res.ok) throw await parseError(res);
  return res;
}

export async function streamGet(path: string, signal?: AbortSignal): Promise<Response> {
  const res = await fetch(path, { credentials: "same-origin", headers: { Accept: "text/event-stream" }, signal });
  if (!res.ok) throw await parseError(res);
  return res;
}
