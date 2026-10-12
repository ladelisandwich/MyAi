import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";

// Live updates from the coordinator (/api/v1/events, Server-Sent Events). Pages subscribe to
// event types to refresh their data - this is also how a conversation started on one device
// appears on another.
type Handler = (data: Record<string, unknown>) => void;

interface EventsCtx {
  subscribe: (types: string[], handler: Handler) => () => void;
  connected: boolean;
}

const Ctx = createContext<EventsCtx>({ subscribe: () => () => undefined, connected: false });

const TYPES = [
  "node.connected", "node.disconnected", "node.updated", "node.telemetry", "node.models", "node.enrolled", "node.revoked",
  "job.created", "job.updated", "job.progress", "conversation.created", "conversation.updated", "conversation.deleted",
  "memory.changed", "knowledge.changed", "settings.updated", "approval.requested", "approval.decided", "approval.expired",
  "project.changed", "resync",
];

export function EventsProvider({ children }: { children: ReactNode }) {
  const handlers = useRef(new Map<string, Set<Handler>>());
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let es: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let delay = 1000;
    let stopped = false;
    const open = () => {
      if (stopped || typeof EventSource === "undefined") return;
      es = new EventSource("/api/v1/events", { withCredentials: true });
      es.addEventListener("hello", () => {
        setConnected(true);
        delay = 1000;
      });
      for (const t of TYPES) {
        es.addEventListener(t, (ev) => {
          let data: Record<string, unknown> = {};
          try {
            data = JSON.parse((ev as MessageEvent).data);
          } catch {
            /* ignore */
          }
          handlers.current.get(t)?.forEach((h) => h(data));
          handlers.current.get("*")?.forEach((h) => h({ ...data, _type: t }));
        });
      }
      es.onerror = () => {
        setConnected(false);
        es?.close();
        retry = setTimeout(open, delay);
        delay = Math.min(delay * 2, 30000);
      };
    };
    open();
    return () => {
      stopped = true;
      clearTimeout(retry);
      es?.close();
    };
  }, []);

  const subscribe = (types: string[], handler: Handler) => {
    for (const t of types) {
      if (!handlers.current.has(t)) handlers.current.set(t, new Set());
      handlers.current.get(t)!.add(handler);
    }
    return () => types.forEach((t) => handlers.current.get(t)?.delete(handler));
  };

  return <Ctx.Provider value={{ subscribe, connected }}>{children}</Ctx.Provider>;
}

export function useEvents(): EventsCtx {
  return useContext(Ctx);
}

/** Re-run `fn` whenever any of the given event types arrives (debounced). */
export function useLiveRefresh(types: string[], fn: () => void, debounceMs = 300): void {
  const { subscribe } = useEvents();
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    let t: ReturnType<typeof setTimeout> | undefined;
    const unsub = subscribe([...types, "resync"], () => {
      clearTimeout(t);
      t = setTimeout(() => fnRef.current(), debounceMs);
    });
    return () => {
      clearTimeout(t);
      unsub();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [types.join(","), debounceMs]);
}
