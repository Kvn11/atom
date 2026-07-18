import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { api, artifactUrl, exportDownloadUrl, Artifact, ChatMsg, Manifest, StreamBlock, Todo } from "./api";
import { currentPlan } from "./plan";
import { Dot, StatusPill, elapsed, fmtSize } from "./ui";

const IMG = /\.(png|jpe?g|gif|webp|svg|bmp|avif|apng|jfif|ico)$/i;
const MD = /\.(md|markdown)$/i;
const PDF = /\.pdf$/i;
const MAX_INLINE = 2_000_000; // bytes — larger deliverables get a download card instead of inline render

// highlight.js language keyed by file extension; unknown/plain text falls back to "plaintext" (no tokens).
const LANG: Record<string, string> = {
  py: "python", pyi: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
  jsx: "javascript", ts: "typescript", tsx: "typescript", json: "json", jsonc: "json",
  yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", cfg: "ini", conf: "ini", env: "bash",
  sh: "bash", bash: "bash", zsh: "bash", go: "go", rs: "rust", rb: "ruby", java: "java",
  kt: "kotlin", c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp",
  cs: "csharp", php: "php", swift: "swift", scala: "scala", sql: "sql", r: "r",
  html: "xml", htm: "xml", xml: "xml", vue: "xml", css: "css", scss: "scss",
  less: "less", diff: "diff", patch: "diff", dockerfile: "dockerfile", makefile: "makefile",
  graphql: "graphql", proto: "protobuf", tex: "latex",
};

function extOf(name: string): string {
  const base = name.slice(name.lastIndexOf("/") + 1).toLowerCase();
  if (base === "dockerfile" || base === "makefile") return base;
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1) : "";
}

const langFor = (name: string): string => LANG[extOf(name)] ?? "plaintext";

// Wrap raw file text in a fenced code block whose fence is longer than any backtick run inside it,
// so the file's own content can never terminate the fence early (safe; nothing leaks to markdown).
function asFence(code: string, lang: string): string {
  const longest = (code.match(/`+/g) ?? []).reduce((m, s) => Math.max(m, s.length), 0);
  const fence = "`".repeat(Math.max(3, longest + 1));
  return `${fence}${lang}\n${code}\n${fence}`;
}

// A NUL byte or Unicode replacement char is a strong signal the fetched bytes weren't UTF-8 text
// (e.g. a PDF/zip/office doc presented as a deliverable) — show a download card, not mojibake.
const looksBinary = (text: string): boolean => /[\u0000\uFFFD]/.test(text.slice(0, 4096));

// Open external links in a new tab safely; leave in-app/relative links as default anchors.
const mdComponents: Components = {
  a: ({ href, children, ...props }) => {
    const external = !!href && /^https?:\/\//i.test(href);
    return (
      <a href={href} target={external ? "_blank" : undefined}
        rel={external ? "noopener noreferrer" : undefined} {...props}>{children}</a>
    );
  },
};

// The one markdown pipeline, shared by deliverable bodies and assistant transcript messages
// (GFM + syntax-highlight + safe links). Callers own the container/`.md` class and layout.
function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
      components={mdComponents}
    >
      {children}
    </ReactMarkdown>
  );
}

type Sel = { step: number; task: string };

// The most-recently presented file set for a task: the last present_files call's `filepaths`,
// resolved to this task's captured artifacts by virtual `path` (capture preserves `path` even when
// basenames collide, so joining on it — not `name` — is exact). Missing/unmatched paths drop out.
function presentedSetFor(chat: ChatMsg[], arts: Artifact[], sel: Sel | null): Artifact[] {
  if (!sel) return [];
  const taskArts = arts.filter((a) => a.step === sel.step && a.task === sel.task);
  if (!taskArts.length) return [];
  const byPath = new Map(taskArts.map((a) => [a.path, a]));
  let last: string[] | null = null;
  for (const m of chat) {
    for (const c of m.tool_calls ?? []) {
      if (c.name !== "present_files") continue;
      const fps = c.args?.filepaths;
      if (Array.isArray(fps)) last = fps.filter((x): x is string => typeof x === "string");
    }
  }
  return last ? last.map((p) => byPath.get(p)).filter((a): a is Artifact => !!a) : [];
}

function argSummary(args?: Record<string, unknown>): string {
  if (!args) return "";
  const a = args as Record<string, unknown>;
  if (Array.isArray(a.filepaths)) return (a.filepaths as unknown[]).join(", ");
  if (typeof a.path === "string") return a.path;
  return Object.keys(a).slice(0, 2).map((k) => `${k}=${String(a[k]).slice(0, 40)}`).join(", ");
}

export function RunView({ runId, onBack, onOpenRun }:
  { runId: string; onBack: () => void; onOpenRun?: (id: string) => void }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [arts, setArts] = useState<Artifact[]>([]);
  const [sel, setSel] = useState<Sel | null>(null);
  const [tab, setTab] = useState<"transcript" | "deliverables">("transcript");
  const [openArt, setOpenArt] = useState<Artifact | null>(null);
  const [exporting, setExporting] = useState<"run" | "task" | null>(null);
  const [exportMsg, setExportMsg] = useState<{ text: string; kind: "ok" | "warn" | "err"; href?: string } | null>(null);
  const [improving, setImproving] = useState(false);
  const [improveMsg, setImproveMsg] = useState<{ text: string; kind: "ok" | "err"; runId?: string } | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelMsg, setCancelMsg] = useState<{ text: string; kind: "ok" | "err" } | null>(null);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const m = await api.run(runId).catch(() => null);
      if (live && m) {
        setManifest(m);
        api.artifacts(runId).then((a) => { if (live) setArts(a); }).catch(() => { /* ignore */ });
        if (m.status === "complete" || m.status === "halted" || m.status === "cancelled") return;
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

  const selTask = manifest && sel
    ? manifest.steps.find((s) => s.index === sel.step)?.tasks.find((t) => t.id === sel.task) ?? null
    : null;
  const taskTerminal = selTask?.status === "succeeded" || selTask?.status === "failed";

  const runExport = async (body?: { step: number; task: string }) => {
    setExporting(body ? "task" : "run");
    setExportMsg(null);
    try {
      const res = await api.exportRun(runId, body);
      if (res.fetched_roots === 0) {
        setExportMsg({ text: "No traces found — was observability enabled for this run?", kind: "warn" });
      } else {
        const what = res.scope === "task" ? `task ${res.task_id}` : "run";
        const partial = res.complete ? "" : ` (partial: ${res.fetched_roots}/${res.expected_roots})`;
        setExportMsg({
          text: `Exported ${what} → ${res.path}${partial}`,
          kind: res.complete ? "ok" : "warn",
          href: exportDownloadUrl(runId, body),
        });
      }
    } catch (e) {
      setExportMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setExporting(null);
    }
  };

  const runSelfImprove = async () => {
    setImproving(true);
    setImproveMsg(null);
    try {
      const res = await api.selfImprove(runId);
      setImproveMsg({ text: "Self-improvement run started", kind: "ok", runId: res.run_id });
    } catch (e) {
      setImproveMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setImproving(false);
    }
  };

  const cancelRun = async () => {
    if (!window.confirm("Cancel this run? The current step finishes, then it stops.")) return;
    setCancelling(true);
    setCancelMsg(null);
    try {
      await api.cancel(runId);
    } catch (e) {
      setCancelMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setCancelling(false);
    }
  };

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
            {manifest.cancel_requested && manifest.status === "running"
              ? <span className="pill warn">cancelling…</span>
              : <StatusPill status={manifest.status} />}
            <span className="dim">Step {curStep} of {manifest.steps.length}</span>
            <span className="dim">{elapsed(manifest.created_at, manifest.ended_at)}</span>
            {(manifest.status === "pending" || manifest.status === "queued"
              || (manifest.status === "running" && !manifest.cancel_requested)) && (
              <button className="btn-sm" disabled={cancelling}
                onClick={() => cancelRun()}
                title="Stop this run at the next step boundary">
                {cancelling ? "Cancelling…" : "Cancel run"}
              </button>
            )}
            <button className="btn-sm" disabled={manifest.status !== "complete" || exporting !== null}
              onClick={() => runExport()}
              title={manifest.status === "complete"
                ? "Download this run's LangSmith traces"
                : "Available once all steps complete"}>
              {exporting === "run" ? "Exporting…" : "Export run"}
            </button>
            {manifest.workflow !== "self-improve" && (
              <button className="btn-sm"
                disabled={!(manifest.status === "complete" || manifest.status === "halted") || improving}
                onClick={() => runSelfImprove()}
                title={(manifest.status === "complete" || manifest.status === "halted")
                  ? "Analyze this run and draft an improved workflow"
                  : "Available once the run finishes"}>
                {improving ? "Improving…" : "Improve"}
              </button>
            )}
          </div>
        )}
      </div>

      {exportMsg && (
        <div className={`export-banner ${exportMsg.kind}`}>
          <span className="export-text">{exportMsg.text}</span>
          {exportMsg.href && (
            <a className="export-dl" href={exportMsg.href} download>Download export ↓</a>
          )}
          <button className="export-x" onClick={() => setExportMsg(null)} title="Dismiss">✕</button>
        </div>
      )}

      {improveMsg && (
        <div className={`export-banner ${improveMsg.kind}`}>
          <span className="export-text">{improveMsg.text}</span>
          {improveMsg.runId && onOpenRun && (
            <button className="export-dl" onClick={() => onOpenRun(improveMsg.runId!)}>
              View self-improvement run →
            </button>
          )}
          <button className="export-x" onClick={() => setImproveMsg(null)} title="Dismiss">✕</button>
        </div>
      )}

      {cancelMsg && (
        <div className={`export-banner ${cancelMsg.kind}`}>
          <span className="export-text">{cancelMsg.text}</span>
          <button className="export-x" onClick={() => setCancelMsg(null)} title="Dismiss">✕</button>
        </div>
      )}

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
              {tab === "transcript" && sel && (
                <button className="btn-sm tabbar-action" disabled={!taskTerminal || exporting !== null}
                  onClick={() => runExport({ step: sel.step, task: sel.task })}
                  title={taskTerminal
                    ? "Download this task's LangSmith trace"
                    : "Available once the task completes"}>
                  {exporting === "task" ? "Exporting…" : "Export task"}
                </button>
              )}
            </div>
            {tab === "transcript"
              ? <Transcript runId={runId} sel={sel} status={manifest.status} taskStatus={selTask?.status}
                  arts={arts} onOpenArtifact={(a) => { setOpenArt(a); setTab("deliverables"); }} />
              : <Deliverables runId={runId} arts={arts} open={openArt} setOpen={setOpenArt} />}
          </section>
        </div>
      )}
    </div>
  );
}

function Transcript(
  { runId, sel, status, taskStatus, arts, onOpenArtifact }:
  { runId: string; sel: Sel | null; status: string; taskStatus?: string;
    arts: Artifact[]; onOpenArtifact: (a: Artifact) => void },
) {
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [pending, setPending] = useState(false);
  const { blocks, streaming, lastEventAt } = useTaskStream(runId, sel, taskStatus);
  const presented = useMemo(() => presentedSetFor(chat, arts, sel), [chat, arts, sel]);
  const plan = currentPlan(blocks, chat, streaming);
  const rail = (plan.length || presented.length) ? (
    <div className="transcript-rail">
      {plan.length > 0 && <PlanPanel todos={plan} />}
      {presented.length > 0 && <PresentedPanel runId={runId} files={presented} onOpen={onOpenArtifact} />}
    </div>
  ) : null;

  useEffect(() => {
    if (!sel) { setChat([]); return; }
    let live = true;
    setPending(true);
    api.messages(runId, sel.step, sel.task)
      .then((m) => { if (live) setChat(m); })
      .catch(() => { if (live) setChat([]); })
      .finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [runId, sel?.step, sel?.task, status, taskStatus, streaming]);

  if (!sel) return <div className="placeholder">Select a task to view its transcript.</div>;

  // Live stream takes over while the task runs and has produced something; after `done` flips
  // `streaming` false, keep the live blocks visible until the reconciled chat snapshot loads
  // (avoids a flash of "No messages yet…", and covers failed tasks that have no persisted chat).
  if (streaming || (blocks.length && !chat.length)) {
    return (
      <div className="transcript-split">
        <div className="transcript">
          {blocks.map((b, i) => {
            const isLast = i === blocks.length - 1;
            if (b.kind === "thinking")
              return <div key={i} className="msg thinking"><div className="msg-role">thinking</div>
                <div className="msg-text think">{b.text}{isLast && <span className="caret" />}</div></div>;
            if (b.kind === "text")
              return <div key={i} className="msg ai"><div className="msg-role">assistant</div>
                <div className="msg-text">{b.text}{isLast && <span className="caret" />}</div></div>;
            if (b.kind === "tool_call")
              return <div key={i} className="msg tool-calls">
                <div className={`toolcall${b.name === "present_files" ? " present" : ""}`}>
                  <span className="tc-name">→ {b.name}</span>
                  <span className="tc-args">{argSummary(b.args)}</span></div></div>;
            return <div key={i} className={`msg tool${b.isError ? " err" : ""}`}>
              <div className="msg-role">{b.name || "tool"}</div>
              <div className="msg-text">{b.text}</div></div>;
          })}
          <GeneratingIndicator streaming={streaming} lastEventAt={lastEventAt} />
        </div>
        {rail}
      </div>
    );
  }

  if (pending && !chat.length) return <div className="placeholder">Loading transcript…</div>;
  if (!chat.length) return <div className="placeholder">No messages yet for {sel.task}.</div>;

  return (
    <div className="transcript-split">
      <div className="transcript">
        {chat.map((m, i) => m.tool_calls?.length ? (
          <div key={i} className="msg tool-calls">
            {m.text && <div className="msg-text md"><Markdown>{m.text}</Markdown></div>}
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
            {m.role === "ai"
              ? <div className="msg-text md"><Markdown>{m.text}</Markdown></div>
              : <div className="msg-text">{m.text}</div>}
          </div>
        ))}
      </div>
      {rail}
    </div>
  );
}

const PLAN_GLYPH: Record<Todo["status"], string> = { completed: "✓", in_progress: "▸", pending: "○" };

// Pinned, glanceable view of the agent's current plan (latest write_todos). Read-only.
function PlanPanel({ todos }: { todos: Todo[] }) {
  const done = todos.filter((t) => t.status === "completed").length;
  return (
    <aside className="plan-panel">
      <div className="plan-head">
        <span className="plan-title">Plan</span>
        <span className="plan-count">{done}/{todos.length} done</span>
      </div>
      <ul className="plan-list">
        {todos.map((t, i) => (
          <li key={i} className={`plan-item ${t.status}`}>
            <span className="plan-glyph">{PLAN_GLYPH[t.status] ?? "○"}</span>
            <span className="plan-text">{t.content}</span>
          </li>
        ))}
      </ul>
    </aside>
  );
}

// Renders the files from a task's most-recent present_files call beside the transcript, each in its
// own column using the same rich renderer as Deliverables. Other (non-presented) files live in the
// Deliverables tab; a per-file button opens that file there full-size.
function PresentedPanel(
  { runId, files, onOpen }:
  { runId: string; files: Artifact[]; onOpen: (a: Artifact) => void },
) {
  return (
    <aside className="present-panel">
      <div className="present-panel-head">
        <span className="pp-title">Presented files</span>
        <span className="pp-count">{files.length}</span>
        <span className="pp-hint">Other files → Deliverables tab</span>
      </div>
      <div className="present-panel-body">
        {files.map((a) => (
          <div key={a.rel} className="pf-file">
            <div className="pf-file-head">
              <span className="pf-name" title={a.path}>{a.name}</span>
              <span className="pf-size">{fmtSize(a.size)}</span>
              <button className="pf-open" title="Open in Deliverables" onClick={() => onOpen(a)}>⤢</button>
            </div>
            <div className="pf-file-body">
              <ArtifactBody runId={runId} art={a} />
            </div>
          </div>
        ))}
      </div>
    </aside>
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
  const isPdf = PDF.test(art.name);
  const raw = artifactUrl(runId, art.rel);
  const tooBig = art.size > MAX_INLINE;
  const [text, setText] = useState<string | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    if (isImg || isPdf || tooBig) return;             // images/pdf stream via <img>/<iframe>; huge files aren't inlined
    let live = true;
    setText(null); setErr("");
    api.artifactText(runId, art.rel).then((t) => { if (live) setText(t); }).catch((e) => { if (live) setErr(String(e)); });
    return () => { live = false; };
  }, [runId, art.rel, isImg, isPdf, tooBig]);

  if (isImg) return <div className="art-img"><img src={raw} alt={art.name} /></div>;
  if (isPdf) return <iframe className="art-pdf" src={raw} title={art.name} />;
  if (tooBig) return <DownloadCard art={art} href={raw} note={`Large file (${fmtSize(art.size)}) — not shown inline.`} />;
  if (err) return <div className="error">{err}</div>;
  if (text === null) return <div className="placeholder">Loading…</div>;
  if (looksBinary(text)) return <DownloadCard art={art} href={raw} note="Binary file — download to view." />;
  if (isMd) return (
    <div className="art-md md">
      <Markdown>{text}</Markdown>
    </div>
  );
  return <CodeView name={art.name} text={text} href={raw} />;
}

// Its own component so toggling "Copied" re-renders only the button — never the (up to MAX_INLINE)
// highlighted code body beside it, which react-markdown would otherwise re-tokenize on every toggle.
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    }).catch(() => { /* clipboard unavailable */ });
  };
  return <button className="btn-sm" onClick={copy}>{copied ? "Copied ✓" : "Copy"}</button>;
}

// Whole-file source view: syntax-highlighted via the same rehype-highlight pipeline (language from
// the file extension), with copy + download affordances. Rendered through a guarded code fence so
// arbitrary file content can never break out into markdown.
function CodeView({ name, text, href }: { name: string; text: string; href: string }) {
  const lang = langFor(name);
  return (
    <div className="art-doc">
      <div className="art-doc-bar">
        <span className="art-lang">{lang === "plaintext" ? "text" : lang}</span>
        <span className="art-doc-spacer" />
        <CopyButton text={text} />
        <a className="btn-sm" href={href} download>Download</a>
      </div>
      <div className="art-code-body">
        <ReactMarkdown rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]}>
          {asFence(text, lang)}
        </ReactMarkdown>
      </div>
    </div>
  );
}

const STALL_MS = 20000; // > server heartbeat (15s) so normal quiet periods don't false-alarm

// Live "the agent is working" affordance shown while a task streams. Gap-aware: once no event has
// arrived for STALL_MS it says so, distinguishing a slow/hung model from normal streaming (a plain
// spinner can't). Truthful across reconnects — a reconnect fires `snapshot`, resetting lastEventAt.
function GeneratingIndicator({ streaming, lastEventAt }: { streaming: boolean; lastEventAt: number }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!streaming) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [streaming]);
  if (!streaming) return null;
  const gap = lastEventAt ? now - lastEventAt : 0;
  const stalled = gap >= STALL_MS;
  return (
    <div className={`generating${stalled ? " stalled" : ""}`} role="status" aria-live="polite">
      <span className="gen-dots"><span /><span /><span /></span>
      <span className="gen-label">
        {stalled ? `Still working — no updates for ${Math.round(gap / 1000)}s` : "Agent is working"}
      </span>
    </div>
  );
}

// Opens ONE EventSource for a running task and folds SSE events into an ordered block list.
// Closes on the `done` event, on task switch, and on unmount; the caller then refetches the
// authoritative persisted transcript. (There is deliberately no `error` listener — native
// EventSource reconnect owns the "error" event.)
function useTaskStream(runId: string, sel: Sel | null, taskStatus: string | undefined) {
  const [blocks, setBlocks] = useState<StreamBlock[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [lastEventAt, setLastEventAt] = useState(0);
  const esRef = useRef<EventSource | null>(null);
  const taskKeyRef = useRef<string | null>(null);

  useEffect(() => {
    esRef.current?.close();
    esRef.current = null;
    // Reset the live block list ONLY when switching to a DIFFERENT task — not when the same task
    // merely transitions running -> terminal. Wiping on the terminal transition would erase the
    // just-streamed transcript of a task that failed before (or without) persisting a chat, which
    // is exactly what left failed tasks showing "No messages yet".
    const taskKey = sel ? `${runId}::${sel.step}::${sel.task}` : null;
    if (taskKey !== taskKeyRef.current) {
      taskKeyRef.current = taskKey;
      setBlocks([]);
    }
    setStreaming(false);
    if (!sel || taskStatus !== "running") return;

    const es = new EventSource(api.streamUrl(runId, sel.step, sel.task));
    esRef.current = es;
    setStreaming(true);
    setLastEventAt(Date.now());

    const appendText = (kind: "thinking" | "text", text: string) =>
      setBlocks((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.kind === kind) {
          const next = prev.slice(0, -1);
          return [...next, { ...last, text: last.text + text }];
        }
        return [...prev, { kind, text } as StreamBlock];
      });

    es.addEventListener("snapshot", (e) => {
      setLastEventAt(Date.now());
      const { blocks: bs } = JSON.parse((e as MessageEvent).data);
      // Map accumulator events (typed by wire name) into render blocks.
      const mapped: StreamBlock[] = (bs || []).map((b: any) =>
        b.type === "thinking_delta" ? { kind: "thinking", text: b.text }
        : b.type === "text_delta" ? { kind: "text", text: b.text }
        : b.type === "tool_call" ? { kind: "tool_call", id: b.id, name: b.name, args: b.args }
        : { kind: "tool_result", name: b.name, text: b.text, isError: b.is_error });
      setBlocks(mapped);
    });
    es.addEventListener("thinking_delta", (e) => { setLastEventAt(Date.now()); appendText("thinking", JSON.parse((e as MessageEvent).data).text); });
    es.addEventListener("text_delta", (e) => { setLastEventAt(Date.now()); appendText("text", JSON.parse((e as MessageEvent).data).text); });
    es.addEventListener("tool_call", (e) => {
      setLastEventAt(Date.now());
      const d = JSON.parse((e as MessageEvent).data);
      setBlocks((prev) => [...prev, { kind: "tool_call", id: d.id, name: d.name, args: d.args }]);
    });
    es.addEventListener("tool_result", (e) => {
      setLastEventAt(Date.now());
      const d = JSON.parse((e as MessageEvent).data);
      setBlocks((prev) => [...prev, { kind: "tool_result", name: d.name, text: d.text, isError: d.is_error }]);
    });
    const end = () => { setStreaming(false); es.close(); if (esRef.current === es) esRef.current = null; };
    es.addEventListener("done", end);   // terminal frame (carries an `error` field on task failure)
    // Native EventSource "error" = transient connection drop; leave it to auto-reconnect. Final
    // teardown also happens when the task leaves "running" (the effect deps re-run and close es).

    return () => { es.close(); if (esRef.current === es) esRef.current = null; };
  }, [runId, sel?.step, sel?.task, taskStatus]);

  return { blocks, streaming, lastEventAt };
}

function DownloadCard({ art, href, note }: { art: Artifact; href: string; note: string }) {
  return (
    <div className="art-download">
      <div className="art-download-name">{art.name}</div>
      <div className="art-download-note">{note}</div>
      <a className="primary art-download-btn" href={href} download>Download ({fmtSize(art.size)})</a>
    </div>
  );
}
