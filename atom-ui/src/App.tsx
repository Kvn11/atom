import { useEffect, useState } from "react";
import { api, Artifact, ChatMsg, Manifest, Workflow } from "./api";

type View =
  | { name: "list" }
  | { name: "form"; workflow: Workflow }
  | { name: "run"; runId: string };

export default function App() {
  const [view, setView] = useState<View>({ name: "list" });
  return (
    <div className="app">
      <header onClick={() => setView({ name: "list" })}>⚛ atom workflows</header>
      {view.name === "list" && <WorkflowList onPick={(w) => setView({ name: "form", workflow: w })} />}
      {view.name === "form" && (
        <RunForm workflow={view.workflow} onStarted={(id) => setView({ name: "run", runId: id })} />
      )}
      {view.name === "run" && <RunView runId={view.runId} />}
    </div>
  );
}

function WorkflowList({ onPick }: { onPick: (w: Workflow) => void }) {
  const [wfs, setWfs] = useState<Workflow[]>([]);
  useEffect(() => { api.workflows().then(setWfs).catch(console.error); }, []);
  return (
    <div className="panel">
      <h2>Workflows</h2>
      {wfs.map((w) => (
        <div key={w.name} className="card" onClick={() => onPick(w)}>
          <strong>{w.name}</strong>
          <div className="dim">{w.description}</div>
        </div>
      ))}
    </div>
  );
}

function RunForm({ workflow, onStarted }: { workflow: Workflow; onStarted: (id: string) => void }) {
  const [values, setValues] = useState<Record<string, string>>({});
  const submit = async () => {
    const { run_id } = await api.submit(workflow.name, values);
    onStarted(run_id);
  };
  return (
    <div className="panel">
      <h2>{workflow.name}</h2>
      {workflow.inputs.map((i) => (
        <label key={i.name} className="field">
          {i.name}{i.required ? " *" : ""}
          <input
            placeholder={i.default ?? ""}
            onChange={(e) => setValues((v) => ({ ...v, [i.name]: e.target.value }))}
          />
        </label>
      ))}
      <button onClick={submit}>Start run</button>
    </div>
  );
}

function RunView({ runId }: { runId: string }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [sel, setSel] = useState<{ step: number; task: string } | null>(null);
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [arts, setArts] = useState<Artifact[]>([]);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      const m = await api.run(runId).catch(() => null);
      if (live && m) {
        setManifest(m);
        api.artifacts(runId).then(setArts).catch(() => {});
        if (m.status === "complete" || m.status === "halted") return;
      }
      if (live) setTimeout(tick, 1500);
    };
    tick();
    return () => { live = false; };
  }, [runId]);

  useEffect(() => {
    if (!sel) return;
    api.messages(runId, sel.step, sel.task).then(setChat).catch(() => setChat([]));
  }, [sel, runId, manifest?.status]);

  if (!manifest) return <div className="panel">Loading…</div>;
  return (
    <div className="run">
      <div className="steps">
        <h2>{manifest.workflow} <span className={`badge ${manifest.status}`}>{manifest.status}</span></h2>
        {manifest.steps.map((s) => (
          <div key={s.index} className="step">
            <div className="step-title">{s.title} <span className="dim">{s.status}</span></div>
            {s.tasks.map((t) => (
              <div
                key={t.id}
                className={`task ${t.status} ${sel?.task === t.id && sel?.step === s.index ? "active" : ""}`}
                onClick={() => setSel({ step: s.index, task: t.id })}
              >
                {t.id} <span className="dim">{t.status}</span>
              </div>
            ))}
          </div>
        ))}
        <h3>Artifacts</h3>
        {arts.map((a) => (
          <div key={a.path} className="artifact" onClick={() => api.artifact(runId, a.path).then(alert)}>
            {a.path} <span className="dim">{a.size}b</span>
          </div>
        ))}
      </div>
      <div className="chat">
        <h3>{sel ? `${sel.task} (step ${sel.step})` : "Select a task"}</h3>
        {chat.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="role">{m.name || m.role}</div>
            <div className="text">{m.text || (m.tool_calls ? `→ ${m.tool_calls.map((c) => c.name).join(", ")}` : "")}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
