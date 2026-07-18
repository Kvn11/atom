# RunView Plan Panel + Generating Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface the agent's todos as a live pinned Plan panel in the RunView, and add a gap-aware "agent is working" indicator so the user can tell working from hung.

**Architecture:** Frontend-only (`atom-ui`). Pure plan-extraction logic lives in a React-free `plan.ts` (unit-checkable with Node's TS type-stripping). `RunView.tsx` gains a `PlanPanel`, a `GeneratingIndicator`, a `lastEventAt` signal from `useTaskStream`, and a shared right-rail refactor of `Transcript`. Todo data is derived from the existing `write_todos` tool calls — no backend change.

**Tech Stack:** React + TypeScript, Vite, plain CSS (`styles.css`). No UI test runner exists; verification is Node type-stripping for pure logic + `npm run build` (tsc + vite) + a visual smoke check.

## Global Constraints

- Frontend only. No changes under `src/atom/`. No backend SSE event added.
- No new dependencies; no UI test framework (none exists — YAGNI).
- Todo type: `{ content: string; status: "pending" | "in_progress" | "completed" }`. `write_todos` replaces the whole list each call → the latest call is the current plan.
- Stall threshold `STALL_MS = 20000` (> the 15s server heartbeat).
- Style with existing CSS tokens only: `--accent #4f46e5`, `--accent-weak`, `--ok #15803d`, `--warn #b45309`, `--warn-weak`, `--ink`, `--ink-3`, `--border`, `--surface`, `--radius-sm`. Mirror `.present-panel` conventions.
- All commands run from `atom-ui/`. Build = `npm run build` (`tsc && vite build`).

---

### Task 1: `Todo` type + pure plan extraction (`plan.ts`)

**Files:**
- Modify: `atom-ui/src/api.ts` (add `TodoStatus`, `Todo`)
- Create: `atom-ui/src/plan.ts`
- Check (throwaway, not committed): `atom-ui/plan.check.ts`

**Interfaces:**
- Produces: `Todo`, `TodoStatus` (in `api.ts`); `todosFromArgs(args: unknown): Todo[] | null` and `currentPlan(blocks: StreamBlock[], chat: ChatMsg[], streaming: boolean): Todo[]` (in `plan.ts`).

- [ ] **Step 1: Add the types to `api.ts`**

Append after the `StreamBlock` type (around line 22):

```ts
export type TodoStatus = "pending" | "in_progress" | "completed";
export interface Todo { content: string; status: TodoStatus; }
```

- [ ] **Step 2: Create `atom-ui/src/plan.ts`**

```ts
import type { ChatMsg, StreamBlock, Todo } from "./api";

// write_todos args are untyped at the wire; narrow args.todos to well-formed items or null.
export function todosFromArgs(args: unknown): Todo[] | null {
  const t = (args as { todos?: unknown } | undefined)?.todos;
  if (!Array.isArray(t)) return null;
  const items = t.filter(
    (x): x is Todo =>
      !!x &&
      typeof (x as Todo).content === "string" &&
      typeof (x as Todo).status === "string",
  );
  return items.length ? items : null;
}

// The current plan = the latest write_todos call. Reads from live blocks while the transcript is
// rendering the live stream, else from the persisted chat's tool_calls. Matches the Transcript's
// own branch selection so panel and transcript read the same source.
export function currentPlan(blocks: StreamBlock[], chat: ChatMsg[], streaming: boolean): Todo[] {
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

- [ ] **Step 3: Write the throwaway logic check `atom-ui/plan.check.ts`**

```ts
import assert from "node:assert";
import { currentPlan, todosFromArgs } from "./src/plan.ts";

const wt = (todos: unknown) => ({ kind: "tool_call" as const, name: "write_todos", args: { todos } });
const p = (c: string, s: string) => ({ content: c, status: s });

// todosFromArgs: well-formed vs malformed
assert.deepEqual(todosFromArgs({ todos: [p("a", "pending")] }), [p("a", "pending")]);
assert.equal(todosFromArgs({ todos: "nope" }), null);
assert.equal(todosFromArgs({}), null);
assert.equal(todosFromArgs({ todos: [{ nope: 1 }] }), null);

// currentPlan: latest write_todos wins, from blocks while streaming
const blocks: any = [wt([p("a", "completed")]), { kind: "text", text: "hi" }, wt([p("a", "completed"), p("b", "in_progress")])];
assert.deepEqual(currentPlan(blocks, [], true).map((t) => t.status), ["completed", "in_progress"]);

// currentPlan: from chat when not streaming
const chat: any = [{ role: "ai", text: "", tool_calls: [{ name: "write_todos", args: { todos: [p("x", "pending")] } }] }];
assert.deepEqual(currentPlan([], chat, false), [p("x", "pending")]);

// currentPlan: empty when no plan
assert.deepEqual(currentPlan([{ kind: "text", text: "hi" } as any], [], true), []);
assert.deepEqual(currentPlan([], [{ role: "ai", text: "done" } as any], false), []);

console.log("plan.ts logic OK");
```

- [ ] **Step 4: Run the logic check (expect PASS; Node 24 strips the types)**

Run: `cd atom-ui && node plan.check.ts`
Expected: prints `plan.ts logic OK`, exit 0. (If it fails, fix `plan.ts` — the assertions encode the spec.)

- [ ] **Step 5: Remove the throwaway check and typecheck**

Run: `cd atom-ui && rm plan.check.ts && npx tsc --noEmit`
Expected: no output, exit 0.

- [ ] **Step 6: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/plan.ts
git commit -m "feat(ui): Todo type + pure current-plan extraction from write_todos"
```

---

### Task 2: `GeneratingIndicator` + `useTaskStream` lastEventAt

**Files:**
- Modify: `atom-ui/src/RunView.tsx` (`useTaskStream` return; new `GeneratingIndicator`)
- Modify: `atom-ui/src/styles.css` (`.generating`, `.gen-dots`)

**Interfaces:**
- Consumes: `useTaskStream` (existing).
- Produces: `useTaskStream(...)` returns `{ blocks, streaming, lastEventAt: number }`; `GeneratingIndicator({ streaming, lastEventAt })` component. Wired into the Transcript in Task 3.

- [ ] **Step 1: Add `lastEventAt` to `useTaskStream`**

In `useTaskStream` (RunView.tsx ~line 545), add state and stamp it on every event. After `const [streaming, setStreaming] = useState(false);` add:

```tsx
  const [lastEventAt, setLastEventAt] = useState(0);
```

Inside the effect, right after `setStreaming(true);` add `setLastEventAt(Date.now());`. Then in EACH listener body (`snapshot`, `thinking_delta`, `text_delta`, `tool_call`, `tool_result`), add `setLastEventAt(Date.now());` as the first line. Finally change the return to:

```tsx
  return { blocks, streaming, lastEventAt };
```

Concretely, the listeners become (only the added first line shown per listener):

```tsx
    es.addEventListener("snapshot", (e) => {
      setLastEventAt(Date.now());
      const { blocks: bs } = JSON.parse((e as MessageEvent).data);
      // ...unchanged mapping...
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
```

- [ ] **Step 2: Add the `GeneratingIndicator` component**

Add near the other RunView helper components (e.g. just above `useTaskStream`, ~line 540):

```tsx
const STALL_MS = 20000; // > server heartbeat (15s) so normal quiet periods don't false-alarm

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
```

- [ ] **Step 3: Add the styles to `styles.css`**

Append (near the `.caret` rule, ~line 165):

```css
.generating { display: flex; align-items: center; gap: 9px; padding: 8px 4px 4px; color: var(--ink-3); font-size: 12.5px; }
.generating.stalled { color: var(--warn); }
.gen-dots { display: inline-flex; gap: 4px; }
.gen-dots span { width: 6px; height: 6px; border-radius: 50%; background: currentColor; opacity: .3; animation: gen-blink 1.2s infinite ease-in-out; }
.gen-dots span:nth-child(2) { animation-delay: .2s; }
.gen-dots span:nth-child(3) { animation-delay: .4s; }
@keyframes gen-blink { 0%, 80%, 100% { opacity: .25; } 40% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .gen-dots span { animation: none; opacity: .6; } }
```

- [ ] **Step 4: Typecheck (component is defined but not yet rendered — that's Task 3)**

Run: `cd atom-ui && npx tsc --noEmit`
Expected: exit 0. (`GeneratingIndicator` may be reported unused if `noUnusedLocals` is on; if so, Task 3 wires it — proceed and let Task 3's build be the gate. If tsc errors on unused, temporarily verify with `npm run build` after Task 3 instead.)

- [ ] **Step 5: Commit**

```bash
git add atom-ui/src/RunView.tsx atom-ui/src/styles.css
git commit -m "feat(ui): gap-aware GeneratingIndicator + lastEventAt from useTaskStream"
```

---

### Task 3: `PlanPanel` + Transcript rail refactor (wire it together)

**Files:**
- Modify: `atom-ui/src/RunView.tsx` (import `Todo`/`currentPlan`; `PlanPanel`; `Transcript` rail)
- Modify: `atom-ui/src/styles.css` (`.transcript-rail`, `.plan-*`)

**Interfaces:**
- Consumes: `currentPlan` (Task 1), `Todo` (Task 1), `GeneratingIndicator` (Task 2), existing `PresentedPanel`.
- Produces: the final rendered feature.

- [ ] **Step 1: Update imports in `RunView.tsx`**

Line 5 currently:
```tsx
import { api, artifactUrl, exportDownloadUrl, Artifact, ChatMsg, Manifest, StreamBlock } from "./api";
```
Change to add `Todo` and import `currentPlan`:
```tsx
import { api, artifactUrl, exportDownloadUrl, Artifact, ChatMsg, Manifest, StreamBlock, Todo } from "./api";
import { currentPlan } from "./plan";
```

- [ ] **Step 2: Add the `PlanPanel` component**

Add just above `PresentedPanel` (~line 417):

```tsx
const PLAN_GLYPH: Record<Todo["status"], string> = { completed: "✓", in_progress: "▸", pending: "○" };

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

- [ ] **Step 3: Refactor `Transcript` to use a shared rail + indicator**

In `Transcript`, destructure `lastEventAt` from the hook (line ~341):
```tsx
  const { blocks, streaming, lastEventAt } = useTaskStream(runId, sel, taskStatus);
```

Right after `const presented = useMemo(...)` (~line 342), add:
```tsx
  const plan = currentPlan(blocks, chat, streaming);
  const rail = (plan.length || presented.length) ? (
    <div className="transcript-rail">
      {plan.length > 0 && <PlanPanel todos={plan} />}
      {presented.length > 0 && <PresentedPanel runId={runId} files={presented} onOpen={onOpenArtifact} />}
    </div>
  ) : null;
```

Replace the live-stream branch (currently `if (blocks.length && (streaming || !chat.length)) { return (<div className="transcript">...</div>); }`, ~lines 360-382) with:
```tsx
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
```

Then replace the persisted return (currently the `transcript-split` with inline `{presented.length > 0 && <PresentedPanel .../>}`, ~lines 387-411) with a version that uses the shared `rail`:
```tsx
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
```

(The `pending`/`!chat.length` placeholder early-returns just above stay unchanged — they render before this and carry no rail.)

- [ ] **Step 4: Add the Plan panel styles to `styles.css`**

Append after the `.present-panel` block (~line 188):

```css
.transcript-rail { flex: 0 1 clamp(300px, 40%, 520px); min-width: 0; min-height: 0; display: flex; flex-direction: column;
  border-left: 1px solid var(--border); background: var(--surface); overflow: hidden; }
.transcript-rail .present-panel { flex: 1; min-height: 0; border-left: 0; }
.plan-panel { flex: 0 1 auto; max-height: 48%; display: flex; flex-direction: column; overflow: hidden;
  border-bottom: 1px solid var(--border); }
.plan-head { display: flex; align-items: baseline; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--border); }
.plan-title { font-weight: 600; font-size: 13px; }
.plan-count { font-size: 11px; color: #fff; background: var(--accent); border-radius: 999px; padding: 1px 7px; font-weight: 650; }
.plan-list { list-style: none; margin: 0; padding: 8px 6px; overflow-y: auto; min-height: 0; }
.plan-item { display: flex; gap: 8px; align-items: flex-start; padding: 4px 8px; border-radius: var(--radius-sm); font-size: 13px; line-height: 1.35; }
.plan-glyph { flex: 0 0 auto; width: 16px; text-align: center; color: var(--ink-3); }
.plan-text { min-width: 0; word-break: break-word; }
.plan-item.completed .plan-glyph { color: var(--ok); }
.plan-item.completed .plan-text { color: var(--ink-3); text-decoration: line-through; }
.plan-item.in_progress { background: var(--accent-weak); }
.plan-item.in_progress .plan-glyph { color: var(--accent); }
.plan-item.in_progress .plan-text { color: var(--ink); font-weight: 550; }
```

- [ ] **Step 5: Build (typecheck + bundle) — the integration gate**

Run: `cd atom-ui && npm run build`
Expected: `tsc` passes and `vite build` completes with no errors (a `dist/` is produced). This confirms all three tasks typecheck and bundle together.

- [ ] **Step 6: Commit**

```bash
git add atom-ui/src/RunView.tsx atom-ui/src/styles.css
git commit -m "feat(ui): pinned Plan panel + rail refactor surfacing agent todos live"
```

---

### Task 4: Visual smoke check

**Files:** none (verification only).

- [ ] **Step 1: Build once more to be safe**

Run: `cd atom-ui && npm run build`
Expected: clean.

- [ ] **Step 2: Visual check (if an API + a run with todos is available)**

Run the dev UI (`cd atom-ui && npm run dev`) with the atom API serving a run whose task calls `write_todos` and streams. Confirm:
- The **Plan panel** appears on the right, shows the checklist with ✓/▸/○ glyphs and `{done}/{total} done`, and updates as `write_todos` fires.
- The **indicator** shows "Agent is working" with animated dots while streaming, flips to "Still working — no updates for Ns" after a >20s gap, and disappears when the task reaches `done`.
- A run with **presented files** still shows the Presented panel (now below the Plan panel in the rail).

If no live API/run is available, note that the visual check was deferred and rely on the build + logic check; do not claim the visual check passed.

---

## Self-review notes (author)

- **Spec coverage:** Plan panel → Task 1 (`currentPlan`) + Task 3 (`PlanPanel`, rail); indicator → Task 2 (`GeneratingIndicator`, `lastEventAt`) + Task 3 (wiring); `Todo` type → Task 1; styles → Tasks 2-3; verification (build + logic + visual) → Tasks 1/3/4. No backend change (spec non-goal honored).
- **Placeholder scan:** none — all steps carry concrete code/commands.
- **Type consistency:** `Todo`/`TodoStatus`, `currentPlan(blocks, chat, streaming)`, `todosFromArgs`, `useTaskStream` `{ blocks, streaming, lastEventAt }`, `GeneratingIndicator({ streaming, lastEventAt })`, `PlanPanel({ todos })`, `STALL_MS` used identically across tasks.
- **Note on tsc `noUnusedLocals`:** `GeneratingIndicator` is defined in Task 2 and consumed in Task 3; if the project enables `noUnusedLocals`, Task 2's standalone `tsc --noEmit` may flag it — Task 3's build is the true gate. Called out in Task 2 Step 4.
