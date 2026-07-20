# RunView: presented-files bottom tray + focused file modal

**Date:** 2026-07-20
**Status:** Design — awaiting review
**Scope:** Frontend only (`atom-ui`). Move presented files out of the right rail (where they share space with the Plan/TODOs and read as low-visibility) into a sticky tray pinned to the bottom of the transcript pane; clicking a file opens a focused, blurred-backdrop modal that renders the file with an internal scrollbar. No backend change.

## Problem

Presented deliverables render in the right rail (`.transcript-rail`) beside the Plan/TODO panel via `PresentedPanel`. That placement is cramped and ambiguous — the files compete with the todo checklist for a narrow column, so it is unclear they are the run's deliverables and they are easy to miss. Presented files should be an obvious, dedicated affordance, and viewing one should give it full focus.

## Key facts (from exploration)

- **Presented artifacts are immutable snapshots captured at task completion.** `present_files` (`src/atom/tools/present_files.py`) only records `{path, physical}` into the `artifacts` state channel (deduped by `path`, `src/atom/reducers.py::merge_artifacts`). At task end the engine calls `RunStore.capture_artifacts` (`src/atom/workflow/run_store.py:251`), which `shutil.copyfile`s each source into `runs/<id>/artifacts/s<step>__<task>/<name>` and builds an `ArtifactRef{name, path, rel, size}` (`engine.py:484`). The serve endpoint returns that frozen copy.
- **Consequences that shape the edge cases:**
  - `GET /api/runs/{id}/artifacts` returns **nothing while a task is running** — captures happen only at task completion. Presented files therefore appear in the UI only once their task is terminal.
  - A captured snapshot **never changes**. A later step re-editing the same file produces a *separate* snapshot with a different `rel`. So "the agent modifies the file while the user is viewing it" cannot corrupt an open view.
  - The snapshot **persists even if the agent deletes the source**; the only genuine "unavailable" path is a serve-time 404 (run dir cleaned / disk error) or the `rel` vanishing from the polled list.
- **Serving.** `GET /api/runs/{id}/artifacts/{rel}` → `FileResponse` of the snapshot, or 404 (`app.py:405`). `api.artifactText(id, rel)` fetches text; `artifactUrl(id, rel)` is the raw URL for `<img>`/`<iframe>`/download. `RunView` polls `api.artifacts(runId)` every 1.5s into `arts` (fresh object instances each poll).
- **Existing renderer is reusable.** `ArtifactBody` (`RunView.tsx:555`) already handles image / pdf / markdown / code (syntax-highlighted, guarded fence) / binary / too-big (`MAX_INLINE = 2_000_000`) and fetches by `rel`, re-fetching on `rel` change. The modal body reuses it verbatim.
- **No modal/overlay primitive exists.** Repo-wide grep for `modal|overlay|dialog|backdrop|portal|createPortal` in `atom-ui/src` returns nothing relevant. This is the first portal/overlay in the app. `react-dom` is present, so `createPortal` is available.
- **Styling** is a single global `atom-ui/src/styles.css`, BEM-ish flat class names + `:root` custom-property tokens, **light theme only** (no `prefers-color-scheme`). Existing relevant classes: `.transcript-split`, `.transcript`, `.transcript-rail`, `.present-panel`/`.pf-*` (to be removed), `.plan-panel`, `.btn-sm`, `.link`, `.error`.
- **No UI test runner.** `atom-ui/package.json` has only `dev` and `build` (`tsc && vite build`). Verification is typecheck + build + driving the real app.

## Non-goals

- **Live-during-run viewing.** Files appear after their task completes (chosen scope). No new "serve a running task's live file" endpoint.
- No redesign of the **Deliverables tab** or the **left-sidebar deliverables list**; both keep their current behavior (the tab keeps its own full-pane viewer — the modal is not unified with it).
- No backend change, no dark mode, no UI test framework.

## Design

### Which files the tray shows

For the selected task, **all of its captured artifacts**:

```ts
const presented = arts.filter((a) => sel && a.step === sel.step && a.task === sel.task);
```

Because captures come only from `present_files`, this is exactly that task's presented deliverables (deduped by path, in present order). This **replaces** `presentedSetFor` (the "last present_files call only" helper) and `PresentedPanel` — showing all of the task's presented files satisfies "any presented files… presented earlier." While a task is still running it has no captures, so `presented` is empty and the tray is absent — consistent with the after-completion timing.

### RunView.tsx — layout & state changes

1. **Right rail becomes Plan-only.** In `Transcript`, drop `PresentedPanel` from `rail`; `rail` renders only `PlanPanel` (when there are todos). Delete `PresentedPanel`, `presentedSetFor`, and the now-unused `onOpenArtifact` plumbing that pointed presented files at the Deliverables tab.
2. **Transcript column wraps messages + tray.** The scrolling `.transcript` and the new `<FilesTray>` sit in a flex column so the tray is a sticky footer of the pane while messages scroll above it:

```
<div className="transcript-split">
  <div className="transcript-main">        {/* new flex column */}
    <div className="transcript"> …messages (scrolls)… </div>
    <FilesTray files={presented} onOpen={setModalRel} />   {/* hidden when empty */}
  </div>
  {rail}                                    {/* PlanPanel only */}
</div>
```

3. **Modal open state lifts to `RunView` and is keyed by `rel`** (stable id), not the artifact object:

```ts
const [modalRel, setModalRel] = useState<string | null>(null);
// live artifact re-derived from the latest poll each render:
const modalArt = modalRel ? arts.find((a) => a.rel === modalRel) ?? null : null;
```

`setModalRel` is threaded down to `Transcript` → `FilesTray`. `<FileModal>` is rendered once at the `RunView` root (so its portal/backdrop covers the whole view). Close the modal (`setModalRel(null)`) whenever `runId` or the selected task changes — the tray belongs to the current task.

### FilesTray (new component)

A horizontal, scrollable strip pinned to the bottom of the transcript column. Header: "Presented files" + count. Each file is a chip: a type glyph (image / pdf / doc / code / generic, derived from the existing `IMG`/`MD`/`PDF` regexes + extension), the `name`, and `fmtSize(size)`. The whole chip is a `<button>` that calls `onOpen(a.rel)`. Returns `null` when `files` is empty.

```
├─ Presented files (3) ─────────────────────── sticky ──┤
│ [📄 report.md 12KB] [🖼 chart.png 88KB] [📄 notes.md]→│   ← overflow-x: auto
```

### FileModal (new component) — `createPortal` to `document.body`

Props: `{ runId, art: Artifact | null, onClose: () => void }`. Renders nothing when `art` is null.

- **Backdrop**: full-viewport fixed layer, semi-transparent scrim + `backdrop-filter: blur(...)` (with a `-webkit-` prefix). Clicking the backdrop calls `onClose`.
- **Dialog panel**: centered, `max-width`/`max-height` (e.g. `min(920px, 92vw)` × `88vh`), `role="dialog"` + `aria-modal="true"`, labelled by the filename. Header row: filename, virtual `path` (dimmed, truncated with `title`), `fmtSize`, a **Download** anchor (`artifactUrl`, `download`), and a **✕** close button. Body: `overflow-y: auto` wrapper around **`<ArtifactBody runId={runId} art={art} />`** so long files scroll inside the modal.
- **Unavailable state**: `FileModal` is only mounted when `modalArt` is non-null; if a poll drops the `rel` from `arts` while open, `modalArt` becomes `null` and the modal unmounts (closes). For an in-list-but-404 snapshot, `ArtifactBody` already surfaces `err` via `.error`; add an `onError` fallback on the `<img>`/`<iframe>` paths so a broken media snapshot shows a clear "This file could not be loaded" instead of a broken-image icon. This is the honest handling of the only real "stale/deleted" path.

### Accessibility & interaction

- **Esc** closes (keydown listener while open). Click-backdrop closes; clicks inside the panel do not (`stopPropagation`).
- On open, move focus into the dialog (focus the close button) and **restore focus** to the triggering chip on close.
- **Body scroll lock** while open (`document.body.style.overflow = "hidden"`, restored on close/unmount).
- The blur/scrim and all new classes use existing `:root` tokens; light-theme only, matching the app.

### Edge-case matrix

| Case | Handling |
|---|---|
| Agent modifies the source after presenting | Snapshot is frozen (immutable, per-task `rel`); open view is unaffected. No action needed. |
| `arts` re-polled (new object instances) | Modal keyed by `rel`, artifact re-derived each render — no stale reference. |
| Same file re-presented in a later step | Separate snapshot, different `rel`, different task tray. No collision. |
| Snapshot 404 (run cleaned / disk error) | `ArtifactBody` `.error` for text; `onError` fallback for `<img>`/`<iframe>`. |
| `rel` disappears from `arts` while modal open | `modalArt` → null → modal unmounts (closes). |
| User switches task / run while modal open | `setModalRel(null)` on `runId`/`sel` change. |
| Task still running (no captures yet) | `presented` empty → tray absent. |

## Testing / verification

No UI test runner exists (`atom-ui` has only `dev`/`build`). Verification:

1. `cd atom-ui && npx tsc --noEmit` and `npm run build` must pass.
2. Drive the real app (per the `run`/`verify` skills): submit a workflow run that presents at least one markdown/code file and one image; confirm (a) the tray appears at the bottom of the transcript once the task completes and is absent while running, (b) clicking a chip opens the modal with a blurred backdrop, (c) a long file scrolls inside the modal, (d) Esc / backdrop-click / ✕ all close and restore focus, (e) Download works, (f) the right rail now shows only the Plan panel.
3. Backend is untouched; existing `tests/test_workflow_api.py` artifact-serving tests remain the contract the UI relies on.

## Files touched

- `atom-ui/src/RunView.tsx` — remove `PresentedPanel`/`presentedSetFor`; add `FilesTray`, `FileModal`, `modalRel` state, `transcript-main` wrapper; `<img>`/`<iframe>` `onError` fallback in `ArtifactBody`.
- `atom-ui/src/styles.css` — new `.files-tray*`, `.file-modal*` (backdrop/dialog/header/body) classes; remove dead `.present-panel`/`.pf-*` rules and the `.transcript-rail .present-panel` / `.plan-panel:has(~ .present-panel)` rules; adjust `.transcript-split` for the `transcript-main` column.
- No other files.
