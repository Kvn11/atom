# Cancel Running Workflows + Text Wrapping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user cancel a queued or running workflow run (graceful, durable) and make long lines wrap in the run UI instead of overflowing.

**Architecture:** Cancellation adds a new terminal run status `cancelled`. Intent is recorded both durably (a `cancel.request` marker file per run) and in-process (a `set` on the engine). A running task stops at its next agent-step boundary via a `should_cancel` callback threaded into `run_agent`'s `astream` loop; the engine's `execute()` then finalizes the run `cancelled` at task/step boundaries. Crash recovery honors the marker. A `POST /api/runs/{id}/cancel` endpoint drives it. Text wrapping is a surgical CSS change.

**Tech Stack:** Python 3, FastAPI, LangChain/LangGraph, pytest + pytest-asyncio, httpx `AsyncClient`; React + TypeScript (Vite) for the UI.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-18-cancel-runs-and-text-wrap-design.md` (authoritative).
- New terminal run status string is exactly `"cancelled"`.
- `WorkflowEngine.request_cancel` MUST stay synchronous and `await`-free (atomicity vs. the worker coroutines on the shared event loop).
- The cancel marker file is `runs/<run_id>/cancel.request`; it is the durable source of truth and is removed on finalize.
- `cancelled` is set **explicitly**, never derived — do NOT change `status.py`.
- Interrupted task record on cancel: task `status="failed"`, `error="cancelled"` (matches the existing shutdown-cancel shape).
- Cancel takes precedence over a simultaneous step failure at a boundary → report `cancelled`, not `halted`.
- The existing `except asyncio.CancelledError` requeue path in `execute()` is shutdown-only and MUST be left unchanged.
- Python tests run with `pytest`; every async test is decorated `@pytest.mark.asyncio`. UI verified with `cd atom-ui && npm run build` (runs `tsc && vite build`).
- Commit messages end with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`

---

## File Structure

- `src/atom/workflow/run_store.py` — add cancel-marker helpers; add `cancelled` to `list_summaries` counts.
- `src/atom/runtime.py` — `run_agent(should_cancel=…)`; `RunResult.cancelled`.
- `src/atom/workflow/engine.py` — `_cancel_requested` set; `_is_cancel_requested`, `_finalize_cancelled`, `request_cancel`; `execute()` boundary checks; `_run_task` cancelled handling; `recover()` + `_drain_one` marker guards; `cancelled` in terminal tuples.
- `src/atom/api/app.py` — `POST /api/runs/{id}/cancel`; `cancel_requested` in `GET /api/runs/{id}`.
- `tests/test_workflow_run_store.py` — marker helpers + counts (append tests).
- `tests/test_runtime_streaming.py` — `should_cancel` behavior (append tests).
- `tests/test_workflow_cancel.py` — **new**; engine-level cancellation.
- `tests/test_workflow_api.py` — cancel endpoint + GET field (append tests).
- `atom-ui/src/api.ts` — `cancel()` client; `Manifest.cancel_requested`; `RunsPage.counts.cancelled`.
- `atom-ui/src/ui.tsx` — `STATUS_CLASS.cancelled`.
- `atom-ui/src/RunView.tsx` — Cancel button + "Cancelling…" state + polling stop condition.
- `atom-ui/src/RunsDashboard.tsx` — `cancelled` filter + count chip.
- `atom-ui/src/styles.css` — wrap prose / scroll code.

---

## Task 1: RunStore cancel-marker helpers + `cancelled` in counts

**Files:**
- Modify: `src/atom/workflow/run_store.py`
- Test: `tests/test_workflow_run_store.py`

**Interfaces:**
- Produces:
  - `RunStore.cancel_marker_path(run_id: str) -> Path`
  - `RunStore.write_cancel_marker(run_id: str, when: str) -> None`
  - `RunStore.cancel_requested(run_id: str) -> bool`
  - `RunStore.clear_cancel_marker(run_id: str) -> None`
  - `RunStore.list_summaries(...)` `counts` dict now has key `"cancelled"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_run_store.py` (add `from atom.workflow.run_store import RunStore, RunManifest` to imports if not present):

```python
def test_cancel_marker_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    store.create(RunManifest(
        run_id="r1", workflow="wf", created_at="2026-07-18T00:00:00",
        workspace_path=str(store.workspace_dir("r1")), steps=[]))
    assert store.cancel_requested("r1") is False
    store.write_cancel_marker("r1", "2026-07-18T00:00:00.000000")
    assert store.cancel_requested("r1") is True
    store.clear_cancel_marker("r1")
    assert store.cancel_requested("r1") is False


def test_cancel_requested_false_for_unsafe_id(atom_home):
    store = RunStore(str(atom_home))
    assert store.cancel_requested("../evil") is False
    store.clear_cancel_marker("../evil")   # no-op, must not raise


def test_list_summaries_counts_cancelled(atom_home):
    store = RunStore(str(atom_home))
    store.create(RunManifest(
        run_id="c1", workflow="wf", status="cancelled", created_at="2026-07-18T00:00:00",
        workspace_path=str(store.workspace_dir("c1")), steps=[]))
    page = store.list_summaries(status="all")
    assert page["counts"]["cancelled"] == 1
    assert page["counts"]["active"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workflow_run_store.py -k "cancel or counts_cancelled" -v`
Expected: FAIL — `AttributeError: 'RunStore' object has no attribute 'cancel_requested'` (and a `KeyError: 'cancelled'` for the counts test).

- [ ] **Step 3: Add the marker helpers**

In `src/atom/workflow/run_store.py`, add these methods to `RunStore` (place them right after `_manifest_path` / before `create`):

```python
    def cancel_marker_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "cancel.request"

    def write_cancel_marker(self, run_id: str, when: str) -> None:
        p = self.cancel_marker_path(run_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"requested_at": when}), encoding="utf-8")

    def cancel_requested(self, run_id: str) -> bool:
        if not _is_safe_run_id(run_id):
            return False
        return self.cancel_marker_path(run_id).exists()

    def clear_cancel_marker(self, run_id: str) -> None:
        if not _is_safe_run_id(run_id):
            return
        self.cancel_marker_path(run_id).unlink(missing_ok=True)
```

- [ ] **Step 4: Add `cancelled` to `list_summaries` counts**

In `src/atom/workflow/run_store.py`, `list_summaries`, change the two count literals:

```python
        empty = {"items": [], "total": 0, "counts": {"active": 0, "complete": 0, "halted": 0, "cancelled": 0}}
```
```python
        counts = {"active": 0, "complete": 0, "halted": 0, "cancelled": 0}
```

(The existing `elif s.status in counts:` line then increments `cancelled` automatically.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_workflow_run_store.py -k "cancel or counts_cancelled" -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the full run-store suite (no regressions)**

Run: `pytest tests/test_workflow_run_store.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(run-store): cancel-marker helpers + cancelled status count

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `run_agent` `should_cancel` + `RunResult.cancelled`

**Files:**
- Modify: `src/atom/runtime.py`
- Test: `tests/test_runtime_streaming.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `RunResult.cancelled: bool` (default `False`).
  - `run_agent(..., should_cancel: Callable[[], bool] | None = None)` — in the streaming path, when `should_cancel()` is true at an agent-step (`updates`) boundary, it stops and returns `RunResult(cancelled=True)` with the partial transcript. Non-streaming (`ainvoke`) path ignores `should_cancel`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_runtime_streaming.py`:

```python
@pytest.mark.asyncio
async def test_should_cancel_stops_stream_and_flags_result(base_config):
    async def on_event(e):
        pass
    prepared = make_streaming_prepared("alpha beta gamma")
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             on_event=on_event, should_cancel=lambda: True)
    assert result.cancelled is True


@pytest.mark.asyncio
async def test_should_cancel_false_completes_normally(base_config):
    async def on_event(e):
        pass
    prepared = make_streaming_prepared("alpha beta gamma")
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             on_event=on_event, should_cancel=lambda: False)
    assert result.cancelled is False
    assert result.final_text.strip() == "alpha beta gamma"


@pytest.mark.asyncio
async def test_should_cancel_ignored_when_streaming_disabled(base_config):
    base_config.streaming.enabled = False
    prepared = make_prepared([AIMessage(content="plain")])
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             on_event=lambda e: None, should_cancel=lambda: True)
    assert result.cancelled is False   # ainvoke path has no cooperative check
    assert result.final_text == "plain"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_streaming.py -k should_cancel -v`
Expected: FAIL — `TypeError: run_agent() got an unexpected keyword argument 'should_cancel'`.

- [ ] **Step 3: Add the `cancelled` field to `RunResult`**

In `src/atom/runtime.py`, in the `RunResult` dataclass, add the field after `awaiting_clarification`:

```python
@dataclass
class RunResult:
    thread_id: str
    messages: list[BaseMessage]
    final_text: str
    state: dict[str, Any] = field(default_factory=dict)
    awaiting_clarification: bool = False
    cancelled: bool = False
```

- [ ] **Step 4: Add the `should_cancel` parameter to `run_agent`**

In `src/atom/runtime.py`, add the parameter to the `run_agent` signature, right after `on_transcript`:

```python
    on_transcript: "Callable[[list], None] | None" = None,
    should_cancel: "Callable[[], bool] | None" = None,
) -> RunResult:
```

- [ ] **Step 5: Initialize the flag and check it in the stream loop**

In `src/atom/runtime.py`, add `cancelled = False` immediately before the `async with open_checkpointer(...)` line:

```python
    db_path = Path(home) / "atom.sqlite"

    cancelled = False
    async with open_checkpointer(cfg.checkpointer.backend, db_path) as cp:
```

Then, inside the streaming `async for` loop, add the cancel check at the end of the `elif mode == "updates":` branch (after the inner `for ... translate_update ...` loop):

```python
                    elif mode == "updates":
                        for _node, update in (data or {}).items():
                            msgs = update.get("messages") if isinstance(update, dict) else None
                            for ev in translate_update(msgs or []):
                                await on_event(ev)
                        if should_cancel is not None and should_cancel():
                            cancelled = True
                            break
```

- [ ] **Step 6: Thread `cancelled` into both return sites**

In `src/atom/runtime.py`, update the clarification-path return:

```python
        return RunResult(
            thread_id=thread_id, messages=messages, final_text=final_text,
            state=result, awaiting_clarification=True, cancelled=cancelled,
        )
```

and the final return:

```python
    return RunResult(thread_id=thread_id, messages=messages, final_text=final_text,
                     state=result, cancelled=cancelled)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_runtime_streaming.py -k should_cancel -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Run the full streaming + runtime suites (no regressions)**

Run: `pytest tests/test_runtime_streaming.py tests/test_streaming.py tests/test_runtime_trace.py tests/test_runtime_context.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/atom/runtime.py tests/test_runtime_streaming.py
git commit -m "feat(runtime): cooperative should_cancel in run_agent streaming loop

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Engine `request_cancel` + finalize + terminal guards

**Files:**
- Modify: `src/atom/workflow/engine.py`
- Test: `tests/test_workflow_cancel.py` (new)

**Interfaces:**
- Consumes: `RunStore.write_cancel_marker/cancel_requested/clear_cancel_marker` (Task 1).
- Produces:
  - `WorkflowEngine._cancel_requested: set[str]`
  - `WorkflowEngine._is_cancel_requested(run_id: str) -> bool`
  - `WorkflowEngine._finalize_cancelled(manifest: RunManifest) -> None`
  - `WorkflowEngine.request_cancel(run_id: str) -> dict` — returns
    `{"run_id", "status", ...}`; `status` is `"cancelled"` (queued/pending finalized now),
    `"running"` (+ `"cancel_requested": True`), or the existing terminal status (+ `"already": True`).

- [ ] **Step 1: Write the failing tests (new file)**

Create `tests/test_workflow_cancel.py`:

```python
"""User-initiated workflow cancellation: graceful, durable, terminal 'cancelled' status."""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

import atom.workflow.engine as engine_mod
from atom.runtime import RunResult
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef


def _two_step_wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo",
        "steps": [
            {"title": "One", "tasks": [{"id": "t1", "prompt": "do one"}]},
            {"title": "Two", "tasks": [{"id": "t2", "prompt": "do two"}]},
        ],
    })


def test_request_cancel_queued_run_marks_cancelled(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rq", "2026-07-18T00:00:00")
    engine.enqueue("rq")
    assert engine.store.load("rq").status == "queued"

    res = engine.request_cancel("rq")

    assert res["status"] == "cancelled"
    assert engine.store.load("rq").status == "cancelled"
    assert engine.store.cancel_requested("rq") is False       # marker cleared on finalize
    assert "rq" not in engine.store.queued_run_ids()          # dropped from the FIFO scan


def test_request_cancel_idempotent_on_cancelled(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rc", "2026-07-18T00:00:00")
    engine.request_cancel("rc")                                # pending -> cancelled
    res = engine.request_cancel("rc")
    assert res == {"run_id": "rc", "status": "cancelled", "already": True}


def test_request_cancel_finished_run_reports_already(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    m = engine.create_run(_two_step_wf(), {}, "rf", "2026-07-18T00:00:00")
    m.status = "complete"
    engine.store.save(m)
    res = engine.request_cancel("rf")
    assert res == {"run_id": "rf", "status": "complete", "already": True}
    assert engine.store.load("rf").status == "complete"


def test_request_cancel_unknown_run_raises(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    with pytest.raises(FileNotFoundError):
        engine.request_cancel("ghost")


def test_enqueue_will_not_reopen_a_cancelled_run(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rec", "2026-07-18T00:00:00")
    engine.request_cancel("rec")                               # -> cancelled
    engine.enqueue("rec")                                      # must be a no-op
    assert engine.store.load("rec").status == "cancelled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workflow_cancel.py -v`
Expected: FAIL — `AttributeError: 'WorkflowEngine' object has no attribute 'request_cancel'`.

- [ ] **Step 3: Add the in-process set in `__init__`**

In `src/atom/workflow/engine.py`, in `WorkflowEngine.__init__`, next to the other durable-queue worker state (after `self._inflight: set[str] = set()`):

```python
        self._inflight: set[str] = set()
        self._cancel_requested: set[str] = set()     # run_ids a user asked to cancel (in-process fast path)
```

- [ ] **Step 4: Add the helper methods**

In `src/atom/workflow/engine.py`, add these methods to `WorkflowEngine` (place them just before `async def execute`):

```python
    def _is_cancel_requested(self, run_id: str) -> bool:
        return run_id in self._cancel_requested or self.store.cancel_requested(run_id)

    def _finalize_cancelled(self, manifest: "RunManifest") -> None:
        """Terminalize a run as user-cancelled and clear its cancel signal."""
        manifest.status = "cancelled"
        manifest.ended_at = _now()
        self.store.save(manifest)
        self.store.clear_cancel_marker(manifest.run_id)
        self._cancel_requested.discard(manifest.run_id)

    def request_cancel(self, run_id: str) -> dict:
        """Request cancellation of a run. MUST stay synchronous + await-free so it runs
        atomically relative to the worker coroutines on the shared event loop.

        Queued/pending runs are finalized immediately; a running run is signalled (durable
        marker + in-process set) and finalized by execute() at its next agent-step boundary.
        """
        m = self.store.load(run_id)                       # FileNotFoundError -> API 404
        if m.status == "cancelled":
            return {"run_id": run_id, "status": "cancelled", "already": True}
        if m.status in ("complete", "halted"):
            return {"run_id": run_id, "status": m.status, "already": True}
        self.store.write_cancel_marker(run_id, _now_micros())
        self._cancel_requested.add(run_id)
        if m.status in ("queued", "pending"):
            self._finalize_cancelled(m)
            return {"run_id": run_id, "status": "cancelled"}
        return {"run_id": run_id, "status": "running", "cancel_requested": True}
```

- [ ] **Step 5: Add `cancelled` to the terminal-status guards**

In `src/atom/workflow/engine.py`, add `"cancelled"` to each of these tuples:

`enqueue`:
```python
        if m.status in ("complete", "halted", "cancelled"):
            return                       # never re-open a terminal run
```

`await_run` (both occurrences):
```python
            if m.status in ("complete", "halted", "cancelled"):
                return m
```
```python
                    if self.store.load(run_id).status not in ("complete", "halted", "cancelled"):
                        await self.execute(run_id)
```

`_drain_one`:
```python
            if self.store.load(run_id).status in ("complete", "halted", "cancelled"):
                return
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_workflow_cancel.py -v`
Expected: PASS (5 tests).

- [ ] **Step 7: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_cancel.py
git commit -m "feat(engine): request_cancel + finalize + cancelled terminal guards

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `execute()` boundary checks + `_run_task` honors `result.cancelled`

**Files:**
- Modify: `src/atom/workflow/engine.py`
- Test: `tests/test_workflow_cancel.py`

**Interfaces:**
- Consumes: `_is_cancel_requested`, `_finalize_cancelled` (Task 3); `run_agent(should_cancel=…)` and `RunResult.cancelled` (Task 2).
- Produces: `execute()` returns a `cancelled` manifest when cancellation is requested before/during a step; an interrupted task is recorded `failed` / `error="cancelled"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_cancel.py`:

```python
@pytest.mark.asyncio
async def test_cancel_requested_before_execute_finalizes_immediately(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rpre", "2026-07-18T00:00:00")
    engine._cancel_requested.add("rpre")
    engine.store.write_cancel_marker("rpre", "2026-07-18T00:00:00.000000")

    manifest = await engine.execute("rpre")

    assert manifest.status == "cancelled"
    assert manifest.steps[0].tasks[0].status == "pending"        # no task ran
    assert engine.store.load_chat("rpre", 0, "t1") is None
    assert engine.store.cancel_requested("rpre") is False         # marker cleared


@pytest.mark.asyncio
async def test_running_run_cancels_gracefully_after_current_task(base_config, atom_home, monkeypatch):
    engine = WorkflowEngine(base_config)

    calls = []

    async def spy(prompt, **kwargs):
        calls.append(prompt)
        sc = kwargs.get("should_cancel")
        engine._cancel_requested.add("rcg")          # cancel arrives WHILE this task runs
        return RunResult(
            thread_id=kwargs.get("thread_id", "t"),
            messages=[AIMessage(content="partial")],
            final_text="partial", state={},
            cancelled=bool(sc and sc()),
        )

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    engine.create_run(_two_step_wf(), {}, "rcg", "2026-07-18T00:00:00")

    manifest = await engine.execute("rcg")

    assert manifest.status == "cancelled"
    assert manifest.steps[0].tasks[0].status == "failed"
    assert manifest.steps[0].tasks[0].error == "cancelled"
    assert manifest.steps[1].tasks[0].status == "pending"        # step 2 never started
    assert len(calls) == 1                                        # only the step-1 task ran
    assert engine.store.cancel_requested("rcg") is False          # marker cleared
    # the partial transcript of the interrupted task was still persisted
    assert engine.store.load_chat("rcg", 0, "t1") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workflow_cancel.py -k "before_execute or gracefully" -v`
Expected: FAIL — the run finalizes `complete`/`halted` (no boundary checks yet), so the `cancelled` assertions fail.

- [ ] **Step 3: Pass `should_cancel` and honor `result.cancelled` in `_run_task`**

In `src/atom/workflow/engine.py`, `_run_task`, add `should_cancel` to the `run_agent(...)` call (alongside `on_transcript=_persist_partial`):

```python
                on_transcript=_persist_partial,
                should_cancel=lambda: self._is_cancel_requested(manifest.run_id),
            )
```

Then replace the success block that follows `result = await (...)`:

```python
            result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            self.store.save_chat(
                manifest.run_id, step_state.index, ts.id, serialize_messages(result.messages)
            )
            if getattr(result, "cancelled", False):
                ts.status = "failed"
                ts.error = "cancelled"
            else:
                presented = (result.state or {}).get("artifacts", [])
                ts.artifacts = self.store.capture_artifacts(
                    manifest.run_id, step_state.index, ts.id, presented,
                )
                ts.status = "succeeded"
```

- [ ] **Step 4: Add the three boundary checks in `execute()`**

In `src/atom/workflow/engine.py`, `execute()`, replace the step-loop body from `for step_state, step_def in zip(...)` through the `if step_state.status != "complete":` block with:

```python
            for step_state, step_def in zip(manifest.steps, workflow.steps):
                if step_state.status == "complete":
                    continue                       # resume: this step finished in a prior life
                if self._is_cancel_requested(run_id):       # CHECK A: don't start a new step
                    self._finalize_cancelled(manifest)
                    return manifest
                step_state.status = "running"
                self.store.save(manifest)

                async def run_one(ts: TaskState, td: TaskDef, sd: StepDef, ss: StepState):
                    async with sem:
                        if self._is_cancel_requested(run_id):   # CHECK B: don't start a queued task
                            return
                        await self._run_task(manifest, workflow, ss, sd, ts, td, notes=notes_binding)

                pending = [
                    (ts, td) for ts, td in zip(step_state.tasks, step_def.tasks)
                    if ts.status != "succeeded"    # resume: skip tasks already completed
                ]
                await asyncio.gather(*[
                    run_one(ts, td, step_def, step_state) for ts, td in pending
                ], return_exceptions=True)

                step_state.status = compute_step_status([t.status for t in step_state.tasks])
                self.store.save(manifest)
                if self._is_cancel_requested(run_id):       # CHECK C: don't advance to the next step
                    self._finalize_cancelled(manifest)
                    return manifest
                if step_state.status != "complete":
                    manifest.status = "halted"
                    manifest.ended_at = _now()
                    self.store.save(manifest)
                    return manifest
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_workflow_cancel.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Run the engine suite (no regressions to the existing CancelledError/shutdown path)**

Run: `pytest tests/test_workflow_engine.py tests/test_workflow_queue.py tests/test_workflow_lease.py -q`
Expected: PASS. (In particular `test_run_task_cancelled_leaves_clean_terminal_state` still passes — the asyncio-CancelledError requeue path is untouched.)

- [ ] **Step 7: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_cancel.py
git commit -m "feat(engine): graceful mid-run cancellation at task/step boundaries

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: `recover()` + `_drain_one` honor the cancel marker

**Files:**
- Modify: `src/atom/workflow/engine.py`
- Test: `tests/test_workflow_cancel.py`

**Interfaces:**
- Consumes: `RunStore.cancel_requested` (Task 1); `_finalize_cancelled` (Task 3).
- Produces: a run left non-terminal by a crash but carrying a cancel marker is finalized
  `cancelled` on `recover()` and on `_drain_one`, instead of being resumed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_cancel.py`:

```python
def test_recover_finalizes_marked_run_instead_of_requeue(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    m = engine.create_run(_two_step_wf(), {}, "rrec", "2026-07-18T00:00:00")
    m.status = "running"                      # simulate a crash mid-run
    m.steps[0].status = "running"
    m.steps[0].tasks[0].status = "running"
    engine.store.save(m)
    engine.store.write_cancel_marker("rrec", "2026-07-18T00:00:00.000000")

    engine.recover()

    assert engine.store.load("rrec").status == "cancelled"
    assert engine.store.cancel_requested("rrec") is False
    assert "rrec" not in engine.store.queued_run_ids()


@pytest.mark.asyncio
async def test_drain_one_finalizes_marked_run_without_executing(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_two_step_wf(), {}, "rdo", "2026-07-18T00:00:00")
    engine.enqueue("rdo")
    engine.store.write_cancel_marker("rdo", "2026-07-18T00:00:00.000000")

    sem = asyncio.Semaphore(1)
    await sem.acquire()                        # _drain_one releases it in its finally block
    await engine._drain_one("rdo", sem)

    assert engine.store.load("rdo").status == "cancelled"
    assert engine.store.load_chat("rdo", 0, "t1") is None   # never executed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workflow_cancel.py -k "recover_finalizes or drain_one_finalizes" -v`
Expected: FAIL — `recover()` re-queues the run (`status == "queued"`) and `_drain_one` executes it.

- [ ] **Step 3: Guard `recover()`**

In `src/atom/workflow/engine.py`, `recover()`, update the terminal skip and add the marker check before `self._reset_interrupted_step(m)`:

```python
            if m.status in ("complete", "halted", "cancelled"):
                continue
            if self.store.cancel_requested(run_id):
                self._finalize_cancelled(m)                 # a cancel outlived a crash — don't resume
                logger.info("recover: finalized cancelled run %s", run_id)
                continue
            # NOTE: a "queued" run is intentionally NOT skipped here. ...
            self._reset_interrupted_step(m)
```

- [ ] **Step 4: Guard `_drain_one`**

In `src/atom/workflow/engine.py`, `_drain_one`, add the marker check right after the existing terminal re-check:

```python
            if self.store.load(run_id).status in ("complete", "halted", "cancelled"):
                return
            if self.store.cancel_requested(run_id):
                self._finalize_cancelled(self.store.load(run_id))
                return
            await self.execute(run_id)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_workflow_cancel.py -k "recover_finalizes or drain_one_finalizes" -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full cancel + queue suites**

Run: `pytest tests/test_workflow_cancel.py tests/test_workflow_queue.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_cancel.py
git commit -m "feat(engine): recover/drain honor the durable cancel marker

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: API cancel endpoint + `cancel_requested` in GET

**Files:**
- Modify: `src/atom/api/app.py`
- Test: `tests/test_workflow_api.py`

**Interfaces:**
- Consumes: `engine.request_cancel` (Task 3); `store.cancel_requested` (Task 1).
- Produces:
  - `POST /api/runs/{run_id}/cancel` → 200 with the `request_cancel` dict; 404 unknown run;
    409 when the run is already `complete`/`halted`.
  - `GET /api/runs/{run_id}` response includes `"cancel_requested": bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_workflow_api.py`:

```python
@pytest.mark.asyncio
async def test_cancel_unknown_run_is_404(base_config, atom_home):
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.post("/api/runs/ghost/cancel")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_cancel_finished_run_is_409(base_config, atom_home, monkeypatch):
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    monkeypatch.setattr(engine, "request_cancel",
                        lambda rid: {"run_id": rid, "status": "complete", "already": True})
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs/x/cancel")
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_cancel_running_run_maps_response(base_config, atom_home, monkeypatch):
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    monkeypatch.setattr(engine, "request_cancel",
                        lambda rid: {"run_id": rid, "status": "running", "cancel_requested": True})
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs/anyid/cancel")
        assert r.status_code == 200
        assert r.json() == {"run_id": "anyid", "status": "running", "cancel_requested": True}


@pytest.mark.asyncio
async def test_get_run_exposes_cancel_requested_field(base_config, atom_home):
    store = _seed_run(atom_home, "rgf")
    m = store.load("rgf")
    m.status = "complete"                      # terminal -> untouched by lifespan recover()/worker
    store.save(m)
    app = create_app(base_config, engine=WorkflowEngine(base_config, prepared_provider=_provider))
    async with _client(app) as client:
        r = await client.get("/api/runs/rgf")
        assert r.status_code == 200
        assert r.json()["cancel_requested"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_workflow_api.py -k "cancel or cancel_requested_field" -v`
Expected: FAIL — `POST .../cancel` returns 405 (route missing) and `GET` has no `cancel_requested` key.

- [ ] **Step 3: Add `cancel_requested` to `get_run`**

In `src/atom/api/app.py`, replace the `get_run` body:

```python
    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        try:
            m = store.load(run_id)
        except FileNotFoundError:
            raise HTTPException(404, "run not found")
        return {**m.model_dump(), "cancel_requested": store.cancel_requested(run_id)}
```

- [ ] **Step 4: Add the cancel route**

In `src/atom/api/app.py`, add this route (place it right after `get_run`):

```python
    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        """Cancel a queued or running run. Queued/pending runs terminalize immediately;
        a running run stops at its next agent-step boundary (see engine.request_cancel)."""
        try:
            res = engine.request_cancel(run_id)
        except FileNotFoundError:
            raise HTTPException(404, "run not found")
        if res.get("already") and res["status"] in ("complete", "halted"):
            raise HTTPException(409, "run already finished; nothing to cancel")
        return res
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_workflow_api.py -k "cancel or cancel_requested_field" -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run the full API suite (no regressions)**

Run: `pytest tests/test_workflow_api.py -q`
Expected: PASS.

- [ ] **Step 7: Full backend suite gate**

Run: `pytest -q`
Expected: PASS (whole suite green).

- [ ] **Step 8: Commit**

```bash
git add src/atom/api/app.py tests/test_workflow_api.py
git commit -m "feat(api): POST /runs/{id}/cancel + cancel_requested in GET run

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: UI client + status pill + dashboard filter

**Files:**
- Modify: `atom-ui/src/api.ts`, `atom-ui/src/ui.tsx`, `atom-ui/src/RunsDashboard.tsx`

**Interfaces:**
- Produces:
  - `api.cancel(id: string): Promise<{ run_id: string; status: string; cancel_requested?: boolean }>`
  - `Manifest.cancel_requested?: boolean`
  - `RunsPage.counts.cancelled: number`
  - `STATUS_CLASS.cancelled` mapping
  - a `cancelled` filter chip in the dashboard

> UI note: there is no JS test harness; each UI task is verified with `cd atom-ui && npm run build` (which runs `tsc`, i.e. a full typecheck) plus the described manual check.

- [ ] **Step 1: Add the client method + types in `api.ts`**

In `atom-ui/src/api.ts`, add `cancel_requested?: boolean;` to the `Manifest` interface:

```typescript
export interface Manifest {
  run_id: string; workflow: string; status: string; inputs: Record<string, unknown>;
  created_at: string; ended_at?: string; workspace_path: string; steps: StepState[];
  cancel_requested?: boolean;
}
```

Change the `RunsPage.counts` type to include `cancelled`:

```typescript
export interface RunsPage { items: RunSummary[]; total: number; counts: { active: number; complete: number; halted: number; cancelled: number }; }
```

Add the `cancel` method to the `api` object (after `selfImprove`):

```typescript
  cancel: (id: string): Promise<{ run_id: string; status: string; cancel_requested?: boolean }> =>
    fetch(`/api/runs/${id}/cancel`, { method: "POST" }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `cancel failed (${r.status})`);
      return data as { run_id: string; status: string; cancel_requested?: boolean };
    }),
```

- [ ] **Step 2: Map the `cancelled` status class in `ui.tsx`**

In `atom-ui/src/ui.tsx`, add `cancelled` to `STATUS_CLASS`:

```typescript
export const STATUS_CLASS: Record<string, string> = {
  pending: "idle", running: "warn", succeeded: "ok", failed: "err",
  complete: "ok", halted: "err", cancelled: "idle",
};
```

- [ ] **Step 3: Add the `cancelled` filter + count chip in `RunsDashboard.tsx`**

In `atom-ui/src/RunsDashboard.tsx`, extend `FILTERS`:

```typescript
const FILTERS = ["active", "complete", "halted", "cancelled", "all"] as const;
```

and widen the count-chip index type:

```typescript
            {f}{counts && f !== "all" ? <span className="chip-n">{counts[f as "active" | "complete" | "halted" | "cancelled"]}</span> : null}
```

- [ ] **Step 4: Typecheck + build**

Run: `cd atom-ui && npm run build`
Expected: builds with no TypeScript errors.

- [ ] **Step 5: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/ui.tsx atom-ui/src/RunsDashboard.tsx
git commit -m "feat(ui): cancel API client, cancelled status pill + dashboard filter

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: RunView Cancel button + "Cancelling…" state

**Files:**
- Modify: `atom-ui/src/RunView.tsx`

**Interfaces:**
- Consumes: `api.cancel` (Task 7); `Manifest.cancel_requested` (Task 7); `StatusPill` (existing).
- Produces: a Cancel button in `.run-status` (active runs only), a "Cancelling…" pill during the running→cancelled window, and `cancelled` added to the polling stop condition.

- [ ] **Step 1: Add cancel state + handler**

In `atom-ui/src/RunView.tsx`, add state hooks alongside the existing ones (after the `improving`/`improveMsg` hooks):

```typescript
  const [cancelling, setCancelling] = useState(false);
  const [cancelMsg, setCancelMsg] = useState<{ text: string; kind: "ok" | "err" } | null>(null);
```

Add the handler (after `runSelfImprove`):

```typescript
  const cancelRun = async () => {
    if (!window.confirm("Cancel this run? The current step finishes, then it stops.")) return;
    setCancelling(true);
    setCancelMsg(null);
    try {
      await api.cancel(runId);
    } catch (e) {
      setCancelMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setCancelling(false);
    }
  };
```

- [ ] **Step 2: Stop polling on `cancelled`**

In `atom-ui/src/RunView.tsx`, in the polling `tick`, update the stop condition:

```typescript
        if (m.status === "complete" || m.status === "halted" || m.status === "cancelled") return;
```

- [ ] **Step 3: Render the Cancel button + "Cancelling…" pill**

In `atom-ui/src/RunView.tsx`, replace the `<div className="run-status">` block's `StatusPill` line and add the button. The `run-status` block becomes:

```tsx
          <div className="run-status">
            {manifest.cancel_requested && manifest.status === "running"
              ? <span className="pill warn">cancelling…</span>
              : <StatusPill status={manifest.status} />}
            <span className="dim">Step {curStep} of {manifest.steps.length}</span>
            <span className="dim">{elapsed(manifest.created_at, manifest.ended_at)}</span>
            {(manifest.status === "pending" || manifest.status === "queued"
              || (manifest.status === "running" && !manifest.cancel_requested)) && (
              <button className="btn-sm" disabled={cancelling}
                onClick={() => cancelRun()}
                title="Stop this run at the next step boundary">
                {cancelling ? "Cancelling…" : "Cancel run"}
              </button>
            )}
            <button className="btn-sm" disabled={manifest.status !== "complete" || exporting !== null}
              onClick={() => runExport()}
              title={manifest.status === "complete"
                ? "Download this run's LangSmith traces"
                : "Available once all steps complete"}>
              {exporting === "run" ? "Exporting…" : "Export run"}
            </button>
            {manifest.workflow !== "self-improve" && (
              <button className="btn-sm"
                disabled={!(manifest.status === "complete" || manifest.status === "halted") || improving}
                onClick={() => runSelfImprove()}
                title={(manifest.status === "complete" || manifest.status === "halted")
                  ? "Analyze this run and draft an improved workflow"
                  : "Available once the run finishes"}>
                {improving ? "Improving…" : "Improve"}
              </button>
            )}
          </div>
```

- [ ] **Step 4: Show a cancel error banner (reuse the export-banner pattern)**

In `atom-ui/src/RunView.tsx`, add this block right after the `improveMsg` banner block:

```tsx
      {cancelMsg && (
        <div className={`export-banner ${cancelMsg.kind}`}>
          <span className="export-text">{cancelMsg.text}</span>
          <button className="export-x" onClick={() => setCancelMsg(null)} title="Dismiss">✕</button>
        </div>
      )}
```

- [ ] **Step 5: Typecheck + build**

Run: `cd atom-ui && npm run build`
Expected: builds with no TypeScript errors.

- [ ] **Step 6: Manual verification**

Start the app (`atom serve` or the project's run command), submit a multi-step workflow, open the run, click **Cancel run**, confirm the dialog. Expected: the pill shows "cancelling…" while the current step finishes, then flips to `cancelled`; no later step runs; the button disappears once cancellation is requested. Cancel a queued run and confirm it goes straight to `cancelled`.

- [ ] **Step 7: Commit**

```bash
git add atom-ui/src/RunView.tsx
git commit -m "feat(ui): Cancel run button + cancelling state in RunView

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Text wrapping (wrap prose, scroll code)

**Files:**
- Modify: `atom-ui/src/styles.css`

**Interfaces:**
- Produces: prose messages/thinking wrap (including long unbreakable tokens); tool output scrolls horizontally within its block; code/table blocks unchanged (already scroll); rail labels wrap defensively.

- [ ] **Step 1: Wrap prose message text**

In `atom-ui/src/styles.css`, add `overflow-wrap: anywhere;` to `.msg-text` (the rule currently reads `.msg-text { white-space: pre-wrap; background: ...; padding: 10px 12px; }`):

```css
.msg-text { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 10px 12px; }
```

- [ ] **Step 2: Wrap markdown prose**

In `atom-ui/src/styles.css`, update `.msg-text.md`:

```css
.msg-text.md { white-space: normal; overflow-wrap: anywhere; }   /* markdown controls its own spacing (shared .md rules) */
```

- [ ] **Step 3: Make tool output scroll instead of overflow**

In `atom-ui/src/styles.css`, update `.msg.tool .msg-text` to scroll (it currently reads `.msg.tool .msg-text { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); }`):

```css
.msg.tool .msg-text { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); white-space: pre; overflow-x: auto; }
```

- [ ] **Step 4: Defensive wrap on fixed-width rail labels**

In `atom-ui/src/styles.css`, update `.rail-task-id` and `.art-name`:

```css
.rail-task-id { flex: 1; font-weight: 540; overflow-wrap: anywhere; }
```
```css
.art-name { font-weight: 540; overflow-wrap: anywhere; }
```

- [ ] **Step 5: Typecheck + build**

Run: `cd atom-ui && npm run build`
Expected: builds cleanly (CSS is bundled by Vite).

- [ ] **Step 6: Manual verification**

Open a run whose transcript contains (a) a very long unbroken token/URL in a prose message, (b) wide tool output (e.g. a long bash line), and (c) a fenced code block. Expected: prose wraps within its box; tool output and code blocks show an inner horizontal scrollbar; the page body never scrolls sideways.

- [ ] **Step 7: Commit**

```bash
git add atom-ui/src/styles.css
git commit -m "fix(ui): wrap long prose lines; scroll code/tool-output blocks

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] Run the whole Python suite: `pytest -q` → all green.
- [ ] Build the UI: `cd atom-ui && npm run build` → no errors.
- [ ] Smoke-test end to end (per Task 8/9 manual steps): submit a multi-step run, cancel it mid-flight (verify graceful stop + `cancelled` status), cancel a queued run, and confirm text wrapping in the transcript.
