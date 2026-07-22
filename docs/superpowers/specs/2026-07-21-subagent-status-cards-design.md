# Sub-agent status cards

**Date:** 2026-07-21
**Branch:** `feat/subagent-status-cards`
**Status:** Approved design — ready for implementation planning.

## Problem

A `delegate_task` sub-agent currently renders as two disconnected pieces in a task
transcript:

1. A dim monospace line `→ delegate_task` whose args are dumped by `argSummary`
   (`description=…, prompt=…`, each truncated to 40 chars — the readable *description*
   is buried and the large *prompt* is noise).
2. Later, a separate plain `tool` block containing the sub-agent's report.

Nothing pairs the call with its result, so the transcript gives **no indication of
whether a sub-agent is running, finished, or failed**. This is worst exactly where it
matters most: leads fan out many `bash` sub-agents in parallel (e.g. the secassess
workflow delegates per-endpoint / per-finding work), producing a wall of call lines
followed by a wall of result blocks with no correspondence between them.

Failure is also invisible: a sub-agent timeout/crash returns a normal `ToolMessage`
whose text is `[sub-agent '…' failed: …]` / `[sub-agent '…' timed out after …s]`. It
carries **no** `status="error"`, so nothing renders it as a failure.

## Goal

Render each `delegate_task` sub-agent as **one self-contained status card** — in both
the live SSE stream and the persisted transcript — showing its description, sub-agent
type, a **running / finished / failed** status, a one-line summary, and a collapsible
full report. Status is derived by pairing each call with its result via
`tool_call_id`, so it stays correct under parallel fan-out.

## Key facts (verified in the current code)

- The tool is `delegate_task` (`src/atom/tools/subagent.py`); args are `description`,
  `prompt`, `subagent_type` (`"general-purpose" | "bash"`).
- The SSE stream **already** carries the ids and error flag we need
  (`src/atom/streaming.py › translate_update`):
  `tool_call` events carry `id`; `tool_result` events carry `tool_call_id`, `name`,
  `text`, and `is_error` (= `status == "error"`). The UI simply discards `tool_call_id`
  today (`useTaskStream` maps results to `{kind, name, text, isError}` only).
- The **persisted** serializer (`src/atom/workflow/run_store.py › serialize_messages`)
  omits both the tool_call `id` and the `ToolMessage.tool_call_id` / error status, so
  the completed transcript cannot pair a call with its result.
- `SubagentRunner.run` (`src/atom/subagent.py`) returns `(text, usage)` and encodes
  timeout/exception only as sentinel text — no failure signal reaches the tool.
- `SubagentRunner.run` has exactly one production caller (`tools/subagent.py:38`) plus
  two test call sites (`tests/test_subagent.py:231,262`).
- `atom-ui` has **no test runner**; UI is verified with a `tsc` type-check and an
  esbuild SSR smoke-render (project norm).

## Design

### Backend (3 files, small, low-risk)

1. **`src/atom/workflow/run_store.py › serialize_messages`** — persist the pairing ids
   and error flag:
   - each tool_call entry gains `"id": c.get("id")`;
   - a `ToolMessage` entry gains `"tool_call_id"` (from `m.tool_call_id`) and
     `"is_error": getattr(m, "status", None) == "error"`.

2. **`src/atom/subagent.py › SubagentRunner.run`** — change the return to
   `(text, usage, failed)`:
   - `failed=True` on `asyncio.TimeoutError` and on the caught `Exception`;
   - `failed=False` on the normal path (including `"[sub-agent produced no output]"` —
     empty output is not a failure).

3. **`src/atom/tools/subagent.py › delegate_task`** — construct the result with the
   error status:
   `ToolMessage(text, tool_call_id=tcid, status="error" if failed else "success")`.
   (The "delegation unavailable" early-return path may also be marked `status="error"`.)

No streaming change is required: once (3) lands, `translate_update` emits
`is_error=True` for failed sub-agents automatically, and the persisted view learns the
same via (1).

### Frontend

**`atom-ui/src/api.ts` — types**
- `ChatMsg.tool_calls[]` entries: add `id?: string`.
- `ChatMsg`: add `tool_call_id?: string` and `is_error?: boolean` (populated on
  tool-result messages).
- `StreamBlock` `tool_result` variant: add `toolCallId?: string`.

**`atom-ui/src/RunView.tsx › useTaskStream`**
- Carry `tool_call_id` into `tool_result` blocks in both the `snapshot` mapping
  (`toolCallId: b.tool_call_id`) and the live `tool_result` listener
  (`toolCallId: d.tool_call_id`). This is the only change to the stream hook.

**`atom-ui/src/RunView.tsx › Transcript` — pairing + rendering**

A shared helper derives, from the ordered items of whichever view is active:
- `resultByCallId: Map<string, { text: string; isError: boolean }>` — from
  `tool_result` blocks that have a `toolCallId` (live), or messages with a
  `tool_call_id` (persisted);
- `delegateCallIds: Set<string>` — ids of tool_calls whose `name === "delegate_task"`.

Render rules (applied in both the live-blocks loop and the persisted-chat loop):
- a `delegate_task` tool_call → `<SubAgentCard>` (see below);
- a tool_result whose `toolCallId ∈ delegateCallIds` → **skipped** (folded into its
  card, never rendered as a standalone block);
- every other item (text, thinking, `present_files`, other tool calls / results)
  renders exactly as today.

In the persisted loop a single AI message may mix delegate and non-delegate tool_calls;
the decision is made **per tool_call**, so non-delegate calls keep the existing
`toolcall` line.

**`<SubAgentCard>` component**

Props (status derived by the parent, card stays presentational):
`{ description: string; subagentType: string; status: SubStatus; report?: string }`
where `SubStatus = "running" | "done" | "failed" | "incomplete"`.

Layout:
- Header: 🤖 (decorative, `aria-hidden`) + `description` as the title + a status pill
  (reuse `.pill`: warn=running, ok=done, err=failed, idle=incomplete). The pill is
  **text-labeled** so it is never color-only. Running shows an animated dot (reuse the
  `.dot.warn` ring; honor `prefers-reduced-motion`).
- Sub-row: `subagent_type` badge (reuse `.tag`) · one-line summary. Summary:
  - **failed** → the reason parsed from the sentinel (strip the `[sub-agent '…' ]`
    wrapper → e.g. `timed out after 900s`, `failed: TimeoutError: …`);
  - **done** → the first non-empty line of the report (truncated), else `reported`;
  - **running/incomplete** → none.
- When `report` is present: a `▸ view report` toggle (`<button aria-expanded>`) reveals
  the full report in a collapsed monospace, pre-wrapped body. **Collapsed by default.**

**Status rule:**
```
result = resultByCallId.get(callId)
if (!result)      status = streaming ? "running" : "incomplete"
else if (result.isError) status = "failed"
else              status = "done"
```
`incomplete` covers the rare dangling call in a terminal transcript (parent crashed
mid-delegation) — a muted state, never a spinner that runs forever.

**`atom-ui/src/styles.css`**
- `.subagent-card` and children (`.sa-head`, `.sa-title`, `.sa-type`, `.sa-summary`,
  `.sa-toggle`, `.sa-report`) built from existing tokens (`--ok/--warn/--err/--surface`,
  `--mono`, `.pill`, `.dot`, `.tag`). Running-dot animation guarded by the existing
  `@media (prefers-reduced-motion: reduce)` block's convention.

## Verification

**Backend (pytest):**
- `serialize_messages` includes `id` on tool_calls and `tool_call_id` + `is_error` on
  tool-result entries.
- `SubagentRunner.run` returns `failed=True` on timeout and on exception, `False` on
  success (and on empty output).
- `delegate_task` sets `status="error"` on the result when the sub-agent failed, and
  `status="success"` on the happy path; usage is still attributed on success.

**Frontend (project norm — no test runner):**
- `tsc --noEmit` passes with the new types.
- esbuild SSR smoke-render of a `<Transcript>` fed a fixture transcript containing
  `delegate_task` cards in running, done, and failed states renders without error and
  shows the three pills, one folded report, and no duplicated standalone result block.

## Out of scope (YAGNI)

- Generalizing the card to non-delegate tools (the id plumbing is generic, but only
  `delegate_task` gets a card).
- The "auto-inline the report when only one sub-agent" variant (report is always
  collapsed by default).
- Any nested-delegation UI — children do not get `delegate_task`.

## Files touched

- `src/atom/workflow/run_store.py`
- `src/atom/subagent.py`
- `src/atom/tools/subagent.py`
- `tests/test_subagent.py` (+ a serialize test, e.g. in `tests/` for `run_store`)
- `atom-ui/src/api.ts`
- `atom-ui/src/RunView.tsx`
- `atom-ui/src/styles.css`
