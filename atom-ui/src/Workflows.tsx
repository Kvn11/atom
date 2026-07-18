import { useEffect, useState } from "react";
import { api, Workflow } from "./api";

export function Workflows({ onPick }: { onPick: (w: Workflow) => void }) {
  const [wfs, setWfs] = useState<Workflow[]>([]);
  const [err, setErr] = useState("");
  useEffect(() => { api.workflows().then(setWfs).catch((e) => setErr(String(e))); }, []);
  return (
    <div className="page">
      <h1>Workflows</h1>
      <p className="sub">Pick a workflow to configure and launch a run.</p>
      {err && <div className="error">{err}</div>}
      <div className="grid">
        {wfs.map((w) => (
          <button key={w.name} className="wf-card" onClick={() => onPick(w)}>
            <div className="wf-name">{w.name}</div>
            <div className="wf-desc">{w.description || "—"}</div>
            <div className="wf-meta">{w.inputs.length} input{w.inputs.length === 1 ? "" : "s"}</div>
          </button>
        ))}
        {!wfs.length && !err && <div className="empty">No workflows found.</div>}
      </div>
    </div>
  );
}

export function RunForm(
  { workflow, onStarted, onBack }:
  { workflow: Workflow; onStarted: (id: string) => void; onBack: () => void },
) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<Record<string, File>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [notesMsg, setNotesMsg] = useState("");
  const submit = async () => {
    setError(""); setBusy(true);
    try { const { run_id } = await api.submit(workflow.name, values, files); onStarted(run_id); }
    catch (e) { setError(String(e instanceof Error ? e.message : e)); setBusy(false); }
  };
  const clearNotes = async () => {
    if (!window.confirm(`Delete the persistent notes vault for "${workflow.name}"?`)) return;
    setNotesMsg("");
    try {
      const { cleared } = await api.clearNotes(workflow.name);
      setNotesMsg(cleared ? "Notes vault cleared." : "No notes vault existed.");
    } catch (e) {
      setNotesMsg(String(e instanceof Error ? e.message : e));
    }
  };
  return (
    <div className="page narrow">
      <button className="link" onClick={onBack}>← Workflows</button>
      <h1>{workflow.name}</h1>
      {workflow.description && <p className="sub">{workflow.description}</p>}
      {workflow.inputs.map((i) => (
        <label key={i.name} className="field">
          <span className="field-label">{i.name}{i.required && <span className="req">required</span>}</span>
          {i.description && <span className="field-hint">{i.description}</span>}
          {i.type === "file" ? (
            <input type="file"
              onChange={(e) => {
                const f = e.target.files?.[0];
                setFiles((prev) => {
                  const next = { ...prev };
                  if (f) next[i.name] = f; else delete next[i.name];
                  return next;
                });
              }} />
          ) : (
            <input placeholder={i.default ?? ""} value={values[i.name] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [i.name]: e.target.value }))} />
          )}
        </label>
      ))}
      <button className="primary" disabled={busy} onClick={submit}>{busy ? "Starting…" : "Start run"}</button>
      {workflow.notes_enabled && (
        <div className="notes-actions">
          <button className="link" onClick={clearNotes}>Clear notes vault</button>
          {notesMsg && <span className="field-hint">{notesMsg}</span>}
        </div>
      )}
      {error && <div className="error">{error}</div>}
    </div>
  );
}
