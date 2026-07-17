# Design: Markdown in AI messages + `present_files` side panel

Date: 2026-07-16

Two independent, **frontend-only** changes to `atom-ui`, both reusing the existing
markdown / `ArtifactBody` machinery. No API, schema, or backend changes — the data the UI
needs is already served.

1. Apply markdown rendering to **assistant** transcript messages (today markdown lives only
   in the Deliverables viewer).
2. Make `present_files` do something visible: render that call's presented files in a panel
   **beside the transcript**, reusing the same rich per-file rendering as Deliverables.

Touch list (both changes): `atom-ui/src/RunView.tsx`, `atom-ui/src/styles.css`.

---

## Change 1 — Markdown for assistant messages

### Problem

The markdown pipeline (`ReactMarkdown` + `remark-gfm` + `rehype-highlight` + `mdComponents`)
is used **only** inside `ArtifactBody` for `.md` deliverables (`RunView.tsx`). Assistant
messages in the transcript render as plain `white-space: pre-wrap` text (`.msg-text`), so
headings, lists, tables, links, and code blocks the model emits show as raw markdown source.

### Change

- Extract the `ReactMarkdown` invocation into a small reusable `<Markdown>` component in
  `RunView.tsx` (same plugins + `mdComponents` as `ArtifactBody` uses today). `ArtifactBody`'s
  `.md` branch is refactored to call it (no behavior change there).
- Render **assistant** message text through `<Markdown>` in the **reconciled** transcript path
  only, in two places:
  - the plain assistant branch (`m.role === "ai"`, no tool calls);
  - the leading text of an assistant message that also carries tool calls
    (`m.text` inside the `.tool-calls` block).
- **Live SSE stream stays plain text + caret.** Markdown appears only when the message
  finalizes — i.e. when the task reaches a terminal state and `Transcript` reconciles to the
  persisted `/messages` snapshot. This is a deliberate decision (no mid-stream markdown
  reflow); the live `text_delta` block keeps its current plain rendering.

### Scope (what is and isn't markdown)

| Message kind        | Rendering                          |
|---------------------|------------------------------------|
| assistant (`ai`)    | **markdown** (reconciled path)     |
| thinking            | plain italic (`.msg-text.think`) — unchanged |
| tool result (`tool`)| monospace (`.msg.tool .msg-text`) — unchanged |
| task / human        | plain `pre-wrap` — unchanged       |

Tool output is raw command text (e.g. bash stdout) and must stay monospace, not markdown.
Thinking is a raw reasoning stream and stays plain italic.

### CSS

Factor the **typographic** rules currently under `.art-md` (headings, `p`, `ul/ol/li`,
`pre`/`code`, `table`, `blockquote`, `a`, `img`, task-lists, `hr`) into a shared **`.md`**
class. `.art-md` keeps only **layout** (`flex`, `overflow`, `padding`, `max-width`, centering)
and additionally carries `.md`, so deliverables render identically. Assistant message bodies
use `.md` alone, with `:first-child`/`:last-child` margin resets and `white-space: normal` so
the bubble hugs its content. The shared `pre code` reset (currently `.art-md pre code,
.art-code-body pre code`) becomes `.md pre code, .art-code-body pre code`.

Sharing one typographic class (rather than duplicating rules) keeps assistant messages, the
Deliverables viewer, and the present-files panel visually consistent and prevents drift.

---

## Change 2 — `present_files` renders files in a side panel beside the transcript

### Problem

`present_files` records each presented file as an `ArtifactRef` (surfaced in Deliverables),
but in the transcript the tool call renders only as a compact `⇪ present_files <paths>` row —
the presented content is never shown inline. The user wants the presented files rendered
alongside the transcript, with other (non-presented) files reachable via the Deliverables tab.

### Data model (already present — no backend change)

- The `present_files` tool call carries `args.filepaths: string[]` in both the reconciled chat
  (`ChatMsg.tool_calls[].args`) and the live stream (`tool_call` block `args`).
- Each presented file is captured **once at task end** (`engine.py` → `capture_artifacts`) as an
  `ArtifactRef { name, path, rel, size }`, where **`path` == the virtual filepath the agent
  passed to `present_files`** (`run_store.capture_artifacts`), and `rel` is the fetch path.
- `RunView` already fetches the run's artifacts (`api.artifacts` → `arts: Artifact[]`, each with
  `step`, `task`).

**Join:** for the selected task, filter `arts` to `a.step === sel.step && a.task === sel.task`,
key by `path`, and resolve each `present_files` call's `filepaths` to artifacts. Because capture
dedupes by `path` and disambiguates basenames (`report.md` → `report-1.md`) while leaving `path`
intact, joining on `path` (not `name`) is exact. Unmatched filepaths (a missing file the tool
reported) are skipped.

### Layout

In the Transcript tab, `Transcript` renders a flex row (`.transcript-split`):

- **left:** the existing `.transcript` column (chat + live stream), unchanged internally;
- **right:** a `PresentedPanel` — shown **only** when the selected task has a `present_files`
  call whose files resolved to ≥1 artifact.

When there is no presented set, the transcript occupies the full width (no empty panel). The
panel and transcript scroll independently within the fixed-height center column.

### Which files the panel shows

The **most-recent** `present_files` call's resolved set. Derivation, in `Transcript` (which
holds the reconciled `chat`):

1. Walk `chat` for messages whose `tool_calls` include `name === "present_files"`.
2. Take the **last** such call; map its `args.filepaths` through the per-task `path → artifact`
   index; drop unmatched paths.
3. That artifact list is the panel's set.

Multi-call tasks: earlier calls' files remain available in the Deliverables tab (the panel
header states *"Other files → Deliverables tab"*). Click-to-switch between sets is **out of
scope** (YAGNI); default is most-recent only.

### Per-file rendering

Each file is a column reusing **`<ArtifactBody runId art>`** — the same component Deliverables
uses — so markdown / image / pdf / code / too-big / binary all render exactly as in the
viewer. Each column has a small header (filename + size + a link that opens the file in the
Deliverables tab). Columns lay out side-by-side via horizontal flex with a per-column
`min-width`; the row scrolls horizontally when files don't fit, and each column body scrolls
vertically so one large file can't dominate.

CSS: scope overrides under `.pf-file` to undo the full-width-viewer assumptions of `.art-md`
(`max-width`, centering, large padding) so file bodies fit the narrower columns.

### Timing

Artifacts are captured at **task end**, so while a task is streaming there is no resolved set
and the panel is absent — consistent with Change 1's "plain while streaming". Once the task
reaches a terminal state and the transcript reconciles (and `RunView`'s 1.5s artifact poll has
the new refs), the panel appears. The compact `⇪ present_files` row remains in the transcript
(live and reconciled) as the in-flow marker; the panel is its rendered companion.

---

## Components (after)

- `Markdown` — thin wrapper over `ReactMarkdown` (shared config). Used by `ArtifactBody` and
  assistant messages.
- `Transcript` — now receives `arts` and renders `.transcript-split` (transcript column +
  optional `PresentedPanel`); derives the presented set from `chat`.
- `PresentedPanel` — given `runId` + the resolved artifact set, renders the side-by-side
  `.pf-file` columns (each an `ArtifactBody`).
- `ArtifactBody`, `CodeView`, `DownloadCard`, `useTaskStream` — unchanged (ArtifactBody's `.md`
  branch delegates to `Markdown`).

## Edge cases

- **No `present_files` in task** → no panel; transcript full width.
- **Presented file missing / binary / too big** → `ArtifactBody`'s existing download/placeholder
  card handles it inside the column.
- **Narrow viewport** → panel has a `min-width`; the file row scrolls horizontally rather than
  crushing columns.
- **Task still running** → no captured artifacts yet → no panel (matches plain-while-streaming).
- **Assistant text that is not valid/complete markdown** → `react-markdown` renders it as
  paragraphs; the reconciled (finalized) text is always complete, so no partial-fence artifacts.

## Testing / verification

Frontend has no unit-test harness today; verification is by running the SPA (`npm run dev` /
built `dist`) against a completed run:

1. A run whose assistant messages contain markdown (headings, list, table, fenced code) →
   confirm they render formatted in the transcript, while thinking/tool-result/task turns are
   visually unchanged.
2. A run whose task called `present_files` → confirm the side panel appears with the presented
   files rendered (md/image/code), the transcript stays on the left, and other files are only in
   the Deliverables tab. Deliverables viewer still renders identically (CSS refactor regression
   check).
3. A task with no `present_files` → transcript is full width, no empty panel.
4. `tsc` build passes (`npm run build`).

## Out of scope (YAGNI)

- Click-to-switch between multiple `present_files` sets (default = most-recent).
- Resizable split divider.
- Markdown on thinking or tool-result messages.
- Live (mid-stream) markdown rendering.
