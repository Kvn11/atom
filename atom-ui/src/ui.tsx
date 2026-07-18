import { RunSummary } from "./api";

export const STATUS_CLASS: Record<string, string> = {
  pending: "idle", running: "warn", succeeded: "ok", failed: "err",
  complete: "ok", halted: "err", cancelled: "idle",
};

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill ${STATUS_CLASS[status] ?? "idle"}`}>{status}</span>;
}

export function Dot({ status }: { status: string }) {
  return <span className={`dot ${STATUS_CLASS[status] ?? "idle"}`} title={status} />;
}

export const fmtSize = (b: number) =>
  b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`;

export const fmtClock = (iso?: string) => (iso ? iso.replace("T", " ") : "");

export function elapsed(start?: string, end?: string): string {
  if (!start) return "";
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const sec = Math.max(0, Math.round((e - s) / 1000));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m}m ${sec % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export const progressText = (r: RunSummary) =>
  `${r.tasks_done}/${r.tasks_total} tasks` + (r.current_step ? ` · ${r.current_step}` : "");
