# RunView: pinned Plan panel + "agent is working" indicator

**Date:** 2026-07-18
**Status:** Design — awaiting review
**Scope:** Frontend only (`atom-ui`). Two additions to the task Transcript: a pinned Plan panel that surfaces the agent's todo list as structured plan state, and a live generating indicator so the user has continuous evidence the agent hasn't hung or crashed. No backend change.

## Problem

1. **Todos are invisible as plan state.** The agent's `todos` are only ever shown in the UI as a generic `→ write_todos {…}` tool-call line (RunView `Transcript`), never as a readable checklist. (Backend review gap #5.)
2. **No liveness indicator.** While a task streams, the only motion is a caret on the last text/thinking block. During gaps — waiting for the model's first token, between a tool result and the next model call, or a long-running tool — the user sees nothing new and cannot tell "working" from "hung/crashed."

## Key facts (from exploration)

- **Todo data already reaches the UI.** `write_todos` streams as a `tool_call` event with `args = {todos: [{content, status}, …]}` (`src/atom/streaming.py::translate_update`), is captured in the RunEventBus accumulator (so it is in the `snapshot` frame), and is present in persisted messages' `tool_calls` (`ChatMsg.tool_calls`). `write_todos` replaces the whole list each call, so the **latest** call is the current plan. No backend change is required.
- **Streaming liveness** is tracked by `useTaskStream`'s `streaming` boolean (EventSource open + task `running`, cleared on the `done` event). Heartbeats are sent as SSE **comments** (`: ping`), which the browser `EventSource` does **not** surface as JS events — so real events (`snapshot`/`thinking_delta`/`text_delta`/`tool_call`/`tool_result`) are the only client-visible liveness signal.
- **No UI test runner.** `atom-ui/package.json` has only `dev` and `build` (`tsc && vite build`); no vitest/jest/testing-library, no `*.test.ts`. Verification is typecheck + build + a visual smoke check.
- **Styles** live in `atom-ui/src/styles.css` (existing `.transcript-split`, `.present-panel`, `.caret`, CSS vars like `--border`).

## Non-goals

- No dedicated backend `todos` SSE event (data already flows via `write_todos`).
- No adding a UI test framework (YAGNI; none exists).
- No editing/interacting with todos from the UI — read-only display.
- No change to the chat/lead per-turn reset behavior (that middleware is separate; workflow tasks are single-turn, so the latest `write_todos` is always the current plan there).

## Design

### api.ts

Add a shared todo type and keep the existing `StreamBlock`/`ChatMsg` shapes:

```ts
export type TodoStatus = "pending" | "in_progress" | "completed";
export interface Todo { content: string; status: TodoStatus; }
```

`write_todos` `args` are untyped (`Record<string, unknown>`); the extractor below narrows `args.todos` to `Todo[]` defensively.

### RunView.tsx — `currentPlan` (pure helper)

Returns the current plan (or `[]`) from whichever source is live. Reverse-scan so the latest `write_todos` wins:

```ts
function todosFromArgs(args: unknown): Todo[] | null {
  const t = (args as { todos?: unknown } | undefined)?.todos;
  if (!Array.isArray(t)) return null;
  const items = t.filter(
    (x): x is Todo =>
      !!x && typeof (x as Todo).content === "string" && typeof (x as Todo).status === "string",
  );
  return items.length ? items : null;
}

function currentPlan(blocks: StreamBlock[], chat: ChatMsg[], streaming: boolean): Todo[] {
  if (streaming || (blocks.length && !chat.length)) {
    for (let i = blocks.length - 1; i >= 0; i--) {
      const b = blocks[i];
      if (b.kind === "tool_call" && b.name === "write_todos") {
        const todos = todosFromArgs(b.args);
        if (todos) return todos;
      }
    }
    return [];
  }
  for (let i = chat.length - 1; i >= 0; i--) {
    const call = chat[i].tool_calls?.find((c) => c.name === "write_todos");
    if (call) {
      const todos = todosFromArgs(call.args);
      if (todos) return todos;
    }
  }
  return [];
}
```

The `streaming || (blocks.length && !chat.length)` guard mirrors the Transcript's own branch selection so the panel reads from the same source the transcript is currently rendering.

### RunView.tsx — `PlanPanel` component

```tsx
const PLAN_GLYPH: Record<TodoStatus, string> = { completed: "✓", in_progress: "▸", pending: "○" };

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
```

### RunView.tsx — Transcript rail refactor

`Transcript` currently has two return branches (live blocks vs. persisted chat). Refactor so both wrap their content in a flex row that can carry a right rail. Compute the plan once:

```tsx
const plan = currentPlan(blocks, chat, streaming);
const rail = (plan.length || presented.length) ? (
  <div className="transcript-rail">
    {plan.length > 0 && <PlanPanel todos={plan} />}
    {presented.length > 0 && <PresentedPanel runId={runId} files={presented} onOpen={onOpenArtifact} />}
  </div>
) : null;
```

- **Live branch** (now gated on `streaming || (blocks.length && !chat.length)`, so it also renders when streaming has started but produced no block yet): render the `.transcript` block list **plus** `<GeneratingIndicator streaming={streaming} lastEventAt={lastEventAt} />` at the bottom, wrapped alongside `rail` in `.transcript-split`.
- **Persisted branch:** unchanged content, but move `PresentedPanel` into the shared `rail` so the plan can sit above it. (Behavior for presented files is preserved.)

Both branches use the existing `.transcript-split` flex container; the rail is a vertical stack (`.transcript-rail`).

### RunView.tsx — `useTaskStream` returns `lastEventAt`

Add `const [lastEventAt, setLastEventAt] = useState<number>(0);`. Call `setLastEventAt(Date.now())` at the top of every listener (`snapshot`, `thinking_delta`, `text_delta`, `tool_call`, `tool_result`) and when the stream opens. Reset to `Date.now()` when (re)subscribing. Return `{ blocks, streaming, lastEventAt }`.

### RunView.tsx — `GeneratingIndicator`

```tsx
const STALL_MS = 20000; // > server heartbeat (15s), so normal quiet periods don't false-alarm

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
      <span className="gen-dots"><span></span><span></span><span></span></span>
      <span className="gen-label">
        {stalled ? `Still working — no updates for ${Math.round(gap / 1000)}s` : "Agent is working"}
      </span>
    </div>
  );
}
```

The 1s tick only runs while `streaming`; the counter is derived, not stored, so it stays truthful across reconnects (a reconnect fires `snapshot` → `lastEventAt` resets → `stalled` clears).

### styles.css

Add, matching existing tokens (`--border`, `--muted`, etc.):

- `.transcript-rail` — vertical flex stack sharing the `clamp(...)` width the `.present-panel` uses today; `.plan-panel` sits above `.present-panel`.
- `.plan-panel`, `.plan-head`, `.plan-title`, `.plan-count`, `.plan-list`, `.plan-item`, `.plan-glyph`, `.plan-text`; `.plan-item.completed` muted with a checked glyph, `.plan-item.in_progress` emphasized (accent glyph + subtle background), `.plan-item.pending` default. Long content wraps.
- `.generating` (flex row, muted), `.gen-dots span` three dots with a staggered `@keyframes gen-blink` opacity animation; `.generating.stalled` amber (`--warn` or an inline amber) copy.

## Testing / verification

No UI test runner exists, so:

1. **Typecheck + build:** `cd atom-ui && npm run build` must pass clean (this is `tsc && vite build` — catches type errors in the new code and the `api.ts` type).
2. **Pure-logic check:** `currentPlan` / `todosFromArgs` are pure and exported (or verified via a throwaway `npx tsx` snippet) against: latest-of-multiple `write_todos` wins; `streaming` reads from `blocks`, non-streaming reads from `chat`; malformed/absent `args.todos` → `[]`; empty plan → `[]`.
3. **Visual smoke check:** run `npm run dev` (proxying the API) against a real or replayed run that calls `write_todos` and streams; confirm the Plan panel renders and updates as `write_todos` fires, and the indicator shows "Agent is working" during streaming, flips to "Still working — no updates for Ns" when a gap exceeds 20s, and disappears on `done`.

## Files touched

- `atom-ui/src/api.ts` — `TodoStatus`, `Todo` types.
- `atom-ui/src/RunView.tsx` — `currentPlan`, `todosFromArgs`, `PlanPanel`, `GeneratingIndicator`, `useTaskStream` `lastEventAt`, Transcript rail refactor.
- `atom-ui/src/styles.css` — `.transcript-rail`, `.plan-*`, `.generating`/`.gen-*` styles.

## Risks / mitigations

- **Stall threshold false positives:** 20s > the 15s heartbeat and longer than typical token/tool gaps; the copy says "still working," not "stalled/dead," so it reassures rather than alarms. Tunable via the `STALL_MS` constant.
- **Rail refactor regressing presented files:** `PresentedPanel` markup/props are unchanged; only its container moves into the shared rail. The visual smoke check covers a run with presented files.
- **Malformed todo args:** `todosFromArgs` filters to well-formed `{content, status}` items and returns `[]` otherwise, so the panel never throws on unexpected shapes.
