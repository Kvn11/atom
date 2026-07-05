import { useEffect, useState } from "react";
import { api, Workflow } from "./api";
import { Workflows, RunForm } from "./Workflows";
import { RunsDashboard } from "./RunsDashboard";
import { RunView } from "./RunView";

type View =
  | { name: "workflows" }
  | { name: "form"; workflow: Workflow }
  | { name: "runs" }
  | { name: "run"; runId: string };

export default function App() {
  const [view, setView] = useState<View>({ name: "workflows" });
  const [active, setActive] = useState(0);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try { const p = await api.runs("all", 1, 0); if (live) setActive(p.counts.active); } catch { /* ignore */ }
      if (live) timer = setTimeout(tick, 4000);
    };
    tick();
    return () => { live = false; clearTimeout(timer); };
  }, []);

  const tab = view.name === "form" ? "workflows" : view.name === "run" ? "runs" : view.name;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand" onClick={() => setView({ name: "workflows" })}>
          <span className="glyph">⚛</span> atom
        </div>
        <nav className="tabs">
          <button className={tab === "workflows" ? "on" : ""} onClick={() => setView({ name: "workflows" })}>Workflows</button>
          <button className={tab === "runs" ? "on" : ""} onClick={() => setView({ name: "runs" })}>
            Runs{active > 0 && <span className="count">{active}</span>}
          </button>
        </nav>
      </header>
      <main>
        {view.name === "workflows" && <Workflows onPick={(w) => setView({ name: "form", workflow: w })} />}
        {view.name === "form" && (
          <RunForm workflow={view.workflow} onStarted={(id) => setView({ name: "run", runId: id })}
            onBack={() => setView({ name: "workflows" })} />
        )}
        {view.name === "runs" && <RunsDashboard onOpen={(id) => setView({ name: "run", runId: id })} />}
        {view.name === "run" && <RunView runId={view.runId} onBack={() => setView({ name: "runs" })} />}
      </main>
    </div>
  );
}
