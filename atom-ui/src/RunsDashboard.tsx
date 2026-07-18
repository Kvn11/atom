import { useEffect, useState } from "react";
import { api, RunsPage } from "./api";
import { StatusPill, elapsed, fmtClock, progressText } from "./ui";

const PAGE = 50;
const FILTERS = ["active", "complete", "halted", "cancelled", "all"] as const;

export function RunsDashboard({ onOpen }: { onOpen: (id: string) => void }) {
  const [status, setStatus] = useState<string>("active");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<RunsPage | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => { setOffset(0); }, [status]);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const ctrl = new AbortController();
    const tick = async () => {
      try {
        const p = await api.runs(status, PAGE, offset, ctrl.signal);
        if (live) { setData(p); setErr(""); }
      } catch (e) {
        if (live && (e as Error).name !== "AbortError") setErr(String(e));
      }
      if (live) timer = setTimeout(tick, 2500);
    };
    tick();
    return () => { live = false; ctrl.abort(); clearTimeout(timer); };
  }, [status, offset]);

  const counts = data?.counts;
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="page wide">
      <h1>Runs</h1>
      <div className="filters">
        {FILTERS.map((f) => (
          <button key={f} className={status === f ? "chip on" : "chip"} onClick={() => setStatus(f)}>
            {f}{counts && f !== "all" ? <span className="chip-n">{counts[f as "active" | "complete" | "halted" | "cancelled"]}</span> : null}
          </button>
        ))}
      </div>
      {err && <div className="error">{err}</div>}
      <table className="runs">
        <thead>
          <tr><th>Status</th><th>Workflow</th><th>Progress</th><th>Started</th><th>Elapsed</th></tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.run_id} onClick={() => onOpen(r.run_id)}>
              <td><StatusPill status={r.status} /></td>
              <td className="mono-cell">{r.workflow}<div className="rid">{r.run_id}</div></td>
              <td>{progressText(r)}</td>
              <td className="dim">{fmtClock(r.created_at)}</td>
              <td className="dim">{elapsed(r.created_at, r.ended_at)}</td>
            </tr>
          ))}
          {!items.length && (
            <tr><td colSpan={5} className="empty">No {status === "all" ? "" : status} runs.</td></tr>
          )}
        </tbody>
      </table>
      <div className="pager">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>← Prev</button>
        <span className="dim">{total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, total)}`} of {total}</span>
        <button disabled={offset + PAGE >= total} onClick={() => setOffset(offset + PAGE)}>Next →</button>
      </div>
    </div>
  );
}
