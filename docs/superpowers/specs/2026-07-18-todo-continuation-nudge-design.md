# TodoContinuationMiddleware — keep the lead agent on-track to finish its todos

**Date:** 2026-07-18
**Status:** Design — awaiting review
**Scope:** Lead agent only. Adds a bounded continuation-nudge + a per-turn todos reset, plus the behavioral test coverage the todos subsystem currently lacks.

## Problem

atom wires LangChain v1's stock `TodoListMiddleware()` with default args (`src/atom/agent.py:304`). That middleware only (a) injects a system-prompt blurb, (b) exposes `write_todos`, and (c) rejects *parallel* `write_todos` calls in `after_model`. Two consequences, both confirmed by an adversarial code review:

1. **No completion enforcement.** When the model ends a turn with a plain-text (no-tool-call) `AIMessage` while todos are still `pending`/`in_progress`, the run simply ends. Nothing re-prompts, loops back, or blocks. Continuation is *prompted* (`lead_system.md:21,39`; stock prompt `todo.py:117,136`) but never *enforced*. No atom middleware reads `state["todos"]`; the only `jump_to` sites in the tree both jump to `"end"`, never `"model"`.
2. **Stale todos bleed across turns.** `todos` is a plain `LastValue` channel persisted per `thread_id`, and nothing ever resets it. On a multi-turn thread (`atom chat`, `cli.py:147`), turn 2 inherits turn 1's plan; compaction even carries it forward (`summary.md:7-8`). A leftover incomplete todo would make even a trivial follow-up turn mis-fire a nudge.

Additionally, the entire todos subsystem has **zero behavioral test coverage** — the only references in `tests/` are a type-name presence assert and `"write_todos"` used as a prompt fixture string.

## Non-goals (explicit)

- **Surfacing todos to the CLI/API/UI** as structured plan state (review gap #5). Separate concern; not needed to keep the agent on-track.
- **Fixing the `in_progress` prompt contradiction** (`lead_system.md:21` "exactly one" vs. stock `todo.py:85` "multiple allowed", gap #6). One-line prompt follow-up, tracked separately.
- **Giving subagents todos** (gap #8). Intentional current design.
- **A defensive reducer on the `todos` channel** (gap #7). Latent-only; no live race while the stock parallel-guard stands. Optional follow-up.

## Design

### New unit: `src/atom/middleware/todo_continuation.py`

A single `TodoContinuationMiddleware(AgentMiddleware)` with its own minimal `state_schema` (following atom's convention that middleware-owned channels live in the middleware, `state.py:4-6`). Sync-only hooks, matching the house style of `clarification.py` / `loop_detection.py` / `instruction_pin.py` (atom runs async via `astream`, where sync hooks are invoked correctly — proven by those existing middlewares).

**State channel** (owned by this middleware):

```python
class _NudgeState(AgentState):
    # Continuation-nudge bookkeeping for the current turn. Reset each turn by before_agent.
    todo_nudge: NotRequired[dict]   # {"count": int, "completed": int}
```

**`before_agent(state, runtime)` — per-turn reset.** Runs once per invocation (per user turn), before the model loop. Returns:

```python
{"todos": [], "todo_nudge": {"count": 0, "completed": 0}}
```

Writing `{"todos": []}` is legal: the channel is contributed by `TodoListMiddleware` (always present, `agent.py:304`), and any node may write an existing channel. On turn 1 this is a harmless no-op (todos already empty); on resumed turns it clears the prior turn's plan so the nudge reasons only about the current turn. This is the fix for problem #2.

> **Open decision (chosen default, revisitable):** this is the "hard reset each turn" behavior. It was recommended but not explicitly confirmed. The alternative — keep todos across turns — was rejected because a leftover incomplete todo would mis-fire a nudge on an unrelated follow-up turn, and todos aren't tagged by turn. If cross-turn plan continuity is wanted, we'd instead scope the nudge to this-turn todos (more state, more complexity). Flip this before implementation if desired.

**`after_model(state, runtime)` — bounded nudge.** Decorated `@hook_config(can_jump_to=["model"])`. Logic:

1. `messages = state["messages"]`; if empty → `None`.
2. `last = messages[-1]`; if not an `AIMessage` or it has `tool_calls` → `None` (not a stopping turn).
3. `todos = state.get("todos") or []`; if empty → `None` (no plan; trivial task terminates normally).
4. `incomplete = [t for t in todos if t.get("status") != "completed"]`; if empty → `None` (all done; clean finish).
5. Budget check with progress-aware reset:
   - `cur = state.get("todo_nudge") or {"count": 0, "completed": 0}`
   - `completed_now = len(todos) - len(incomplete)`
   - `progress = completed_now > cur["completed"]`
   - `new_count = 1 if progress else cur["count"] + 1`
   - if `new_count > max_nudges` → `None` (budget exhausted for this no-progress streak; let the turn end).
6. Otherwise build the nudge and continue:
   ```python
   {
       "messages": [HumanMessage(content=<nudge text>,
                                 additional_kwargs={"lc_source": "todo_continuation"})],
       "jump_to": "model",
       "todo_nudge": {"count": new_count, "completed": completed_now},
   }
   ```

**Nudge text** (bracketed prefix constant, mirroring `compaction.py:_PIN_PREFIX`). Lists the open items so the plan is re-surfaced at the exact moment it matters (also partially mitigates gap #5 in-context):

```
[Automated planning check] You ended your turn, but these todo items are still open:
- (in_progress) <content>
- (pending) <content>
If the task isn't finished, keep going — start the next step now. If it IS finished,
call write_todos to mark these items completed (or remove ones no longer needed), then
write your final answer. Don't stop with open todos unless you're blocked — if you are,
say what's blocking you.
```

Message role is `HumanMessage`: the last `AIMessage` has no tool calls, so there is no `tool_call_id` to answer with a `ToolMessage`; `HumanMessage` with an `lc_source` tag matches the codebase's existing injected-message pattern (`compaction.py:67-70`). `InstructionPinMiddleware` is write-once and already captured turn 1's first human message, so the nudge is never mistaken for the pinned instruction. The nudge is not an `AIMessage`, so `runtime.py`'s final-text extraction (`runtime.py:228-234`) never returns it as `final_text`.

### Termination guarantee

`LoopDetectionMiddleware` counts only repeated identical *tool-call* signatures (`loop_detection.py:12-17,34`) and `continue`s past no-tool-call `AIMessage`s — so a nudge loop (all no-tool-call turns) evades it entirely, and the only other backstop is `recursion_limit`, which raises a hard `GraphRecursionError`. The `max_nudges` counter is therefore the real bound:

- The counter can only increment on **consecutive no-tool-call stalls** (turns with tool calls don't reach step 5). Between stalls, real work (tool calls) that completes a todo raises `completed_now`, tripping `progress` and resetting `new_count` to 1 — so a busy, progressing agent is never prematurely capped.
- A genuinely stuck agent (repeated no-progress stalls) hits `new_count > max_nudges` and is allowed to stop. With `max_nudges = 2`, a false positive (finished-but-forgot-to-mark) costs ≤2 extra turns, and the nudge tells the model exactly how to resolve it in one.

Worked traces (with `max_nudges = 2`):
- *Happy path:* stall with 1 pending → nudge(count=1) → model does the work (tool calls, no nudge eval) → next stall all completed → step 4 returns `None` → clean end.
- *Stuck:* stall(count=1) → nudge → stall no progress(count=2) → nudge → stall no progress(count=3 > 2) → `None` → ends. Exactly 2 nudges.
- *Progress mid-streak:* stall(count=1) → nudge → stall no progress(count=2) → nudge → model completes 3 todos (tool calls) → stall with progress → `new_count` resets to 1 → nudge again. Fresh budget after real progress.

### Placement (load-bearing)

Register `TodoContinuationMiddleware` **immediately after** `TodoListMiddleware()` in `_build_middlewares` (`agent.py:304`) — i.e. *early* in the chain. `after_model` hooks unwind in reverse registration order, so an early registration runs *late* in the unwind, i.e. **after** `LoopDetectionMiddleware` (`agent.py:317`) and `ClarificationMiddleware` (`agent.py:318`). When either of those returns `jump_to="end"`, the graph routes straight to the exit node and our node is never reached. Therefore:

- On an `ask_clarification` turn, Clarification ends the turn first — the nudge never fires (correct: the agent is legitimately waiting on the user, todos may be incomplete).
- On a detected tool-loop, LoopDetection ends the turn first — the nudge never re-ignites it.

This exclusion is enforced by graph edge ordering, not a runtime guard the nudge must implement. It is asserted by an ordering test so a future reorder can't silently break it.

### Config

New section in `src/atom/config/schema.py`, wired into `AtomConfig` and defaulted so existing configs need no change:

```python
class TodosConfig(_Base):
    # When true, if the lead agent ends a turn with incomplete todos, nudge it to keep going
    # (up to max_nudges consecutive no-progress stalls) instead of stopping early.
    continuation_nudge: bool = True
    # Infinite-loop backstop: max consecutive no-progress nudges before the turn is allowed to end.
    max_nudges: int = 2
```

`build_lead_agent` / `_build_middlewares` insert the middleware only when `cfg.todos.continuation_nudge` is true, constructed as `TodoContinuationMiddleware(max_nudges=cfg.todos.max_nudges)`. Lead-only: it is **not** added to the subagent chain (`subagent.py`), matching `TodoListMiddleware`'s lead-only wiring.

## Testing (TDD)

New `tests/test_todo_continuation.py`, unit tests calling hooks directly with dict state and `runtime=None` (style of `tests/test_middleware.py`):

1. **fires when incomplete** — last msg `AIMessage(content="done!", tool_calls=[])`, one `in_progress` todo → returns `jump_to="model"`, appends one `HumanMessage`, `todo_nudge.count == 1`.
2. **no fire when all completed** — all todos `completed` → `None`.
3. **no fire when no todos** — `todos` empty/absent → `None` (trivial tasks still terminate).
4. **inert with tool calls** — last `AIMessage` has `tool_calls` → `None`.
5. **cap stops the loop** — starting from `todo_nudge={"count": max_nudges, "completed": 0}` with no progress → `None` (no infinite loop).
6. **progress resets budget** — `count == max_nudges` but `completed_now > stored completed` → nudges again with `count == 1`.
7. **per-turn reset** — `before_agent` on state with a stale todo list returns `{"todos": [], "todo_nudge": {...0}}`.
8. **nudge text lists open items** — message body contains the incomplete todo contents and statuses.
9. **ordering invariant** — in the built chain, `TodoContinuationMiddleware` is registered before `LoopDetectionMiddleware` and `ClarificationMiddleware` (guards the placement guarantee).
10. **disabled by config** — `continuation_nudge=False` → middleware absent from the chain.

Plus one integration test (fake model, mirroring `tests/test_agent_smoke.py`): a scripted model that emits `write_todos` (one item `in_progress`) then a no-tool-call answer, then on the nudged turn marks it `completed` and answers. Assert the run does not stop at the first answer and the final state has the todo `completed`.

## Files touched

- **new** `src/atom/middleware/todo_continuation.py` — the middleware + nudge state schema + text constant.
- `src/atom/config/schema.py` — `TodosConfig` + `AtomConfig.todos` field.
- `src/atom/agent.py` — construct + insert the middleware after `TodoListMiddleware()`, gated on config.
- **new** `tests/test_todo_continuation.py` — unit + integration tests.

## Risks / mitigations

- **False positive (finished but unmarked todo):** ≤`max_nudges` extra turns; nudge text gives a one-turn resolution. Mitigated further by `max_nudges` being small (2) and configurable.
- **Infinite loop:** structurally bounded by the counter (see Termination guarantee); `recursion_limit` remains as a final hard backstop.
- **Writing `todos` from a foreign middleware:** safe only while `TodoListMiddleware` is present; it is always wired (`agent.py:304`) and both are lead-only, so they are coupled by construction. The ordering test plus the always-on comment document the dependency.
