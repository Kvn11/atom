# Cancel running workflows + text wrapping — design

Date: 2026-07-18
Status: approved (ready for implementation plan)

## Summary

Two independent features for the atom workflow platform:

1. **Cancel a workflow run** — a user can stop a queued or running run. Cancellation is
   *graceful* (the in-flight agent step finishes; nothing new starts) and *durable* (survives a
   server restart and a queue-pickup race). Cancelled runs reach a new terminal status
   `cancelled`, distinct from `halted` (which continues to mean "failed").

2. **Text wrapping in the UI** — long lines currently overflow their container horizontally.
   Prose (messages, thinking) must wrap; code and tool-output blocks keep their formatting on an
   inner horizontal scrollbar instead of pushing the page sideways.

The two features share no code and can be implemented and reviewed independently, but ship
together.

## Background: how runs execute today

(Established by reading `engine.py`, `run_store.py`, `status.py`, `lease.py`, `api/app.py`,
`runtime.py`.)

- **One directory per run** under `$ATOM_HOME/workflows/runs/<run_id>/`: `run.json` (authoritative
  `RunManifest`), `summary.json` (cheap cache for list scans), plus `workspace/`, `uploads/`,
  `chats/`, `artifacts/`, `exports/`. `RunStore.save()` writes each file via tmp + `os.replace`
  (atomic). `run_id` = `uuid4().hex[:12]`; `_is_safe_run_id` / `run_dir()` are the single path
  chokepoint.
- **Statuses.** Run: `pending | queued | running | complete | halted`. Step:
  `pending | running | complete | failed`. Task: `pending | running | succeeded | failed`.
  Terminal run statuses today are `complete` and `halted`. `compute_run_status` /
  `compute_step_status` (`status.py`) *derive* run/step status from children; they never produce
  `halted` from cancellation — `halted` comes from a failed step or an uncaught error.
- **Worker model.** The "worker" is an asyncio background task (`run_worker`) created by
  `start_worker()`, wired into the FastAPI `lifespan`: the process that wins the `WorkerLease`
  flock (`workflows/queue/worker.lock`) calls `recover()` then `start_worker()`, and on shutdown
  `stop_worker()` then releases the lease. **The API process that holds the lease is the durable
  worker** — so an API request handler and the worker run on the *same event loop, in the same
  process*. There is no separate worker process/thread.
- **Execution.** `run_worker` scans `queued_run_ids()` (FIFO by `enqueued_at`), and for each
  runnable run spawns `_drain_one` → `execute(run_id)`. `execute()` iterates steps sequentially;
  within a step, tasks run concurrently under `asyncio.gather(..., return_exceptions=True)`,
  bounded by `Semaphore(workflow.max_parallel)`. Each task calls `run_agent(...)`.
- **`run_agent` (`runtime.py`).** When `on_event` is set and `cfg.streaming.enabled`, it drives
  the agent with `async for item in agent.astream(inp, ..., stream_mode=["messages","updates"])`
  and, after the loop, reads the authoritative final state via `agent.aget_state(run_config)`.
  Otherwise it uses atomic `agent.ainvoke(...)`. On any `Exception` it recovers the checkpointer's
  partial transcript via `aget_state` and hands it to the `on_transcript` hook before re-raising.
- **CancelledError is already used — for shutdown only.** `stop_worker()` cancels the loop and all
  in-flight drain tasks; `execute()`'s `except asyncio.CancelledError` **requeues** the run
  (`status = "queued"`) so shutdown is lossless; `_run_task`'s `except asyncio.CancelledError`
  marks the task `failed` with `error="cancelled"`. This path is **left untouched** by this
  feature — user-cancel uses a *separate* signal so the two never conflict.

## Feature 1 — Cancel a run

### 1.1 New terminal status `cancelled`

Add `cancelled` as a terminal run status alongside `complete` and `halted`. It is set
**explicitly** by the cancel path — never derived — so `status.py` is unchanged.

Thread `cancelled` through every place that treats a run as terminal or active, so a cancelled run
is never rescanned, recovered, re-queued, or counted as active:

- `run_store.py`
  - `_ACTIVE` already excludes `cancelled` (it lists only `pending, queued, running`) — no change,
    but confirm cancelled is not treated as active.
  - `list_summaries`: add a `cancelled` bucket to `counts` (currently `{active, complete, halted}`).
    The `status` filter path already works for any exact status string, so `?status=cancelled`
    filters correctly with no code change beyond the count.
- `engine.py` terminal-check tuples — add `"cancelled"` to each:
  - `enqueue` (`m.status in ("complete", "halted")` guard)
  - `recover` (skip `("complete", "halted")`)
  - `await_run` (both `("complete", "halted")` checks)
  - `_drain_one` (`store.load(run_id).status in ("complete", "halted")` guard)
- `api/app.py`
  - `self_improve` keeps its `("complete", "halted")` guard **unchanged** — a cancelled run is a
    user abort with partial data and is intentionally *not* self-improvable.

### 1.2 Signal: durable marker file + in-process set

The cancel intent lives in two places kept in sync:

- **Durable marker file** — `runs/<id>/cancel.request` is the source of truth. New `RunStore`
  helpers:
  - `cancel_marker_path(run_id) -> Path` (guarded by `_is_safe_run_id` via `run_dir`)
  - `write_cancel_marker(run_id, when: str) -> None` — writes `{"requested_at": when}` (small JSON)
  - `cancel_requested(run_id) -> bool` — marker exists (returns `False` for unsafe id)
  - `clear_cancel_marker(run_id) -> None` — `unlink(missing_ok=True)`
- **In-process set** — `WorkflowEngine._cancel_requested: set[str]`, a fast path so the hot loop
  need not stat the disk on every check.

Engine helper: `_is_cancel_requested(run_id) -> bool` returns
`run_id in self._cancel_requested or self.store.cancel_requested(run_id)`. The marker covers
restart / cross-process / pickup-race; the set covers the common same-process case.

### 1.3 Requesting a cancel: `engine.request_cancel`

`request_cancel(run_id) -> dict` is a **synchronous method with no `await`**. Because the worker
and the API handler share one event loop, a sync method runs atomically relative to the worker
coroutines — there is no interleaving, so the queued→cancelled flip is race-free in the
single-process deployment.

Logic:

1. `m = self.store.load(run_id)` (raises `FileNotFoundError` → API 404).
2. If `m.status == "cancelled"`: return `{"run_id", "status": "cancelled", "already": True}`
   (idempotent).
3. If `m.status in ("complete", "halted")`: return `{"run_id", "status": m.status,
   "already": True}` — API translates to 409.
4. Otherwise record intent: `self.store.write_cancel_marker(run_id, _now_micros())` and
   `self._cancel_requested.add(run_id)`.
5. If `m.status in ("queued", "pending")` (nothing executing): finalize immediately via
   `_finalize_cancelled(m)` and return `{"run_id", "status": "cancelled"}`.
6. If `m.status == "running"`: leave the executing loop to finalize at its next boundary; return
   `{"run_id", "status": "running", "cancel_requested": True}`.

`_finalize_cancelled(m) -> None` (shared helper): `m.status = "cancelled"`; `m.ended_at = _now()`;
`self.store.save(m)`; `self.store.clear_cancel_marker(m.run_id)`;
`self._cancel_requested.discard(m.run_id)`.

### 1.4 Mid-task graceful interruption

`run_agent` gains `should_cancel: Callable[[], bool] | None = None`, and `RunResult` gains
`cancelled: bool = False`.

In the streaming (`astream`) path, check the flag at **agent-step (node) boundaries only** — i.e.
when the yielded item is an `updates` frame — so the in-flight model/tool call completes cleanly
(no aborted mid-generation HTTP), then the loop breaks before the next step:

```python
cancelled = False
async for item in agent.astream(inp, config=run_config, context=context,
                                stream_mode=["messages", "updates"]):
    mode, data = item if isinstance(item, tuple) else (item.get("type"), item.get("data"))
    if mode == "messages":
        ...  # unchanged: translate + emit token/tool deltas
    elif mode == "updates":
        ...  # unchanged: translate + emit
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
result = (await agent.aget_state(run_config)).values   # partial state after break
```

After the loop, `aget_state` returns the last committed checkpoint (consistent — the aborted
super-step never committed). Return `RunResult(..., cancelled=cancelled)`.

- **Non-streaming fallback.** The `ainvoke` path is atomic and has no loop to check, so with
  streaming disabled cancellation degrades to *task-boundary* granularity (the current task
  finishes, then `execute()`'s boundary checks stop the run). Streaming is on by default and the
  engine always sets `on_event` when `cfg.streaming.enabled`, so the default deployment gets
  mid-task cancellation. This limitation is documented, not fixed, in v1.

### 1.5 Engine enforcement in `execute()` / `_run_task`

`_run_task` passes `should_cancel=lambda: self._is_cancel_requested(manifest.run_id)` to
`run_agent`. After `result = await run_agent(...)`:

- Persist the transcript as today (`save_chat(serialize_messages(result.messages))`).
- If `getattr(result, "cancelled", False)`: set `ts.status = "failed"`, `ts.error = "cancelled"`,
  `ts.ended_at = _now()`, and **skip** artifact capture / the `succeeded` path. (Same task-record
  shape the existing shutdown-cancel path produces.)
- Otherwise the existing success path (artifacts, `succeeded`).

`execute()` gains cooperative checks that call `_finalize_cancelled(manifest)` and `return` when
`_is_cancel_requested(run_id)` is true, at three points:

- **A** — top of the step loop, before starting a not-yet-complete step (don't start a new step).
- **B** — inside `run_one`, after acquiring the semaphore and before the task begins (skip tasks
  still queued behind `max_parallel`; already-running tasks finish via 1.4). A skipped task is left
  `pending`.
- **C** — after a step's `gather` completes and step status is computed, before advancing (don't
  proceed to the next step; finalize now).

Tie-break: at check **C**, cancel takes precedence over a step that failed in the same window — the
run is reported `cancelled`, not `halted` (the user explicitly asked to stop).

The existing `except asyncio.CancelledError → requeue` branch is unchanged and remains
shutdown-only.

### 1.6 Crash-safety in `recover()` and `_drain_one`

- `recover()`: for each interrupted run, after the terminal-status skip, if
  `self.store.cancel_requested(run_id)` → `_finalize_cancelled(m)` and continue (do **not**
  re-queue). So a cancel requested just before a crash still ends `cancelled` on restart.
- `_drain_one`: before `execute()`, in addition to the terminal-status skip, if
  `self.store.cancel_requested(run_id)` → load, `_finalize_cancelled`, and return (covers a
  cross-process / queued-with-marker run reaching the drainer).

### 1.7 API

- **`POST /api/runs/{run_id}/cancel`** — `async def`; calls `engine.request_cancel(run_id)` inside
  a `try` that maps `FileNotFoundError` → 404. If the result carries `already` and
  `status in ("complete", "halted")` → raise 409 `"run already finished; nothing to cancel"`.
  Otherwise return the dict (200). Response shapes:
  - queued/pending cancelled → `{"run_id", "status": "cancelled"}`
  - running → `{"run_id", "status": "running", "cancel_requested": true}`
  - already cancelled → `{"run_id", "status": "cancelled", "already": true}` (200, idempotent)
- **`GET /api/runs/{run_id}`** — augment the returned dict with a computed
  `"cancel_requested": store.cancel_requested(run_id)` (do **not** add it to the persisted
  `RunManifest`; the manifest is held in memory by `execute()` and frequent saves would clobber a
  field written externally — the marker file avoids that write race).

`run_id` confinement is inherited: every call routes through `store.load` / `run_dir` which reject
unsafe ids.

### 1.8 UI

- `api.ts`
  - `cancel(id): Promise<{run_id: string; status: string; cancel_requested?: boolean}>` →
    `POST /api/runs/{id}/cancel`, surfacing `{detail}` on error (same pattern as `selfImprove`).
  - `Manifest` gains `cancel_requested?: boolean`.
  - `RunsPage.counts` gains `cancelled: number`.
- `ui.tsx` — `STATUS_CLASS.cancelled = "idle"` (muted gray pill, distinct from red `halted`).
- `RunView.tsx`
  - In `.run-status`, add a **Cancel run** button shown only when
    `status in ("pending", "queued", "running")`. On click, `window.confirm(...)` then
    `api.cancel(runId)`; disable while the request is in flight; surface errors in the existing
    banner pattern.
  - While `manifest.cancel_requested && status === "running"`: render the pill as **"Cancelling…"**
    (warn) and hide the Cancel button (already requested).
  - Add `"cancelled"` to the polling stop condition (currently
    `m.status === "complete" || m.status === "halted"`).
- `RunsDashboard.tsx` — add `"cancelled"` to `FILTERS` and to the `counts` chip indexing.

### 1.9 Explicitly out of scope (v1)

- **CLI `atom cancel`.** With the durable marker, a CLI could write the marker and let the drainer
  finalize; but finalizing a *queued* run from a non-drainer process has a cross-process TOCTOU
  window against the API worker. Deferred to keep v1 race-free. Documented follow-up.
- **Per-row cancel in the dashboard table.** Cancel lives in the run detail view for v1.

## Feature 2 — Text wrapping

Choice: **wrap prose, scroll code/tool-output.** All changes in `atom-ui/src/styles.css`.

- `.msg-text` (plain messages, thinking `.think`) — add `overflow-wrap: anywhere` so long
  unbreakable tokens (URLs, paths, base64) wrap. `white-space: pre-wrap` already handles normal
  wrapping.
- `.msg-text.md` (markdown prose) — add `overflow-wrap: anywhere`. Harmless to descendant
  `.md pre` / `.md code`, whose own `white-space` governs them.
- `.msg.tool .msg-text` (tool output) — override to `white-space: pre; overflow-x: auto` so command
  output keeps its formatting on an inner horizontal scrollbar rather than overflowing the page.
- **No change** to code/table blocks that already scroll: `.md pre`, `.art-code-body`,
  `.art-code-body pre`, `.md table` all already carry `overflow: auto` / `overflow-x: auto`.
- Defensive `overflow-wrap: anywhere` on fixed-width rail labels `.rail-task-id` and `.art-name`.

## Testing

- **`tests/test_workflow_cancel.py`** (new, engine-level, using the existing fake/prepared-model
  fixtures):
  - Cancel a **queued** run → status `cancelled`, marker cleared, never drained.
  - Cancel a **running** run → the in-flight task stops at a step boundary (via a fake
    `should_cancel`/agent that yields multiple `updates`), later steps never run, run ends
    `cancelled`, interrupted task is `failed`/`error="cancelled"` with a persisted partial
    transcript.
  - **Durable**: write a marker + leave a run `running`/`pending`, call `recover()` → run
    finalized `cancelled`, not re-queued. Same for `_drain_one` reaching a marked run.
  - **Idempotency / already-terminal**: cancel an already-`cancelled` run (idempotent);
    cancel a `complete`/`halted` run (reports already-finished).
  - **Counts**: cancelled runs are excluded from `active` and counted under `cancelled` in
    `list_summaries`.
- **`tests/test_workflow_api.py`** (additions): `POST /cancel` → 200 (queued & running shapes),
  404 (unknown/unsafe id), 409 (finished run); `GET /runs/{id}` includes `cancel_requested`.
- **UI** has no JS test harness → verified by building `atom-ui` and driving the cancel + wrapping
  flows in the running app.

## Risks / notes

- Breaking out of `agent.astream(...)` triggers the async generator's `aclose()`, which cancels the
  in-flight (uncommitted) super-step; `aget_state` then returns the last committed checkpoint. This
  early-break-then-read pattern must be verified against the installed LangGraph in a test.
- `request_cancel` must stay `await`-free to preserve its atomicity relative to the worker; any
  future `await` inside it reintroduces an interleaving race and must be reconsidered.
- The marker file is removed on finalize; `GET`'s `cancel_requested` is therefore true only during
  the transient `running` → `cancelled` window.
