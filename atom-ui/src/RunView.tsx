import { useEffect, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { api, artifactUrl, Artifact, ChatMsg, Manifest } from "./api";
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
  html: "xml", htm: "xml", xml: "xml", svg: "xml", vue: "xml", css: "css", scss: "scss",
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
  const [exporting, setExporting] = useState<"run" | "task" | null>(null);
  const [exportMsg, setExportMsg] = useState<{ text: string; kind: "ok" | "warn" | "err" } | null>(null);

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      const m = await api.run(runId).catch(() => null);
      if (live && m) {
        setManifest(m);
        api.artifacts(runId).then((a) => { if (live) setArts(a); }).catch(() => { /* ignore */ });
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
        setExportMsg({ text: `Exported ${what} → ${res.path}${partial}`, kind: res.complete ? "ok" : "warn" });
      }
    } catch (e) {
      setExportMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setExporting(null);
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
            <StatusPill status={manifest.status} />
            <span className="dim">Step {curStep} of {manifest.steps.length}</span>
            <span className="dim">{elapsed(manifest.created_at, manifest.ended_at)}</span>
            <button className="btn-sm" disabled={manifest.status !== "complete" || exporting !== null}
              onClick={() => runExport()}
              title={manifest.status === "complete"
                ? "Download this run's LangSmith traces"
                : "Available once all steps complete"}>
              {exporting === "run" ? "Exporting…" : "Export run"}
            </button>
          </div>
        )}
      </div>

      {exportMsg && (
        <div className={`export-banner ${exportMsg.kind}`}>
          <span className="export-text">{exportMsg.text}</span>
          <button className="export-x" onClick={() => setExportMsg(null)} title="Dismiss">✕</button>
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
    <div className="art-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[[rehypeHighlight, { detect: true, ignoreMissing: true }]]}
        components={mdComponents}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
  return <CodeView name={art.name} text={text} href={raw} />;
}

// Whole-file source view: syntax-highlighted via the same rehype-highlight pipeline (language from
// the file extension), with copy + download affordances. Rendered through a guarded code fence so
// arbitrary file content can never break out into markdown.
function CodeView({ name, text, href }: { name: string; text: string; href: string }) {
  const lang = langFor(name);
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(text).then(() => setCopied(true)).catch(() => { /* clipboard unavailable */ });
  };
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1400);
    return () => clearTimeout(t);
  }, [copied]);
  return (
    <div className="art-doc">
      <div className="art-doc-bar">
        <span className="art-lang">{lang === "plaintext" ? "text" : lang}</span>
        <span className="art-doc-spacer" />
        <button className="btn-sm" onClick={copy}>{copied ? "Copied ✓" : "Copy"}</button>
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

function DownloadCard({ art, href, note }: { art: Artifact; href: string; note: string }) {
  return (
    <div className="art-download">
      <div className="art-download-name">{art.name}</div>
      <div className="art-download-note">{note}</div>
      <a className="primary art-download-btn" href={href} download>Download ({fmtSize(art.size)})</a>
    </div>
  );
}
