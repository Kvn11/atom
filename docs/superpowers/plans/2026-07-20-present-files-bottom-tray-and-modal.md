# Present-Files Bottom Tray + Focused File Modal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move presented deliverables out of the cramped right rail into a sticky tray at the bottom of the transcript pane, and open a clicked file in a focused, blurred-backdrop modal that renders it with an internal scrollbar.

**Architecture:** Frontend-only change to two files (`atom-ui/src/RunView.tsx`, `atom-ui/src/styles.css`). Reuses the existing immutable per-task artifact snapshots (served by `rel`) and the existing `ArtifactBody` renderer. Task 1 relocates presented files to a bottom `FilesTray` and shrinks the right rail to Plan-only. Task 2 adds a `createPortal`-based `FileModal` and repoints the tray at it, plus a media `onError` fallback for the only real "stale/deleted" path (a serve-time 404).

**Tech Stack:** React 18 + TypeScript, Vite, `react-dom` `createPortal`, `react-markdown`/`rehype-highlight` (already used by `ArtifactBody`). Plain global CSS with `:root` custom-property tokens.

## Global Constraints

- **No backend change.** The `/api/runs/{id}/artifacts` list and `/api/runs/{id}/artifacts/{rel}` serve endpoints are used as-is.
- **No UI test runner exists.** `atom-ui/package.json` has only `dev` and `build` (`tsc && vite build`). Every task is verified by `npx tsc --noEmit` + `npm run build` + a concrete manual check against the running app. Do NOT add a test framework (YAGNI).
- **Light theme only.** No `prefers-color-scheme` / dark-mode variants. Style with existing `:root` tokens: `--bg --surface --surface-2 --surface-3 --border --border-strong --ink --ink-2 --ink-3 --accent --accent-weak --ok --warn --err --radius --radius-sm --shadow --mono`.
- **Styling convention:** hand-written rules in the single global `atom-ui/src/styles.css`, BEM-ish flat class names applied via `className`. No CSS-in-JS, no Tailwind, no inline styles beyond the one-off `document.body.style.overflow` scroll-lock.
- **Presented set = the selected task's captured artifacts.** `arts.filter(a => a.step === sel.step && a.task === sel.task)`. Captures come only from `present_files`, so this is exactly that task's presented deliverables; it is empty while the task is still running.
- **Keep out of scope:** the Deliverables tab, the left-sidebar deliverables list, live-during-run viewing.

**Working directory for all commands:** `atom-ui/` (e.g. `cd atom-ui && npx tsc --noEmit`).

---

## File Structure

- `atom-ui/src/RunView.tsx` — remove `presentedSetFor` + `PresentedPanel`; add `fileGlyph` + `FilesTray` (Task 1) and `FileModal` + modal state (Task 2); add `mediaErr` handling to `ArtifactBody` (Task 2).
- `atom-ui/src/styles.css` — remove `.present-panel*` / `.pf-*` / `.plan-panel:has(~ .present-panel)`; add `.transcript-main`, `.files-tray*`, `.ft-*` (Task 1) and `.file-modal*` (Task 2).

No other files change.

---

## Task 1: Relocate presented files to a bottom tray; right rail → Plan-only

**Files:**
- Modify: `atom-ui/src/RunView.tsx` (remove `presentedSetFor` ~78-92 and `PresentedPanel` ~493-523; edit `Transcript` ~379-468)
- Modify: `atom-ui/src/styles.css` (remove `.present-panel*`/`.pf-*`/`:has` rules ~199-239; add tray rules)
- Test: none (no runner) — verified by typecheck + build + manual check

**Interfaces:**
- Consumes: `Artifact` (`{ name, path, rel, size, step, task }`), `fmtSize`, the `IMG`/`MD`/`PDF` regexes already defined at the top of `RunView.tsx`, and the existing `onOpenArtifact: (a: Artifact) => void` prop already passed into `Transcript` (opens the Deliverables tab — used as the tray's temporary click target this task; repointed to the modal in Task 2).
- Produces: `FilesTray({ files: Artifact[]; onOpen: (a: Artifact) => void })` and `fileGlyph(name: string): string`, plus the `.transcript-main` layout wrapper. The `onOpen` signature `(a: Artifact) => void` is stable across Task 2 (only the callsite target changes).

- [ ] **Step 1: Add the `fileGlyph` helper and `FilesTray` component**

In `RunView.tsx`, add these two above the `Deliverables` function (near the other transcript components). `IMG`, `MD`, `PDF`, `Artifact`, and `fmtSize` are already in scope at module top.

```tsx
// Emoji glyph for a presented file, by extension family (purely decorative; aria-hidden).
function fileGlyph(name: string): string {
  if (IMG.test(name)) return "🖼";
  if (PDF.test(name)) return "📕";
  if (MD.test(name)) return "📝";
  return "📄";
}

// Sticky strip of the selected task's presented deliverables, pinned to the bottom of the
// transcript pane. Renders nothing when the task has presented nothing (e.g. still running).
// Each chip opens the file via `onOpen`.
function FilesTray({ files, onOpen }: { files: Artifact[]; onOpen: (a: Artifact) => void }) {
  if (!files.length) return null;
  return (
    <div className="files-tray">
      <div className="files-tray-head">
        <span className="ft-title">Presented files</span>
        <span className="ft-count">{files.length}</span>
      </div>
      <div className="files-tray-strip">
        {files.map((a) => (
          <button key={a.rel} className="ft-chip" onClick={() => onOpen(a)} title={a.path}>
            <span className="ft-glyph" aria-hidden="true">{fileGlyph(a.name)}</span>
            <span className="ft-name">{a.name}</span>
            <span className="ft-size">{fmtSize(a.size)}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Replace the presented-set computation and rail in `Transcript`**

In `Transcript` (currently ~386-394), replace:

```tsx
  const presented = useMemo(() => presentedSetFor(chat, arts, sel), [chat, arts, sel]);
  const plan = currentPlan(blocks, chat, streaming);
  const rail = (plan.length || presented.length) ? (
    <div className="transcript-rail">
      {plan.length > 0 && <PlanPanel todos={plan} />}
      {presented.length > 0 && <PresentedPanel runId={runId} files={presented} onOpen={onOpenArtifact} />}
    </div>
  ) : null;
```

with:

```tsx
  const presented = useMemo(
    () => arts.filter((a) => !!sel && a.step === sel.step && a.task === sel.task),
    [arts, sel],
  );
  const plan = currentPlan(blocks, chat, streaming);
  const rail = plan.length ? (
    <div className="transcript-rail">
      <PlanPanel todos={plan} />
    </div>
  ) : null;
```

- [ ] **Step 3: Wrap both transcript render branches in `.transcript-main` and mount the tray**

In `Transcript`, the **streaming branch** (currently `return (<div className="transcript-split"> <div className="transcript"> …blocks… <GeneratingIndicator/> </div> {rail} </div>)`) becomes:

```tsx
    return (
      <div className="transcript-split">
        <div className="transcript-main">
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
          <FilesTray files={presented} onOpen={onOpenArtifact} />
        </div>
        {rail}
      </div>
    );
```

And the **reconciled (final) branch** (currently `return (<div className="transcript-split"> <div className="transcript"> {chat.map(...)} </div> {rail} </div>)`) becomes:

```tsx
  return (
    <div className="transcript-split">
      <div className="transcript-main">
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
        <FilesTray files={presented} onOpen={onOpenArtifact} />
      </div>
      {rail}
    </div>
  );
```

(Only the wrapping `<div className="transcript-main">…<FilesTray/></div>` is added around the existing `.transcript` in each branch; the inner content is unchanged.)

- [ ] **Step 4: Delete the now-dead `presentedSetFor` and `PresentedPanel`**

Remove the `presentedSetFor` function (and its leading comment block, ~76-92) and the entire `PresentedPanel` function (and its leading comment block, ~493-523). Confirm no other references remain:

Run: `cd atom-ui && grep -n "presentedSetFor\|PresentedPanel" src/RunView.tsx`
Expected: no output.

- [ ] **Step 5: Swap the presented-panel CSS for tray CSS in `styles.css`**

Delete these now-dead rules: `.present-panel`, `.transcript-rail .present-panel`, `.plan-panel:has(~ .present-panel)`, `.present-panel-head`, `.pp-title`, `.pp-count`, `.pp-hint`, `.present-panel-body`, `.pf-file`, `.pf-file:last-child`, `.pf-file-head`, `.pf-name`, `.pf-size`, `.pf-open`, `.pf-open:hover`, `.pf-file-body`, `.pf-file .art-md`, `.pf-file .art-img` (the block ~199-239, keeping `.transcript-split`, `.transcript-rail`, and all `.plan-*` rules). Update the `.transcript-split` comment to drop the "Presented-files side panel" wording.

Then append the tray rules (near the transcript styles):

```css
/* Presented-files tray — sticky footer of the transcript pane; chips open the file modal. */
.transcript-main { flex: 1; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
.files-tray { flex: 0 0 auto; border-top: 1px solid var(--border); background: var(--surface); }
.files-tray-head { display: flex; align-items: baseline; gap: 8px; padding: 8px 16px 2px; }
.ft-title { font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--ink-3); font-weight: 600; }
.ft-count { font-size: 11px; color: #fff; background: var(--accent); border-radius: 999px; padding: 1px 7px; font-weight: 650; }
.files-tray-strip { display: flex; gap: 8px; overflow-x: auto; padding: 6px 16px 12px; }
.ft-chip { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 8px; max-width: 280px;
  border: 1px solid var(--border-strong); background: var(--surface); border-radius: 999px;
  padding: 6px 12px; color: var(--ink); cursor: pointer; }
.ft-chip:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-weak); }
.ft-glyph { font-size: 14px; line-height: 1; flex: 0 0 auto; }
.ft-name { min-width: 0; font-family: var(--mono); font-size: 12.5px; font-weight: 550;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ft-size { flex: 0 0 auto; font-size: 11.5px; color: var(--ink-3); white-space: nowrap; }
.ft-chip:hover .ft-size { color: var(--accent); }
```

- [ ] **Step 6: Typecheck and build**

Run: `cd atom-ui && npx tsc --noEmit`
Expected: exits 0, no errors (in particular no "unused `onOpenArtifact`" — it is still used by the tray this task).

Run: `cd atom-ui && npm run build`
Expected: Vite reports "✓ built in …" with no errors.

- [ ] **Step 7: Manual verification against the running app**

Start the app (per the project's `run` skill / README; typically `python -m atom.api` + `cd atom-ui && npm run dev`, or the built SPA). Open a completed run whose task called `present_files`.

Confirm:
- The right rail shows **only the Plan panel** (no presented-files column); if the task has no todos, there is no rail.
- A **"Presented files" tray** sits pinned at the bottom of the transcript pane, listing every file the task presented (chips: glyph · name · size), with horizontal scroll if they overflow.
- Scrolling the transcript keeps the tray visible.
- Clicking a chip opens the file (this task: in the Deliverables tab — the modal arrives in Task 2).
- Open a run whose task is **still running**: the tray is absent.

- [ ] **Step 8: Commit**

```bash
git add atom-ui/src/RunView.tsx atom-ui/src/styles.css
git commit -m "feat(ui): presented files as a bottom-of-transcript tray; rail → Plan-only

Replace the right-rail PresentedPanel (shared with the Plan/TODO panel, low
visibility) with a sticky FilesTray at the foot of the transcript pane showing
all of the selected task's captured artifacts. Right rail is now Plan-only.
Chips temporarily open the Deliverables tab; the focused modal follows.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Focused file modal + repoint the tray + media 404 fallback

**Files:**
- Modify: `atom-ui/src/RunView.tsx` (imports; `RunView` state + `<FileModal>` mount; `Transcript` props; new `FileModal`; `ArtifactBody` media error handling)
- Modify: `atom-ui/src/styles.css` (add `.file-modal*` rules)
- Test: none (no runner) — verified by typecheck + build + manual check

**Interfaces:**
- Consumes from Task 1: `FilesTray({ files, onOpen: (a: Artifact) => void })`, the `.transcript-main` layout, `Artifact`, `artifactUrl`, `fmtSize`, `ArtifactBody`.
- Produces: `FileModal({ runId: string; art: Artifact | null; onClose: () => void })` rendered via `createPortal` at the `RunView` root; `modalRel: string | null` state with `closeModal: () => void` and `modalArt = arts.find(a => a.rel === modalRel) ?? null`; a new `onOpenModal: (a: Artifact) => void` prop on `Transcript` that replaces `onOpenArtifact` as the tray's target.

- [ ] **Step 1: Add imports**

At the top of `RunView.tsx`, add `useCallback` to the React import and import `createPortal`:

```tsx
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
```

- [ ] **Step 2: Add modal state and derive the live artifact in `RunView`**

In the `RunView` component body, alongside the other `useState` hooks (near `openArt`), add:

```tsx
  const [modalRel, setModalRel] = useState<string | null>(null);
  const closeModal = useCallback(() => setModalRel(null), []);
  // Re-derive the live artifact from the latest poll by stable `rel`. If the file drops out of the
  // list (run cleaned up), this becomes null and <FileModal> closes itself.
  const modalArt = useMemo(
    () => (modalRel ? arts.find((a) => a.rel === modalRel) ?? null : null),
    [modalRel, arts],
  );
```

Then, so the modal never outlives the task/run it belongs to, add this effect near the other `useEffect`s:

```tsx
  // The tray belongs to the selected task — close any open file when the task or run changes.
  useEffect(() => { setModalRel(null); }, [runId, sel?.step, sel?.task]);
```

- [ ] **Step 3: Thread `onOpenModal` into `Transcript` and repoint the tray**

In `RunView`'s render, the transcript branch currently calls:

```tsx
              ? <Transcript runId={runId} sel={sel} status={manifest.status} taskStatus={selTask?.status}
                  arts={arts} onOpenArtifact={(a) => { setOpenArt(a); setTab("deliverables"); }} />
```

Replace the `onOpenArtifact` prop with `onOpenModal`:

```tsx
              ? <Transcript runId={runId} sel={sel} status={manifest.status} taskStatus={selTask?.status}
                  arts={arts} onOpenModal={(a) => setModalRel(a.rel)} />
```

In the `Transcript` function signature, rename the prop and its type:

```tsx
function Transcript(
  { runId, sel, status, taskStatus, arts, onOpenModal }:
  { runId: string; sel: Sel | null; status: string; taskStatus?: string;
    arts: Artifact[]; onOpenModal: (a: Artifact) => void },
) {
```

And in both render branches, change the tray callsite `onOpen={onOpenArtifact}` → `onOpen={onOpenModal}` (two occurrences).

- [ ] **Step 4: Mount `<FileModal>` at the `RunView` root**

As the last child of the top-level `<div className="runview">` in `RunView` (just before its closing `</div>`, after the `{!manifest ? … : (…)}` block), add:

```tsx
      <FileModal runId={runId} art={modalArt} onClose={closeModal} />
```

- [ ] **Step 5: Add the `FileModal` component**

Add near the `Deliverables`/`FilesTray` components in `RunView.tsx`:

```tsx
// Focused overlay for one presented file, portalled to <body> so its blurred backdrop covers the
// whole view. Renders nothing when `art` is null (closed, or the file dropped out of the run).
// Body mirrors the Deliverables viewer's flex column so ArtifactBody's `flex:1` children (pdf/code)
// fill and scroll internally. Esc / backdrop-click / ✕ close; focus is trapped-in on open and
// restored on close; body scroll is locked while open.
function FileModal(
  { runId, art, onClose }: { runId: string; art: Artifact | null; onClose: () => void },
) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const rel = art?.rel ?? null;   // keyed on rel so the 1.5s poll (new object, same file) is inert
  useEffect(() => {
    if (!rel) return;
    const prevFocus = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      prevFocus?.focus?.();          // best-effort: return focus to the triggering chip
    };
  }, [rel, onClose]);

  if (!art) return null;
  const raw = artifactUrl(runId, art.rel);
  return createPortal(
    <div className="file-modal-backdrop" onClick={onClose}>
      <div className="file-modal" role="dialog" aria-modal="true" aria-label={art.name}
        onClick={(e) => e.stopPropagation()}>
        <div className="file-modal-head">
          <span className="file-modal-name">{art.name}</span>
          <span className="file-modal-path dim" title={art.path}>{art.path}</span>
          <span className="file-modal-size dim">{fmtSize(art.size)}</span>
          <a className="btn-sm" href={raw} download>Download</a>
          <button ref={closeRef} className="file-modal-x" onClick={onClose}
            title="Close (Esc)" aria-label="Close">✕</button>
        </div>
        <div className="file-modal-body">
          <ArtifactBody runId={runId} art={art} />
        </div>
      </div>
    </div>,
    document.body,
  );
}
```

- [ ] **Step 6: Add a media-error fallback to `ArtifactBody`**

In `ArtifactBody`, add a `mediaErr` state, reset it inside the existing effect, and use it on the image/pdf paths so a 404/broken snapshot shows a clear message instead of a broken-image icon.

Add the state (with the existing `text`/`err` state):

```tsx
  const [mediaErr, setMediaErr] = useState(false);
```

In the existing effect, add `setMediaErr(false);` as its first line (before the early return), so switching files clears a prior error:

```tsx
  useEffect(() => {
    setMediaErr(false);
    if (isImg || isPdf || tooBig) return;             // images/pdf stream via <img>/<iframe>; huge files aren't inlined
    let live = true;
    setText(null); setErr("");
    api.artifactText(runId, art.rel).then((t) => { if (live) setText(t); }).catch((e) => { if (live) setErr(String(e)); });
    return () => { live = false; };
  }, [runId, art.rel, isImg, isPdf, tooBig]);
```

Then, just before the `if (isImg)` / `if (isPdf)` returns, add the fallback, and wire `onError` on both media elements:

```tsx
  if (mediaErr) return <div className="error">This file could not be loaded — it may have been moved or deleted.</div>;
  if (isImg) return <div className="art-img"><img src={raw} alt={art.name} onError={() => setMediaErr(true)} /></div>;
  if (isPdf) return <iframe className="art-pdf" src={raw} title={art.name} onError={() => setMediaErr(true)} />;
```

(The remaining `ArtifactBody` returns — `tooBig`, `err`, loading, binary, markdown, code — are unchanged. `<iframe onError>` is unreliable across browsers, but harmless; the header's Download link remains the fallback for a broken PDF.)

- [ ] **Step 7: Add the modal CSS to `styles.css`**

Append (near the Deliverables/`.viewer` rules):

```css
/* Focused file modal — portalled to <body>; blurred, dimmed backdrop; body mirrors .viewer's
   flex column so ArtifactBody's flex:1 children (pdf/code/md/img) fill and scroll internally. */
.file-modal-backdrop {
  position: fixed; inset: 0; z-index: 100;
  display: flex; align-items: center; justify-content: center; padding: 4vh 4vw;
  background: rgba(28, 25, 23, .38);
  backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
}
.file-modal {
  display: flex; flex-direction: column; overflow: hidden;
  width: min(920px, 92vw); max-height: 88vh;
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: 0 12px 40px rgba(28, 25, 23, .28);
}
.file-modal-head { display: flex; align-items: center; gap: 12px; padding: 11px 16px; border-bottom: 1px solid var(--border); }
.file-modal-name { font-weight: 600; font-size: 14px; white-space: nowrap; }
.file-modal-path { flex: 1; min-width: 0; font-family: var(--mono); font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-modal-size { flex: 0 0 auto; font-size: 12.5px; }
.file-modal-head a.btn-sm { text-decoration: none; display: inline-flex; align-items: center; }
.file-modal-x { border: 0; background: transparent; color: var(--ink-2);
  font-size: 15px; line-height: 1; padding: 4px 8px; border-radius: var(--radius-sm); cursor: pointer; }
.file-modal-x:hover { background: var(--surface-2); color: var(--ink); }
.file-modal-body { flex: 1; min-height: 0; display: flex; flex-direction: column; }
@media (prefers-reduced-motion: no-preference) {
  .file-modal { animation: file-modal-in .12s ease-out; }
  @keyframes file-modal-in { from { transform: translateY(6px); opacity: .6; } to { transform: none; opacity: 1; } }
}
```

- [ ] **Step 8: Typecheck and build**

Run: `cd atom-ui && npx tsc --noEmit`
Expected: exits 0, no errors. In particular, no "unused `onOpenArtifact`" — the prop was renamed to `onOpenModal` and the old Deliverables-tab closure removed from the `Transcript` callsite.

Run: `cd atom-ui && npm run build`
Expected: Vite reports "✓ built in …" with no errors.

- [ ] **Step 9: Manual verification against the running app**

Open a completed run whose task presented (a) a markdown or code file and (b) an image; ideally also a long file and a PDF.

Confirm:
- Clicking a tray chip opens a **centered modal** over a **blurred, dimmed** backdrop; the rest of the view is de-emphasized.
- A **long file scrolls inside the modal** (markdown/code scroll internally; a PDF fills the modal body; an image fits with `max-width`).
- **Esc**, **clicking the backdrop**, and the **✕** all close the modal; after closing, keyboard focus is back on (or near) the triggering chip.
- The header **Download** link downloads the file.
- With the modal open, **switch to another task** in the left rail → the modal closes.
- 404 path: temporarily point a chip at a bogus `rel` (or delete the snapshot file under `runs/<id>/artifacts/…` and reopen) → the modal shows "This file could not be loaded…" (image) or the error text (text file), not a hung spinner or broken-image icon.

- [ ] **Step 10: Commit**

```bash
git add atom-ui/src/RunView.tsx atom-ui/src/styles.css
git commit -m "feat(ui): focused file modal for presented deliverables

Click a tray chip to open the file in a createPortal modal with a blurred,
dimmed backdrop and internal scroll (body mirrors the deliverables viewer's
flex column so pdf/code/md/img all render correctly). Esc/backdrop/✕ close with
focus restore and body scroll-lock; modal is keyed by stable rel so the 1.5s
artifacts poll is inert, and it closes itself if the file drops out of the run
or the task/run changes. ArtifactBody gains a media onError fallback for a
serve-time 404 (the only real stale/deleted path).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- Move presented files out of the right rail → Task 1 (Steps 2, 4–5). ✅
- Presented files at the bottom of the transcript → Task 1 (Steps 1, 3, 5: `FilesTray` + `.transcript-main`). ✅
- Click opens a modal of the file → Task 2 (Steps 3–5). ✅
- Scrollbar for lengthy files → Task 2 (Step 7: `.file-modal-body` flex column + existing `.art-*` internal `overflow:auto`). ✅
- Blurred backdrop → Task 2 (Step 7: `backdrop-filter: blur`). ✅
- "Modified while viewing" → immutable snapshot + `rel`-keyed effect (Task 2 Steps 2, 5). ✅
- "Stale/deleted files presented earlier" → `modalArt` null-out auto-close (Task 2 Step 2) + media `onError` 404 fallback (Task 2 Step 6). ✅
- Right rail → Plan-only → Task 1 (Step 2). ✅
- Present set = all of the task's captured artifacts → Task 1 (Step 2). ✅
- Task/run switch closes modal → Task 2 (Step 2 effect). ✅
- No backend change / light theme / no test runner → Global Constraints; verification is tsc + build + manual. ✅
- Deliverables tab + left sidebar untouched → not modified by any task. ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every verification step has an exact command + expected output or a concrete manual checklist. ✅

**Type consistency:** `FilesTray.onOpen: (a: Artifact) => void` is stable across both tasks (Task 1 passes `onOpenArtifact`, Task 2 passes `onOpenModal`, both `(a: Artifact) => void`). `FileModal` props `{ runId: string; art: Artifact | null; onClose: () => void }` match the `<FileModal runId={runId} art={modalArt} onClose={closeModal} />` mount. `modalRel: string | null` ↔ `setModalRel(a.rel)` (string) and `arts.find(a => a.rel === modalRel)`. `closeModal: () => void` ↔ `FileModal.onClose`. `Transcript`'s prop rename `onOpenArtifact` → `onOpenModal` is applied at the signature, both tray callsites, and the `RunView` callsite. ✅
