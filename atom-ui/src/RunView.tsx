import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, artifactUrl, Artifact, ChatMsg, Manifest } from "./api";
import { Dot, StatusPill, elapsed, fmtSize } from "./ui";

const IMG = /\.(png|jpe?g|gif|webp|svg|bmp)$/i;
const MD = /\.(md|markdown)$/i;

type Sel = { step: number; task: string };

function argSummary(args?: Record<string, unknown>): string {
  if (!args) return "";
  const a = args as Record<string, unknown>;
  if (Array.isArray(a.filepaths)) return (a.filepaths as unknown[]).join(", ");
  if (typeof a.path === "string") return a.path;
  return Object.keys(a).slice(0, 2).map((k) => `${k}=${String(a[k]).slice(0, 40)}`).join(", ");
}

export function RunView({ runId, onBack }: { runId: string; onBack: () => void }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [arts, setArts] = useState<Artifact[]>([]);
  const [sel, setSel] = useState<Sel | null>(null);
  const [tab, setTab] = useState<"transcript" | "deliverables">("transcript");
  const [openArt, setOpenArt] = useState<Artifact | null>(null);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const m = await api.run(runId).catch(() => null);
      if (live && m) {
        setManifest(m);
        api.artifacts(runId).then(setArts).catch(() => { /* ignore */ });
        if (m.status === "complete" || m.status === "halted") return;
      }
      if (live) timer = setTimeout(tick, 1500);
    };
    tick();
    return () => { live = false; clearTimeout(timer); };
  }, [runId]);

  // default-select the running (or first) task once the manifest arrives
  useEffect(() => {
    if (!manifest || sel) return;
    const flat = manifest.steps.flatMap((s) => s.tasks.map((t) => ({ step: s.index, task: t.id, status: t.status })));
    const running = flat.find((t) => t.status === "running");
    const pick = running ?? flat[0];
    if (pick) setSel({ step: pick.step, task: pick.task });
  }, [manifest, sel]);

  const doneSteps = manifest ? manifest.steps.filter((s) => s.status === "complete").length : 0;
  const curStep = manifest
    ? Math.min(manifest.status === "complete" ? doneSteps : doneSteps + 1, manifest.steps.length)
    : 0;

  return (
    <div className="runview">
      <div className="run-head">
        <div className="crumbs">
          <button className="link" onClick={onBack}>← Runs</button>
          <span className="sep">/</span>
          <b>{manifest?.workflow ?? runId}</b>
        </div>
        {manifest && (
          <div className="run-status">
            <StatusPill status={manifest.status} />
            <span className="dim">Step {curStep} of {manifest.steps.length}</span>
            <span className="dim">{elapsed(manifest.created_at, manifest.ended_at)}</span>
          </div>
        )}
      </div>

      {!manifest ? (
        <div className="loading">Loading…</div>
      ) : (
        <div className="run-body">
          <aside className="rail">
            {manifest.steps.map((s) => (
              <div key={s.index} className="rail-step">
                <div className="rail-step-head">
                  <span className="step-idx">Step {s.index + 1}</span> {s.title} <Dot status={s.status} />
                </div>
                {s.tasks.map((t) => (
                  <button key={t.id}
                    className={`rail-task${sel?.task === t.id && sel?.step === s.index ? " on" : ""}`}
                    onClick={() => { setSel({ step: s.index, task: t.id }); setTab("transcript"); }}>
                    <Dot status={t.status} />
                    <span className="rail-task-id">{t.id}</span>
                    {t.model && <span className="tag">{t.model}</span>}
                  </button>
                ))}
              </div>
            ))}
            <div className="rail-deliverables">
              <div className="rail-h">Deliverables</div>
              {arts.length === 0 && <div className="rail-empty">None yet</div>}
              {arts.map((a) => (
                <button key={`${a.step}-${a.task}-${a.rel}`}
                  className={`rail-art${openArt?.rel === a.rel ? " on" : ""}`}
                  onClick={() => { setOpenArt(a); setTab("deliverables"); }}>
                  <span className="art-name">{a.name}</span>
                  <span className="art-meta">{a.task} · {fmtSize(a.size)}</span>
                </button>
              ))}
            </div>
          </aside>

          <section className="center">
            <div className="tabbar">
              <button className={tab === "transcript" ? "on" : ""} onClick={() => setTab("transcript")}>Transcript</button>
              <button className={tab === "deliverables" ? "on" : ""} onClick={() => setTab("deliverables")}>
                Deliverables{arts.length ? ` (${arts.length})` : ""}
              </button>
            </div>
            {tab === "transcript"
              ? <Transcript runId={runId} sel={sel} status={manifest.status} />
              : <Deliverables runId={runId} arts={arts} open={openArt} setOpen={setOpenArt} />}
          </section>
        </div>
      )}
    </div>
  );
}

function Transcript({ runId, sel, status }: { runId: string; sel: Sel | null; status: string }) {
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [pending, setPending] = useState(false);
  useEffect(() => {
    if (!sel) { setChat([]); return; }
    let live = true;
    setPending(true);
    api.messages(runId, sel.step, sel.task)
      .then((m) => { if (live) setChat(m); })
      .catch(() => { if (live) setChat([]); })
      .finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [runId, sel?.step, sel?.task, status]);

  if (!sel) return <div className="placeholder">Select a task to view its transcript.</div>;
  if (pending && !chat.length) return <div className="placeholder">Loading transcript…</div>;
  if (!chat.length) return <div className="placeholder">No messages yet for {sel.task}.</div>;

  return (
    <div className="transcript">
      {chat.map((m, i) => m.tool_calls?.length ? (
        <div key={i} className="msg tool-calls">
          {m.text && <div className="msg-text">{m.text}</div>}
          {m.tool_calls.map((c, k) => (
            <div key={k} className={`toolcall${c.name === "present_files" ? " present" : ""}`}>
              <span className="tc-name">{c.name === "present_files" ? "⇪ present_files" : `→ ${c.name}`}</span>
              <span className="tc-args">{argSummary(c.args)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div key={i} className={`msg ${m.role}`}>
          <div className="msg-role">{m.name || m.role}</div>
          <div className="msg-text">{m.text}</div>
        </div>
      ))}
    </div>
  );
}

function Deliverables(
  { runId, arts, open, setOpen }:
  { runId: string; arts: Artifact[]; open: Artifact | null; setOpen: (a: Artifact | null) => void },
) {
  if (!arts.length) return <div className="placeholder">No deliverables presented yet.</div>;
  if (open) {
    return (
      <div className="viewer">
        <div className="viewer-head">
          <button className="link" onClick={() => setOpen(null)}>← All deliverables</button>
          <span className="viewer-name">{open.name}</span>
          <span className="dim">step {open.step} · {open.task} · {fmtSize(open.size)}</span>
        </div>
        <ArtifactBody runId={runId} art={open} />
      </div>
    );
  }
  return (
    <div className="gallery">
      {arts.map((a) => (
        <button key={`${a.step}-${a.task}-${a.rel}`} className="gal-card" onClick={() => setOpen(a)}>
          <div className="gal-name">{a.name}</div>
          <div className="gal-meta">step {a.step} · {a.task}</div>
          <div className="gal-size">{fmtSize(a.size)}</div>
        </button>
      ))}
    </div>
  );
}

function ArtifactBody({ runId, art }: { runId: string; art: Artifact }) {
  const isImg = IMG.test(art.name);
  const isMd = MD.test(art.name);
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (isImg) return;
    let live = true;
    setText(null); setErr("");
    api.artifactText(runId, art.rel).then((t) => { if (live) setText(t); }).catch((e) => { if (live) setErr(String(e)); });
    return () => { live = false; };
  }, [runId, art.rel, isImg]);

  if (isImg) return <div className="art-img"><img src={artifactUrl(runId, art.rel)} alt={art.name} /></div>;
  if (err) return <div className="error">{err}</div>;
  if (text === null) return <div className="placeholder">Loading…</div>;
  if (isMd) return <div className="art-md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>;
  return <pre className="art-code">{text}</pre>;
}
