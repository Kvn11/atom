# Sub-agent status cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render each `delegate_task` sub-agent as one self-contained transcript card showing running / finished / failed status and a collapsible report, correct under parallel fan-out.

**Architecture:** Pair each sub-agent's tool call with its result by `tool_call_id` to derive status. The live SSE stream already carries the ids; two small backend edits make the *persisted* transcript carry them too and mark a sub-agent failure as a real tool error. The UI folds each call+result into a `<SubAgentCard>`; pure pairing/status helpers live in a new `atom-ui/src/subagent.tsx`.

**Tech Stack:** Python (FastAPI, LangChain, pytest); React 18 + TypeScript + Vite (atom-ui).

## Global Constraints

- No new dependencies (backend or frontend).
- `atom-ui` has **no test runner**: verify frontend with `npx tsc` (type-check; tsconfig is `noEmit`) and an esbuild-bundle + `node` smoke run. Do not add a test framework or commit throwaway verify scripts.
- Reuse existing CSS tokens/classes from `atom-ui/src/styles.css` (`--ok/--warn/--err(+ -weak)`, `--idle`, `--border`, `--surface`, `--ink-2/-3`, `--mono`, `--radius-sm`, `.pill`, `.tag`, `@keyframes gen-blink`). No new color literals.
- Backend tests run with `pytest` from the repo root (`testpaths = ["tests"]`).
- Keep the visual card scoped to `delegate_task` only; do not restyle other tool calls (`present_files`, etc.).

---

### Task 1: Persist pairing ids + error flag in `serialize_messages`

**Files:**
- Modify: `src/atom/workflow/run_store.py` (`serialize_messages`, ~lines 116-123)
- Test: `tests/test_workflow_run_store.py` (extend `test_serialize_messages_shape` ~line 57; add one test)

**Interfaces:**
- Produces (persisted `ChatMsg` dict shape consumed by Task 3's `pairChat`):
  - a tool_call entry: `{"name": str, "args": dict, "id": str | None}`
  - a tool-result entry (from a `ToolMessage`): adds `"tool_call_id": str` and `"is_error": bool`

- [ ] **Step 1: Update the existing test to expect `id`, and add an error-flag test**

In `tests/test_workflow_run_store.py`, change the assertion in `test_serialize_messages_shape` (currently line 67) and extend the checks:

```python
def test_serialize_messages_shape():
    msgs = [
        HumanMessage(content="do it"),
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "p"}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content="ok", tool_call_id="c1", name="write_file"),
        AIMessage(content="done"),
    ]
    out = serialize_messages(msgs)
    # The opening prompt of a workflow task is authored by the automated workflow, not a human.
    assert out[0] == {"role": "task", "text": "do it"}
    assert out[1]["tool_calls"] == [{"name": "write_file", "args": {"path": "p"}, "id": "c1"}]
    assert out[2]["role"] == "tool" and out[2]["name"] == "write_file"
    assert out[2]["tool_call_id"] == "c1" and out[2]["is_error"] is False
    assert out[3]["text"] == "done"


def test_serialize_messages_marks_tool_error():
    msgs = [
        AIMessage(content="", tool_calls=[{"name": "delegate_task", "args": {}, "id": "d1", "type": "tool_call"}]),
        ToolMessage(content="[sub-agent 'x' timed out after 900s]", tool_call_id="d1", status="error"),
    ]
    out = serialize_messages(msgs)
    assert out[0]["tool_calls"][0]["id"] == "d1"
    assert out[1]["tool_call_id"] == "d1" and out[1]["is_error"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_workflow_run_store.py::test_serialize_messages_shape tests/test_workflow_run_store.py::test_serialize_messages_marks_tool_error -v`
Expected: FAIL — `test_serialize_messages_shape` KeyError/assert on missing `"id"`/`"tool_call_id"`; the new test fails on missing keys.

- [ ] **Step 3: Add the ids + error flag in `serialize_messages`**

In `src/atom/workflow/run_store.py`, replace the body of the per-message loop (the block building `entry`) with:

```python
        entry: dict = {"role": role, "text": message_text(m)}
        tcs = getattr(m, "tool_calls", None)
        if tcs:
            entry["tool_calls"] = [
                {"name": c.get("name"), "args": c.get("args", {}), "id": c.get("id")} for c in tcs
            ]
        name = getattr(m, "name", None)
        if name:
            entry["name"] = name
        tcid = getattr(m, "tool_call_id", None)
        if tcid:  # a ToolMessage — record the call it answers and whether it errored
            entry["tool_call_id"] = tcid
            entry["is_error"] = getattr(m, "status", None) == "error"
        out.append(entry)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_workflow_run_store.py -v`
Expected: PASS (all serialize tests green).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(run_store): persist tool_call ids + error flag for call/result pairing"
```

---

### Task 2: Mark sub-agent failure as a tool error

**Files:**
- Modify: `src/atom/subagent.py` (`SubagentRunner.run`, ~lines 175-229)
- Modify: `src/atom/tools/subagent.py` (`delegate_task`, whole body)
- Test: `tests/test_subagent.py` (fix the unpack at line 231; add two tests)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `SubagentRunner.run(...) -> tuple[str, dict[str, int], bool]` — third element `failed` is `True` on timeout/exception, else `False`.
  - `delegate_task` result `ToolMessage` carries `status="error"` when the sub-agent failed, else `status="success"` (the default). This makes streaming `is_error=True` (via `streaming.py`, unchanged) and persisted `is_error=True` (via Task 1).

- [ ] **Step 1: Write failing tests for the `failed` flag and the tool's error status**

Add to `tests/test_subagent.py`:

```python
@pytest.mark.asyncio
async def test_run_reports_failed_on_success_and_exception(base_config):
    from atom.subagent import SubagentRunner
    from tests.conftest import ScriptedChatModel

    model = ScriptedChatModel(responses=[AIMessage(content="OK")], profile={"max_input_tokens": 100_000})
    runner = SubagentRunner(model=model, home=str(base_config.home),
                            context_window=100_000, bash_enabled=False)

    class _OkAgent:
        async def ainvoke(self, inp, config=None, context=None):
            return {"messages": [AIMessage(content="OK")]}

    runner._child_agent = lambda st, system=None: _OkAgent()
    text, _usage, failed = await runner.run("p1", "d", "go", "general-purpose")
    assert text == "OK" and failed is False

    class _BoomAgent:
        async def ainvoke(self, inp, config=None, context=None):
            raise RuntimeError("boom")

    runner._child_agent = lambda st, system=None: _BoomAgent()
    text, _usage, failed = await runner.run("p1", "d", "go", "general-purpose")
    assert failed is True and text.startswith("[sub-agent 'd' failed:")


@pytest.mark.asyncio
async def test_run_reports_failed_on_timeout(base_config):
    import asyncio
    from atom.subagent import SubagentRunner
    from tests.conftest import ScriptedChatModel

    model = ScriptedChatModel(responses=[AIMessage(content="OK")], profile={"max_input_tokens": 100_000})
    runner = SubagentRunner(model=model, home=str(base_config.home),
                            context_window=100_000, bash_enabled=False)
    runner.timeout_seconds = 0.01

    class _SlowAgent:
        async def ainvoke(self, inp, config=None, context=None):
            await asyncio.sleep(1)
            return {"messages": [AIMessage(content="OK")]}

    runner._child_agent = lambda st, system=None: _SlowAgent()
    text, _usage, failed = await runner.run("p1", "slow", "go", "general-purpose")
    assert failed is True and "timed out" in text


@pytest.mark.asyncio
async def test_delegate_task_sets_error_status_on_failure():
    from types import SimpleNamespace
    from atom.subagent import register_runner, unregister_runner
    from atom.tools.subagent import delegate_task

    class _FakeRunner:
        async def run(self, thread_id, description, prompt, subagent_type):
            return "[sub-agent 'x' failed: RuntimeError: boom]", {}, True

    register_runner("p1", _FakeRunner())
    try:
        runtime = SimpleNamespace(context={"thread_id": "p1"}, tool_call_id="tc1")
        cmd = await delegate_task.func(runtime, description="x", prompt="go", subagent_type="general-purpose")
    finally:
        unregister_runner("p1")
    msg = cmd.update["messages"][0]
    assert msg.status == "error" and msg.tool_call_id == "tc1"


@pytest.mark.asyncio
async def test_delegate_task_success_status_is_not_error():
    from types import SimpleNamespace
    from atom.subagent import register_runner, unregister_runner
    from atom.tools.subagent import delegate_task

    class _FakeRunner:
        async def run(self, thread_id, description, prompt, subagent_type):
            return "report", {"total_tokens": 5}, False

    register_runner("p1", _FakeRunner())
    try:
        runtime = SimpleNamespace(context={"thread_id": "p1"}, tool_call_id="tc2")
        cmd = await delegate_task.func(runtime, description="x", prompt="go", subagent_type="general-purpose")
    finally:
        unregister_runner("p1")
    msg = cmd.update["messages"][0]
    assert msg.status == "success" and cmd.update["usage"] == {"total_tokens": 5}
```

Also fix the existing unpack at `tests/test_subagent.py:231`:

```python
    text, _usage, _failed = await runner.run("p1", "do the thing", "go", "general-purpose")
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/test_subagent.py -k "failed or error_status or not_error" -v`
Expected: FAIL — `run()` returns a 2-tuple (unpack error) and the tool has no `status`.

- [ ] **Step 3: Change `run()` to return the `failed` flag**

In `src/atom/subagent.py`, update the signature and every `return` in `run`:

```python
    async def run(
        self, parent_thread_id: str, description: str, prompt: str, subagent_type: SubagentType
    ) -> tuple[str, dict[str, int], bool]:
        """Run a child agent; return ``(report_text, usage_delta, failed)``."""
```

Then, inside the same method, change the four return sites:

```python
            except asyncio.TimeoutError:
                return f"[sub-agent '{description}' timed out after {self.timeout_seconds}s]", {}, True
            except Exception as exc:  # noqa: BLE001
                return f"[sub-agent '{description}' failed: {type(exc).__name__}: {exc}]", {}, True
```

```python
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    text = message_text(msg)
                    if text.strip():
                        return text, usage, False
            return "[sub-agent produced no output]", usage, False
```

- [ ] **Step 4: Map `failed` to the ToolMessage status in the tool**

Replace the body of `delegate_task` in `src/atom/tools/subagent.py` after the docstring:

```python
    tcid = runtime.tool_call_id
    runner = get_runner(thread_id_of(runtime))
    if runner is None:
        return Command(update={"messages": [ToolMessage(
            "[sub-agent delegation is unavailable in this run]",
            tool_call_id=tcid, status="error")]})
    text, usage, failed = await runner.run(thread_id_of(runtime), description, prompt, subagent_type)
    update: dict = {"messages": [ToolMessage(
        text, tool_call_id=tcid, status="error" if failed else "success")]}
    if usage:  # attribute the child's token usage to the parent run
        update["usage"] = usage
    return Command(update=update)
```

- [ ] **Step 5: Run the subagent tests to verify they pass**

Run: `pytest tests/test_subagent.py -v`
Expected: PASS (including the previously-passing trace tests, now using the 3-tuple unpack).

- [ ] **Step 6: Commit**

```bash
git add src/atom/subagent.py src/atom/tools/subagent.py tests/test_subagent.py
git commit -m "feat(subagent): surface sub-agent failure as a tool error status"
```

---

### Task 3: Frontend types, stream plumbing, and pure pairing/status helpers

**Files:**
- Modify: `atom-ui/src/api.ts` (`ChatMsg`, `StreamBlock`)
- Create: `atom-ui/src/subagent.tsx` (pure helpers now; component added in Task 4)
- Modify: `atom-ui/src/RunView.tsx` (`useTaskStream` only — carry `tool_call_id` into tool_result blocks)

**Interfaces:**
- Consumes: the persisted dict shape from Task 1; the stream `tool_result` event fields `tool_call_id`, `name`, `text`, `is_error` (already emitted by `src/atom/streaming.py`).
- Produces (consumed by Task 4):
  - `SubStatus = "running" | "done" | "failed" | "incomplete"`
  - `SubResult = { text: string; isError: boolean }`
  - `Pairing = { delegateIds: Set<string>; resultByCallId: Map<string, SubResult> }`
  - `pairBlocks(blocks: StreamBlock[]): Pairing`
  - `pairChat(chat: ChatMsg[]): Pairing`
  - `subStatus(result: SubResult | undefined, streaming: boolean): SubStatus`
  - `subSummary(status: SubStatus, report: string | undefined): string`

- [ ] **Step 1: Extend the types in `api.ts`**

In `atom-ui/src/api.ts`, update `ChatMsg` and the `StreamBlock` `tool_result` variant:

```ts
export interface ChatMsg {
  role: string; text: string; name?: string;
  tool_call_id?: string; is_error?: boolean;
  tool_calls?: { name: string; args?: Record<string, unknown>; id?: string }[];
}
export type StreamBlock =
  | { kind: "thinking"; text: string }
  | { kind: "text"; text: string }
  | { kind: "tool_call"; id?: string; name?: string; args?: Record<string, unknown> }
  | { kind: "tool_result"; toolCallId?: string; name?: string; text: string; isError: boolean };
```

- [ ] **Step 2: Carry `tool_call_id` into tool_result blocks in `useTaskStream`**

In `atom-ui/src/RunView.tsx`, in the `snapshot` listener's `mapped` expression, change the tool_result fallback to include `toolCallId`:

```ts
        : { kind: "tool_result", toolCallId: b.tool_call_id, name: b.name, text: b.text, isError: b.is_error });
```

And in the `tool_result` event listener, change the pushed block:

```ts
      setBlocks((prev) => [...prev, { kind: "tool_result", toolCallId: d.tool_call_id, name: d.name, text: d.text, isError: d.is_error }]);
```

- [ ] **Step 3: Create the pure helpers in `subagent.tsx`**

Create `atom-ui/src/subagent.tsx`:

```tsx
import { useState } from "react";
import { ChatMsg, StreamBlock } from "./api";

export type SubStatus = "running" | "done" | "failed" | "incomplete";
export interface SubResult { text: string; isError: boolean; }
export interface Pairing { delegateIds: Set<string>; resultByCallId: Map<string, SubResult>; }

const DELEGATE = "delegate_task";

// Live stream: delegate call-ids from tool_call blocks; results keyed by the id they answer.
export function pairBlocks(blocks: StreamBlock[]): Pairing {
  const delegateIds = new Set<string>();
  const resultByCallId = new Map<string, SubResult>();
  for (const b of blocks) {
    if (b.kind === "tool_call" && b.name === DELEGATE && b.id) delegateIds.add(b.id);
    else if (b.kind === "tool_result" && b.toolCallId)
      resultByCallId.set(b.toolCallId, { text: b.text, isError: b.isError });
  }
  return { delegateIds, resultByCallId };
}

// Persisted transcript: same, from serialized messages (tool_calls[].id + ToolMessage tool_call_id).
export function pairChat(chat: ChatMsg[]): Pairing {
  const delegateIds = new Set<string>();
  const resultByCallId = new Map<string, SubResult>();
  for (const m of chat) {
    for (const c of m.tool_calls ?? []) if (c.name === DELEGATE && c.id) delegateIds.add(c.id);
    if (m.tool_call_id) resultByCallId.set(m.tool_call_id, { text: m.text, isError: !!m.is_error });
  }
  return { delegateIds, resultByCallId };
}

// No result yet -> running while the task streams, else a dangling call in a terminal transcript.
export function subStatus(result: SubResult | undefined, streaming: boolean): SubStatus {
  if (!result) return streaming ? "running" : "incomplete";
  return result.isError ? "failed" : "done";
}

// One-line summary: the failure reason (sentinel stripped) or the report's first line.
export function subSummary(status: SubStatus, report: string | undefined): string {
  if (!report) return "";
  if (status === "failed") {
    const m = report.match(/^\[sub-agent '.*?' (.*)\]\s*$/s);
    return m ? m[1] : firstLine(report, 80);
  }
  if (status === "done") return firstLine(report, 80) || "reported";
  return "";
}

function firstLine(s: string, n: number): string {
  const line = s.split("\n").find((l) => l.trim()) ?? "";
  return line.length > n ? line.slice(0, n - 1) + "…" : line;
}
```

- [ ] **Step 4: Type-check**

Run: `cd atom-ui && npx tsc`
Expected: no output, exit 0. (`react-jsx` + `noEmit` are set in `tsconfig.json`.)

Note: `subagent.tsx` imports `useState` but does not use it yet — that is fine (`noUnusedLocals` is `false`). Task 4 uses it.

- [ ] **Step 5: Verify the pure helpers with an esbuild + node smoke run**

Write this throwaway driver to the scratchpad (do NOT commit it):

`/private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-pair.tsx`

```tsx
import { pairBlocks, pairChat, subStatus, subSummary } from "/Users/kev/gitclones/atom/atom-ui/src/subagent";

const blocks: any[] = [
  { kind: "tool_call", id: "a", name: "delegate_task", args: { description: "A", subagent_type: "bash" } },
  { kind: "tool_call", id: "b", name: "delegate_task", args: { description: "B", subagent_type: "bash" } },
  { kind: "tool_result", toolCallId: "b", text: "[sub-agent 'B' timed out after 900s]", isError: true },
];
const p = pairBlocks(blocks);
console.assert(p.delegateIds.has("a") && p.delegateIds.has("b"), "delegateIds");
console.assert(subStatus(p.resultByCallId.get("a"), true) === "running", "a running");
console.assert(subStatus(p.resultByCallId.get("a"), false) === "incomplete", "a incomplete");
console.assert(subStatus(p.resultByCallId.get("b"), true) === "failed", "b failed");
console.assert(subSummary("failed", "[sub-agent 'B' timed out after 900s]") === "timed out after 900s", "summary");

const chat: any[] = [
  { role: "ai", text: "", tool_calls: [{ name: "delegate_task", args: { description: "C" }, id: "c" }] },
  { role: "tool", text: "done report line 1\nmore", tool_call_id: "c", is_error: false },
];
const q = pairChat(chat);
console.assert(subStatus(q.resultByCallId.get("c"), false) === "done", "c done");
console.assert(subSummary("done", q.resultByCallId.get("c")!.text) === "done report line 1", "done summary");
console.log("OK: pairing/status helpers pass");
```

Run (bundles from `atom-ui` so `react` etc. resolve, then executes):

```bash
cd atom-ui && npx esbuild /private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-pair.tsx --bundle --platform=node --format=esm --outfile=/private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-pair.mjs && node /private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-pair.mjs
```

Expected: `OK: pairing/status helpers pass` and no `Assertion failed` lines.

- [ ] **Step 6: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/subagent.tsx atom-ui/src/RunView.tsx
git commit -m "feat(ui): sub-agent pairing/status helpers + carry tool_call_id in stream"
```

---

### Task 4: `<SubAgentCard>` component, transcript integration, and styles

**Files:**
- Modify: `atom-ui/src/subagent.tsx` (add `SubAgentCard`)
- Modify: `atom-ui/src/RunView.tsx` (`Transcript`: pairing memo + both render loops + import)
- Modify: `atom-ui/src/styles.css` (append card styles)

**Interfaces:**
- Consumes: `pairBlocks`, `pairChat`, `subStatus`, `subSummary`, `SubResult` from Task 3.
- Produces: `SubAgentCard({ description, subagentType, result, streaming })` — `result?: SubResult`, `streaming: boolean`.

- [ ] **Step 1: Add the `SubAgentCard` component to `subagent.tsx`**

Append to `atom-ui/src/subagent.tsx`:

```tsx
const STATUS_PILL: Record<SubStatus, string> = { running: "warn", done: "ok", failed: "err", incomplete: "idle" };

// One delegate_task rendered as a status card: description + type badge + running/done/failed pill,
// a one-line summary, and a collapsible (default-closed) full report. Status is derived, not fetched.
export function SubAgentCard(
  { description, subagentType, result, streaming }:
  { description: string; subagentType: string; result: SubResult | undefined; streaming: boolean },
) {
  const [open, setOpen] = useState(false);
  const status = subStatus(result, streaming);
  const report = result?.text;
  const summary = subSummary(status, report);
  return (
    <div className={`subagent-card ${status}`}>
      <div className="sa-head">
        <span className="sa-icon" aria-hidden="true">{"\u{1F916}"}</span>
        <span className="sa-title" title={description}>{description}</span>
        <span className={`pill ${STATUS_PILL[status]} sa-status`}>
          {status === "running" && <span className="sa-live" aria-hidden="true" />}
          {status}
        </span>
      </div>
      <div className="sa-sub">
        <span className="tag">{subagentType}</span>
        {summary && <span className="sa-summary" title={summary}>{summary}</span>}
        {report && (
          <button className="sa-toggle" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            {open ? "▾ hide report" : "▸ view report"}
          </button>
        )}
      </div>
      {open && report && <div className="sa-report">{report}</div>}
    </div>
  );
}
```

- [ ] **Step 2: Import the helpers + component in `RunView.tsx`**

Add near the other imports at the top of `atom-ui/src/RunView.tsx`:

```ts
import { SubAgentCard, pairBlocks, pairChat } from "./subagent";
```

- [ ] **Step 3: Add pairing memos in `Transcript`**

In `atom-ui/src/RunView.tsx`, inside `Transcript`, just after `const plan = currentPlan(blocks, chat, streaming);`, add:

```ts
  const livePair = useMemo(() => pairBlocks(blocks), [blocks]);
  const chatPair = useMemo(() => pairChat(chat), [chat]);
```

- [ ] **Step 4: Render cards in the live-stream loop**

In the `if (streaming || (blocks.length && !chat.length))` branch, replace the `blocks.map(...)` callback so delegate calls become cards and their results are folded away:

```tsx
            {blocks.map((b, i) => {
              const isLast = i === blocks.length - 1;
              if (b.kind === "tool_call" && b.name === "delegate_task" && b.id)
                return <SubAgentCard key={i}
                  description={String(b.args?.description ?? "sub-agent")}
                  subagentType={String(b.args?.subagent_type ?? "general-purpose")}
                  result={livePair.resultByCallId.get(b.id)} streaming={streaming} />;
              if (b.kind === "tool_result" && b.toolCallId && livePair.delegateIds.has(b.toolCallId))
                return null;   // folded into its card
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
```

- [ ] **Step 5: Render cards in the persisted-chat loop**

Replace the final `chat.map(...)` block (the persisted transcript) with:

```tsx
          {chat.map((m, i) => {
            if (m.tool_call_id && chatPair.delegateIds.has(m.tool_call_id)) return null;  // folded into its card
            if (m.tool_calls?.length) return (
              <div key={i} className="msg tool-calls">
                {m.text && <div className="msg-text md"><Markdown>{m.text}</Markdown></div>}
                {m.tool_calls.map((c, k) =>
                  c.name === "delegate_task" && c.id ? (
                    <SubAgentCard key={k}
                      description={String(c.args?.description ?? "sub-agent")}
                      subagentType={String(c.args?.subagent_type ?? "general-purpose")}
                      result={chatPair.resultByCallId.get(c.id)} streaming={false} />
                  ) : (
                    <div key={k} className={`toolcall${c.name === "present_files" ? " present" : ""}`}>
                      <span className="tc-name">{c.name === "present_files" ? "⇪ present_files" : `→ ${c.name}`}</span>
                      <span className="tc-args">{argSummary(c.args)}</span>
                    </div>
                  )
                )}
              </div>
            );
            return (
              <div key={i} className={`msg ${m.role}`}>
                <div className="msg-role">{m.name || m.role}</div>
                {m.role === "ai"
                  ? <div className="msg-text md"><Markdown>{m.text}</Markdown></div>
                  : <div className="msg-text">{m.text}</div>}
              </div>
            );
          })}
```

- [ ] **Step 6: Append the card styles**

Add to the end of `atom-ui/src/styles.css`:

```css
/* Sub-agent (delegate_task) status cards */
.subagent-card { border: 1px solid var(--border); background: var(--surface); border-radius: var(--radius-sm); padding: 9px 11px; margin: 4px 0; }
.subagent-card.failed { border-color: var(--err-weak); background: var(--err-weak); }
.sa-head { display: flex; align-items: center; gap: 8px; }
.sa-icon { flex: none; font-size: 14px; line-height: 1; }
.sa-title { font-weight: 600; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sa-status { margin-left: auto; flex: none; }
.sa-live { width: 7px; height: 7px; border-radius: 999px; background: currentColor; margin-right: 5px; display: inline-block; animation: gen-blink 1.2s infinite ease-in-out; }
.sa-sub { display: flex; align-items: center; gap: 8px; margin-top: 5px; font-size: 12px; color: var(--ink-3); }
.sa-summary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sa-toggle { margin-left: auto; flex: none; border: 0; background: transparent; color: var(--ink-2); font-size: 12px; padding: 0; cursor: pointer; }
.sa-report { margin-top: 7px; padding-top: 7px; border-top: 1px solid var(--border); font-family: var(--mono); font-size: 12px; color: var(--ink-2); white-space: pre-wrap; overflow-x: auto; }
@media (prefers-reduced-motion: reduce) { .sa-live { animation: none; } }
```

- [ ] **Step 7: Type-check**

Run: `cd atom-ui && npx tsc`
Expected: no output, exit 0.

- [ ] **Step 8: SSR smoke-render the card in all three states**

Write this throwaway driver to the scratchpad (do NOT commit):

`/private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-card.tsx`

```tsx
import { renderToStaticMarkup } from "react-dom/server";
import { createElement as h } from "react";
import { SubAgentCard } from "/Users/kev/gitclones/atom/atom-ui/src/subagent";

const cases = [
  { description: "Recon /api/v2/orders", subagentType: "bash", result: undefined, streaming: true },
  { description: "Recon /api/v2/orders", subagentType: "bash", result: { text: "3 findings reported", isError: false }, streaming: false },
  { description: "Audit /api/login", subagentType: "bash", result: { text: "[sub-agent 'Audit /api/login' timed out after 900s]", isError: true }, streaming: false },
];
const html = cases.map((c) => renderToStaticMarkup(h(SubAgentCard, c as any))).join("\n");
console.log(html);
for (const s of ["running", "done", "failed", "timed out after 900s", "view report"]) {
  if (!html.includes(s)) { console.error("MISSING:", s); process.exit(1); }
}
if ((html.match(/subagent-card/g) || []).length !== 3) { console.error("expected 3 cards"); process.exit(1); }
console.log("OK: three sub-agent states rendered");
```

Run:

```bash
cd atom-ui && npx esbuild /private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-card.tsx --bundle --platform=node --format=esm --outfile=/private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-card.mjs && node /private/tmp/claude-501/-Users-kev-gitclones-atom/869a44f3-2050-4081-9785-08a5d7a25837/scratchpad/verify-card.mjs
```

Expected: prints three `subagent-card` blocks and `OK: three sub-agent states rendered`.

- [ ] **Step 9: Build the UI to confirm the production bundle compiles**

Run: `cd atom-ui && npm run build`
Expected: `tsc` passes and `vite build` writes `dist/` with no errors.

- [ ] **Step 10: Commit**

```bash
git add atom-ui/src/subagent.tsx atom-ui/src/RunView.tsx atom-ui/src/styles.css
git commit -m "feat(ui): render delegate_task sub-agents as running/finished/failed status cards"
```

---

## Final verification (after all tasks)

- [ ] Run the backend suite: `pytest tests/test_workflow_run_store.py tests/test_subagent.py tests/test_streaming.py -v` → all PASS.
- [ ] `cd atom-ui && npm run build` → clean build.
- [ ] Remove scratchpad verify scripts (they were never committed).

## Notes / risks

- **No streaming change needed:** `src/atom/streaming.py › translate_update` already emits `is_error` (`status == "error"`) and `tool_call_id`; Task 2 makes those meaningful for sub-agents and Task 3 stops discarding `tool_call_id` in the UI.
- **Parallel fan-out:** correctness rests on `tool_call_id` pairing (not order/adjacency), so N sibling cards each flip independently as their result arrives.
- **`incomplete` state:** a dangling delegate call in a *terminal* transcript (parent crashed mid-delegation) shows a muted `incomplete` pill rather than a perpetual spinner.
