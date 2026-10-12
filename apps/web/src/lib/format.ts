export function bytes(n?: number | null, digits = 1): string {
  if (n === null || n === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}

export function mb(n?: number | null): string {
  return n === null || n === undefined ? "—" : bytes(n * 1024 * 1024);
}

export function ms(n?: number | null): string {
  if (n === null || n === undefined) return "—";
  if (n < 1000) return `${Math.round(n)} ms`;
  if (n < 60000) return `${(n / 1000).toFixed(n < 10000 ? 2 : 1)} s`;
  return `${Math.floor(n / 60000)}m ${Math.round((n % 60000) / 1000)}s`;
}

export function pct(n?: number | null): string {
  return n === null || n === undefined ? "—" : `${Math.round(n)}%`;
}

export function num(n?: number | null, digits = 0): string {
  return n === null || n === undefined ? "—" : n.toFixed(digits);
}

export function parseTs(ts?: string | null): Date | null {
  if (!ts) return null;
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function relTime(ts?: string | null): string {
  const d = parseTs(ts);
  if (!d) return "—";
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
  return d.toLocaleDateString();
}

export function dateTime(ts?: string | null): string {
  const d = parseTs(ts);
  return d ? d.toLocaleString() : "—";
}

export function duration(start?: string | null, end?: string | null): string {
  const a = parseTs(start);
  const b = parseTs(end) ?? (a ? new Date() : null);
  if (!a || !b) return "—";
  return ms(b.getTime() - a.getTime());
}

export function uptime(s?: number | null): string {
  if (s === null || s === undefined) return "—";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}
