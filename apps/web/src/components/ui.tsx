import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row nowrap" role="status">
      <span className="spinner" aria-hidden="true" />
      {label && <span className="muted small">{label}</span>}
    </span>
  );
}

export function Badge({ kind, children, title }: { kind?: string; children: ReactNode; title?: string }) {
  return (
    <span className={`badge ${kind ?? ""}`} title={title}>
      {children}
    </span>
  );
}

export function MockBadge() {
  return (
    <Badge kind="mock" title="Produced by the mock test backend - no AI model ran">
      MOCK
    </Badge>
  );
}

export function StatusDot({ status }: { status: "ok" | "warn" | "danger" | "off" }) {
  return <span className={`dot ${status === "off" ? "" : status}`} aria-hidden="true" />;
}

export function Meter({ value, max = 100, label, detail }: { value?: number | null; max?: number; label: string; detail?: string }) {
  const known = value !== null && value !== undefined && max > 0;
  const p = known ? Math.max(0, Math.min(100, ((value as number) / max) * 100)) : 0;
  const cls = p > 90 ? "danger" : p > 75 ? "warn" : "";
  return (
    <div className="metric">
      <div className="label">
        <span>{label}</span>
        <span>{detail ?? (known ? `${Math.round(p)}%` : "not reported")}</span>
      </div>
      <div className={`meter ${cls}`} role="meter" aria-label={label} aria-valuenow={known ? Math.round(p) : undefined} aria-valuemin={0} aria-valuemax={100}>
        <span style={{ width: `${p}%` }} />
      </div>
    </div>
  );
}

export function Empty({ icon, title, children }: { icon?: string; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      {icon && <div className="big" aria-hidden="true">{icon}</div>}
      <div style={{ fontWeight: 600, color: "var(--text)" }}>{title}</div>
      {children && <div className="small" style={{ marginTop: 6 }}>{children}</div>}
    </div>
  );
}

export function ErrorAlert({ error }: { error: unknown }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div className="alert danger" role="alert">
      {e.message ?? String(error)}
      {e.hint && <span className="hint">{e.hint}</span>}
    </div>
  );
}

export function Modal({ title, children, onClose, wide }: { title: string; children: ReactNode; onClose: () => void; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <div className="card-head">
          <h2>{title}</h2>
          <span className="spacer" />
          <button className="btn ghost sm icon-only" onClick={onClose} aria-label="Close">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function ConfirmButton({ onConfirm, children, message, className = "btn danger sm", disabled }: {
  onConfirm: () => void | Promise<void>; children: ReactNode; message: string; className?: string; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  return (
    <>
      <button className={className} onClick={() => setOpen(true)} disabled={disabled}>{children}</button>
      {open && (
        <Modal title="Please confirm" onClose={() => setOpen(false)}>
          <p>{message}</p>
          <div className="modal-actions">
            <button className="btn" onClick={() => setOpen(false)}>Cancel</button>
            <button className="btn danger solid" disabled={busy} onClick={async () => {
              setBusy(true);
              try { await onConfirm(); setOpen(false); } finally { setBusy(false); }
            }}>{busy ? "Working…" : "Confirm"}</button>
          </div>
        </Modal>
      )}
    </>
  );
}

export function CopyButton({ text, label = "Copy", className = "btn ghost sm" }: { text: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className={className} title={label} onClick={async () => {
      try {
        await navigator.clipboard.writeText(text);
      } catch {
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
      }
      setDone(true);
      setTimeout(() => setDone(false), 1200);
    }}>{done ? "Copied" : label}</button>
  );
}

// ---------------------------------------------------------------- toasts
interface Toast { id: number; kind: "info" | "ok" | "error"; text: string; hint?: string }
const ToastCtx = createContext<(kind: Toast["kind"], text: string, hint?: string) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: Toast["kind"], text: string, hint?: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-3), { id, kind, text, hint }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 8000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.kind}`}>
            {t.text}
            {t.hint && <div className="small muted">{t.hint}</div>}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  const push = useContext(ToastCtx);
  return {
    ok: (text: string) => push("ok", text),
    info: (text: string) => push("info", text),
    error: (e: unknown) => {
      const err = e as ApiError;
      push("error", err?.message ?? String(e), err?.hint);
    },
  };
}

/** Load data with loading/error state and a reload function. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []): { data: T | null; error: unknown; loading: boolean; reload: () => Promise<void>; setData: (d: T) => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    try {
      const d = await loader();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    setLoading(true);
    void reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}
