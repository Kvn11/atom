# Durable Workflow Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put a durable, config-driven single-lane queue in front of workflow execution so back-to-back invocations run one-at-a-time (raisable later) and survive process death.

**Architecture:** The run store *is* the queue (new `queued` status). A background worker inside `atom serve` drains it under an `asyncio.Semaphore` sized by `queue.max_concurrent_runs`, guarded across processes by a POSIX `flock` lease so a standalone `atom workflow run` can drain when no server is up without ever overlapping one. On startup a recovery pass re-queues runs left non-terminal by a crash; `execute()` resumes them at step granularity.

**Tech Stack:** Python 3.10+, LangChain/LangGraph v1, Pydantic v2, FastAPI/Starlette (lifespan), Typer CLI, `fcntl.flock` (stdlib), pytest + pytest-asyncio.

## Global Constraints

- **Run tests with `.venv/bin/python -m pytest`** — NOT bare `pytest` (repo-root import requirement; several modules do `from tests.conftest import ...`).
- **No new runtime dependencies.** The lease uses stdlib `fcntl`; lifespan tests use `app.router.lifespan_context` (Starlette), not `asgi-lifespan`.
- **Config-driven, default `queue.max_concurrent_runs: 1`** (clamp to `>= 1` at use site via `max(1, ...)`, mirroring `workflow.max_parallel`).
- **Atomic persistence already exists** — always mutate through `RunStore.save()` (tmp + `os.replace`). Never write `run.json` directly.
- **`flock` is POSIX** (macOS + Linux). Windows is unsupported for the standalone-drain path; acceptable.
- **`asyncio_mode = "auto"`** is set — new async tests may use `async def test_...` directly; existing tests also carry `@pytest.mark.asyncio`, so match the file you edit.
- **Run status vocabulary becomes `queued | running | complete | halted`** (was `pending | running | complete | halted`). Task status vocabulary is unchanged (`pending | running | succeeded | failed`).

---

## File Structure

**New files:**
- `src/atom/workflow/lease.py` — `WorkerLease`, a flock-based cross-process single-drainer lease.
- `tests/test_workflow_lease.py` — lease mutual-exclusion tests.
- `tests/test_workflow_queue.py` — engine enqueue/worker/recovery/resume tests.

**Modified files:**
- `src/atom/config/schema.py` — add `QueueConfig`, wire into `AtomConfig`.
- `config.yaml` — add the `queue:` block.
- `src/atom/workflow/run_store.py` — `queued` status, `enqueued_at`, `queue_dir`, scan helpers.
- `src/atom/workflow/engine.py` — enqueue, worker loop, lease, recovery, step-level resume; remove `launch()`.
- `src/atom/api/app.py` — lifespan (recover + worker under lease), `submit_run` enqueues.
- `src/atom/cli.py` — `workflow run` enqueues then `await_run`; `serve` unchanged.
- `tests/test_workflow_config.py` — `QueueConfig` defaults/override.
- `tests/test_workflow_run_store.py` — scan-helper + `enqueued_at` tests.
- `tests/test_workflow_api.py` — run existing tests under the lifespan; assert `queued`.
- `tests/test_workflow_cli.py` — standalone drain still completes.
- `tests/test_workflow_engine.py` — remove the obsolete `launch()` test.
- `README.md` — document the queue.

---

## Task 1: `QueueConfig` — config schema + config.yaml

**Files:**
- Modify: `src/atom/config/schema.py` (add `QueueConfig`; add field to `AtomConfig`)
- Modify: `config.yaml` (add `queue:` block)
- Test: `tests/test_workflow_config.py`

**Interfaces:**
- Produces: `atom.config.schema.QueueConfig(max_concurrent_runs: int = 1, poll_interval_seconds: float = 3.0)`; `AtomConfig.queue: QueueConfig`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_config.py`:

```python
def test_queue_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.queue.max_concurrent_runs == 1
    assert cfg.queue.poll_interval_seconds == 3.0


def test_queue_config_override():
    from atom.config.schema import QueueConfig
    qc = QueueConfig(max_concurrent_runs=3, poll_interval_seconds=0.5)
    assert qc.max_concurrent_runs == 3 and qc.poll_interval_seconds == 0.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py::test_queue_config_defaults -v`
Expected: FAIL — `ImportError: cannot import name 'QueueConfig'` (or `AttributeError: ... 'queue'`).

- [ ] **Step 3: Write minimal implementation**

In `src/atom/config/schema.py`, add the class after `WorkflowConfig` (around line 62):

```python
class QueueConfig(_Base):
    # How many workflow RUNS execute at once (distinct from workflow.max_parallel, which caps
    # TASKS within a step). Default 1 = strictly one workflow at a time. Raise as compute grows.
    max_concurrent_runs: int = 1
    # How often the worker re-scans the store for cross-process enqueues + orphaned runs.
    # In-process API enqueues wake it instantly via an event; this only bounds cross-process latency.
    poll_interval_seconds: float = 3.0
```

In `AtomConfig` (after the `workflow` field, ~line 129), add:

```python
    queue: QueueConfig = Field(default_factory=QueueConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 5: Update config.yaml**

In `config.yaml`, add after the `workflow:` block (after line 29):

```yaml
queue:
  max_concurrent_runs: 1   # how many workflow RUNS execute at once (raise when compute grows)
  poll_interval_seconds: 3 # worker re-scan interval for cross-process enqueues + crash recovery
```

- [ ] **Step 6: Commit**

```bash
git add src/atom/config/schema.py config.yaml tests/test_workflow_config.py
git commit -m "feat(queue): config-driven queue.max_concurrent_runs (default 1)"
```

---

## Task 2: Manifest — `queued` status, `enqueued_at`, store scan helpers

**Files:**
- Modify: `src/atom/workflow/run_store.py`
- Test: `tests/test_workflow_run_store.py`

**Interfaces:**
- Produces: `RunManifest.enqueued_at: Optional[str]`; `RunSummary.enqueued_at: Optional[str]`; `RunStore.queue_dir -> Path`; `RunStore.queued_run_ids() -> list[str]` (FIFO by `(enqueued_at or created_at, run_id)`); `RunStore.interrupted_run_ids() -> list[str]` (status in `("pending","running")`).
- Consumes: existing `RunStore`, `RunManifest`, `RunSummary`, `summarize()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_run_store.py` (import `RunStore`, `RunManifest`, `StepState`, `TaskState` from `atom.workflow.run_store` at top if not already imported):

```python
def _mk(run_id, status, enqueued_at=None, created_at="2026-07-12T00:00:00"):
    from atom.workflow.run_store import RunManifest
    return RunManifest(
        run_id=run_id, workflow="wf", status=status,
        created_at=created_at, enqueued_at=enqueued_at, workspace_path="/x", steps=[],
    )


def test_queue_dir_path(atom_home):
    from atom.workflow.run_store import RunStore
    store = RunStore(str(atom_home))
    assert store.queue_dir == atom_home / "workflows" / "queue"


def test_queued_run_ids_fifo_and_interrupted(atom_home):
    from atom.workflow.run_store import RunStore
    store = RunStore(str(atom_home))
    store.create(_mk("b", "queued", enqueued_at="2026-07-12T00:00:02.000000"))
    store.create(_mk("a", "queued", enqueued_at="2026-07-12T00:00:01.000000"))
    store.create(_mk("done", "complete"))
    store.create(_mk("mid", "running"))
    store.create(_mk("new", "pending"))

    assert store.queued_run_ids() == ["a", "b"]           # FIFO by enqueued_at
    assert set(store.interrupted_run_ids()) == {"mid", "new"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_run_store.py::test_queued_run_ids_fifo_and_interrupted -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'enqueued_at'` or `AttributeError: 'RunStore' object has no attribute 'queued_run_ids'`.

- [ ] **Step 3: Write minimal implementation**

In `src/atom/workflow/run_store.py`:

Change `_ACTIVE` (line 16) to include `queued`:

```python
_ACTIVE = ("pending", "queued", "running")
```

Add `enqueued_at` to `RunManifest` (after `created_at`, ~line 50):

```python
    enqueued_at: Optional[str] = None  # microsecond-precision; primary FIFO sort key
```

Add `enqueued_at` to `RunSummary` (after `created_at`, ~line 60):

```python
    enqueued_at: Optional[str] = None
```

In `summarize()` (~line 70), add `enqueued_at=manifest.enqueued_at,` to the `RunSummary(...)` call.

Add a `queue_dir` property to `RunStore` (near `runs_dir`, ~line 105):

```python
    @property
    def queue_dir(self) -> Path:
        return self.home / "workflows" / "queue"
```

Add a shared scan helper + the two query methods (place after `list_summaries`, end of class):

```python
    def _scan_summaries(self) -> list["RunSummary"]:
        if not self.runs_dir.is_dir():
            return []
        out: list[RunSummary] = []
        for d in self.runs_dir.iterdir():
            s = self._read_summary(d)
            if s is not None:
                out.append(s)
        return out

    def queued_run_ids(self) -> list[str]:
        q = [s for s in self._scan_summaries() if s.status == "queued"]
        q.sort(key=lambda s: (s.enqueued_at or s.created_at, s.run_id))
        return [s.run_id for s in q]

    def interrupted_run_ids(self) -> list[str]:
        return [s.run_id for s in self._scan_summaries() if s.status in ("pending", "running")]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_run_store.py -v`
Expected: PASS (old + new).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/run_store.py tests/test_workflow_run_store.py
git commit -m "feat(queue): queued status + enqueued_at + store scan helpers"
```

---

## Task 3: `WorkerLease` — flock-based single-drainer lease

**Files:**
- Create: `src/atom/workflow/lease.py`
- Test: `tests/test_workflow_lease.py`

**Interfaces:**
- Produces: `atom.workflow.lease.WorkerLease(path: Path)` with `acquire() -> bool` (idempotent True if already held), `release() -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflow_lease.py`:

```python
"""WorkerLease: cross-process (and cross-handle) mutual exclusion via flock."""
from __future__ import annotations

from atom.workflow.lease import WorkerLease


def test_lease_is_mutually_exclusive_and_reacquirable(tmp_path):
    path = tmp_path / "queue" / "worker.lock"
    a = WorkerLease(path)
    b = WorkerLease(path)

    assert a.acquire() is True          # first holder wins
    assert a.acquire() is True          # idempotent for the same handle
    assert b.acquire() is False         # second handle is denied while a holds it

    a.release()
    assert b.acquire() is True          # freed -> b can take it now
    b.release()


def test_release_without_acquire_is_safe(tmp_path):
    WorkerLease(tmp_path / "q" / "w.lock").release()   # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_lease.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.workflow.lease'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/atom/workflow/lease.py`:

```python
"""Cross-process single-drainer lease via POSIX flock.

flock is tied to the open file description and is released automatically by the OS when the
holding process dies, so a crashed holder never leaves a stale lock. Two distinct handles
(even in one process) contend, which is what makes "only one drainer" hold across processes.
POSIX only (macOS + Linux); the standalone-drain path is unsupported on Windows.
"""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkerLease:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to take the lease without blocking. True if held (or already held by us)."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_lease.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/lease.py tests/test_workflow_lease.py
git commit -m "feat(queue): WorkerLease flock-based single-drainer lease"
```

---

## Task 4: `engine.enqueue()` + wake event + engine wiring

**Files:**
- Modify: `src/atom/workflow/engine.py` (imports, `__init__`, add `_now_micros`, `enqueue`)
- Test: `tests/test_workflow_queue.py` (new file)

**Interfaces:**
- Consumes: `WorkerLease` (Task 3); `RunStore.queue_dir` (Task 2); `RunManifest.enqueued_at` (Task 2).
- Produces: `WorkflowEngine.enqueue(run_id: str) -> None` (sets status `queued` + `enqueued_at`, persists, signals `self._wake`); `WorkflowEngine._wake: asyncio.Event`; `WorkflowEngine.lease: WorkerLease`; `WorkflowEngine._inflight: set[str]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflow_queue.py`:

```python
"""Durable queue: enqueue, worker draining, crash recovery, step-level resume."""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

import atom.workflow.engine as engine_mod
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef
from tests.conftest import make_prepared


def _one_task_wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo",
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "hi"}]}],
    })


def test_enqueue_marks_queued_and_stamps_enqueued_at(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "rq1", "2026-07-12T00:00:00")
    engine.enqueue("rq1")
    m = engine.store.load("rq1")
    assert m.status == "queued"
    assert m.enqueued_at is not None
    assert engine.store.queued_run_ids() == ["rq1"]

    # terminal runs are never re-opened by enqueue
    done = engine.store.load("rq1")
    done.status = "complete"
    engine.store.save(done)
    engine.enqueue("rq1")
    assert engine.store.load("rq1").status == "complete"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py::test_enqueue_marks_queued_and_stamps_enqueued_at -v`
Expected: FAIL — `AttributeError: 'WorkflowEngine' object has no attribute 'enqueue'`.

- [ ] **Step 3: Write minimal implementation**

In `src/atom/workflow/engine.py`:

Add import near the other `atom.workflow` imports (top of file):

```python
from atom.workflow.lease import WorkerLease
```

Add a microsecond timestamp helper next to `_now()` (~line 36):

```python
def _now_micros() -> str:
    return datetime.datetime.now().isoformat(timespec="microseconds")
```

In `WorkflowEngine.__init__`, after `self._task_cfg = self._build_task_cfg(cfg)` (~line 67), add the queue/worker state:

```python
        # --- durable-queue worker state ---
        self.lease = WorkerLease(self.store.queue_dir / "worker.lock")
        self._wake = asyncio.Event()
        self._inflight: set[str] = set()
        self._worker_tasks: set[asyncio.Task] = set()
        self._worker_loop_task: asyncio.Task | None = None
        self._stopping = False
```

Add the `enqueue` method (place after `create_run`, ~line 108):

```python
    def enqueue(self, run_id: str) -> None:
        """Mark a created run as queued (durable + atomic) and wake any in-process worker.
        The job is safe the instant this returns; a worker picks it up in FIFO order."""
        m = self.store.load(run_id)
        if m.status in ("complete", "halted"):
            return                       # never re-open a terminal run
        m.status = "queued"
        m.enqueued_at = _now_micros()
        self.store.save(m)
        self._wake.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_queue.py
git commit -m "feat(queue): engine.enqueue + worker state scaffolding"
```

---

## Task 5: `engine.recover()` — re-queue interrupted runs

**Files:**
- Modify: `src/atom/workflow/engine.py` (add `recover`, `_reset_interrupted_step`)
- Test: `tests/test_workflow_queue.py`

**Interfaces:**
- Consumes: `RunStore.interrupted_run_ids()` (Task 2); `compute_step_status` (already imported in engine).
- Produces: `WorkflowEngine.recover() -> None` (status `pending`/`running` → `queued`, reset interrupted step's `running` tasks to `pending`); `WorkflowEngine._reset_interrupted_step(m) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_queue.py`:

```python
def test_recover_requeues_running_and_resets_interrupted_step(base_config, atom_home):
    from atom.workflow.run_store import RunManifest, StepState, TaskState
    engine = WorkflowEngine(base_config)
    # Step 0 fully done; step 1 interrupted mid-flight (one succeeded, one still "running").
    m = RunManifest(
        run_id="rc1", workflow="demo", status="running",
        created_at="2026-07-12T00:00:00", enqueued_at="2026-07-12T00:00:00.000000",
        workspace_path=str(engine.store.workspace_dir("rc1")),
        steps=[
            StepState(index=0, title="A", status="complete",
                      tasks=[TaskState(id="a", thread_id="rc1:s0:a", status="succeeded")]),
            StepState(index=1, title="B", status="running", tasks=[
                TaskState(id="b1", thread_id="rc1:s1:b1", status="succeeded"),
                TaskState(id="b2", thread_id="rc1:s1:b2", status="running", started_at="t"),
            ]),
        ],
    )
    engine.store.create(m)

    engine.recover()

    r = engine.store.load("rc1")
    assert r.status == "queued"                      # re-queued for resume
    assert r.steps[0].status == "complete"           # finished step untouched
    b1, b2 = r.steps[1].tasks
    assert b1.status == "succeeded"                  # already-done task kept
    assert b2.status == "pending" and b2.started_at is None  # in-flight task reset for rerun
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py::test_recover_requeues_running_and_resets_interrupted_step -v`
Expected: FAIL — `AttributeError: 'WorkflowEngine' object has no attribute 'recover'`.

- [ ] **Step 3: Write minimal implementation**

In `src/atom/workflow/engine.py`, add after `enqueue` (import `RunManifest` is already imported at top via the run_store import block):

```python
    def recover(self) -> None:
        """Re-queue runs left non-terminal by a crash/shutdown so the worker resumes them.
        MUST be called only while holding the worker lease (sole-drainer guarantee)."""
        for run_id in self.store.interrupted_run_ids():
            try:
                m = self.store.load(run_id)
            except Exception:  # noqa: BLE001 — a corrupt manifest must not block recovery
                logger.exception("recover: failed to load run %s; skipping", run_id)
                continue
            if m.status in ("complete", "halted", "queued"):
                continue
            self._reset_interrupted_step(m)
            m.status = "queued"
            if m.enqueued_at is None:
                m.enqueued_at = _now_micros()
            self.store.save(m)
            logger.info("recover: re-queued interrupted run %s", run_id)

    @staticmethod
    def _reset_interrupted_step(m: "RunManifest") -> None:
        for step in m.steps:
            if step.status == "running":
                for t in step.tasks:
                    if t.status == "running":
                        t.status = "pending"
                        t.started_at = None
                step.status = compute_step_status([t.status for t in step.tasks])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_queue.py
git commit -m "feat(queue): boot recovery re-queues interrupted runs"
```

---

## Task 6: `execute()` — step-level resume + requeue-on-cancel

**Files:**
- Modify: `src/atom/workflow/engine.py` (`execute()` step loop + exception handling)
- Test: `tests/test_workflow_queue.py`

**Interfaces:**
- Consumes: existing `execute()`, `_run_task`, `compute_step_status`.
- Produces: `execute()` skips `complete` steps and re-runs only non-`succeeded` tasks; a cancellation (worker stop / Ctrl-C) sets the run back to `queued` instead of `halted`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_queue.py`:

```python
@pytest.mark.asyncio
async def test_execute_resumes_skipping_completed_step(base_config, atom_home, monkeypatch):
    from atom.workflow.run_store import RunManifest, StepState, TaskState
    from atom.runtime import RunResult

    calls: list[str] = []

    async def spy(prompt, **kwargs):
        calls.append(kwargs.get("thread_id", "?"))
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)

    wf = WorkflowDef.model_validate({
        "name": "demo",
        "steps": [
            {"title": "A", "tasks": [{"id": "a", "prompt": "x"}]},
            {"title": "B", "tasks": [{"id": "b", "prompt": "y"}]},
        ],
    })
    # Persist a run where step 0 already completed; step 1 pending (as after a resume/recover).
    m = RunManifest(
        run_id="rr1", workflow="demo", status="queued",
        created_at="2026-07-12T00:00:00", enqueued_at="2026-07-12T00:00:00.000000",
        workspace_path=str(engine_store_ws(base_config, "rr1")),
        steps=[
            StepState(index=0, title="A", status="complete",
                      tasks=[TaskState(id="a", thread_id="rr1:s0:a", status="succeeded")]),
            StepState(index=1, title="B", status="pending",
                      tasks=[TaskState(id="b", thread_id="rr1:s1:b", status="pending")]),
        ],
    )
    engine = WorkflowEngine(base_config)
    engine.store.create(m)
    engine._defs["rr1"] = wf               # provide the WorkflowDef (no demo.yaml on disk here)

    manifest = await engine.execute("rr1")

    assert manifest.status == "complete"
    assert calls == ["rr1:s1:b"]           # only the unfinished task ran; step 0 was skipped


def engine_store_ws(cfg, run_id):
    from atom.workflow.run_store import RunStore
    return RunStore(cfg.home).workspace_dir(run_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py::test_execute_resumes_skipping_completed_step -v`
Expected: FAIL — `assert calls == ["rr1:s1:b"]` fails because current `execute()` re-runs step 0's task too (`calls == ["rr1:s0:a", "rr1:s1:b"]`).

- [ ] **Step 3: Write minimal implementation**

In `src/atom/workflow/engine.py`, replace the step loop body inside `execute()` (currently lines ~169–188) with the resume-aware version:

```python
            sem = asyncio.Semaphore(max(1, self.cfg.workflow.max_parallel))
            for step_state, step_def in zip(manifest.steps, workflow.steps):
                if step_state.status == "complete":
                    continue                       # resume: this step finished in a prior life
                step_state.status = "running"
                self.store.save(manifest)

                async def run_one(ts: TaskState, td: TaskDef, sd: StepDef, ss: StepState):
                    async with sem:
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
                if step_state.status != "complete":
                    manifest.status = "halted"
                    manifest.ended_at = _now()
                    self.store.save(manifest)
                    return manifest
```

Then add a `CancelledError` branch to `execute()`'s exception handling. Immediately **before** the existing `except BaseException:` (~line 194), insert:

```python
        except asyncio.CancelledError:
            # Worker stop / Ctrl-C: put the run back on the queue so the next startup resumes it
            # (step-level). Do NOT mark it halted — halted is terminal and would strand the run.
            manifest.status = "queued"
            try:
                self.store.save(manifest)
            except Exception:  # noqa: BLE001
                logger.exception("workflow run %s: failed to requeue on cancel", run_id)
            raise
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py tests/test_workflow_engine.py -v`
Expected: PASS (resume test passes; existing engine tests still pass — none assert `execute()` returns `halted` on cancellation).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_queue.py
git commit -m "feat(queue): step-level resume + requeue-on-cancel in execute()"
```

---

## Task 7: The worker — `run_worker`/`_drain_one`/`start_worker`/`stop_worker`

**Files:**
- Modify: `src/atom/workflow/engine.py` (add worker methods; additive — `launch()` retired in Task 8)
- Test: `tests/test_workflow_queue.py`

**Interfaces:**
- Consumes: `enqueue`, `execute`, `store.queued_run_ids`, `_wake`, `_inflight` (Tasks 4–6).
- Produces: `WorkflowEngine.start_worker() -> asyncio.Task`; `async WorkflowEngine.stop_worker() -> None`; `async WorkflowEngine.run_worker() -> None`; `async WorkflowEngine._drain_one(run_id, sem) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_queue.py`:

```python
@pytest.mark.asyncio
async def test_worker_serializes_runs_at_concurrency_one(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult
    live = {"n": 0, "max": 0}

    async def spy(prompt, **kwargs):
        live["n"] += 1
        live["max"] = max(live["max"], live["n"])
        await asyncio.sleep(0.05)
        live["n"] -= 1
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    base_config.queue.max_concurrent_runs = 1
    engine = WorkflowEngine(base_config)
    for rid in ("w1", "w2", "w3"):
        engine.create_run(_one_task_wf(), {}, rid, "2026-07-12T00:00:00")
        engine.enqueue(rid)

    engine.start_worker()
    for _ in range(200):                       # wait until all drained
        if all(engine.store.load(r).status == "complete" for r in ("w1", "w2", "w3")):
            break
        await asyncio.sleep(0.02)
    await engine.stop_worker()

    assert live["max"] == 1                     # never two runs at once at concurrency 1
    assert all(engine.store.load(r).status == "complete" for r in ("w1", "w2", "w3"))


@pytest.mark.asyncio
async def test_worker_allows_two_at_concurrency_two(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult
    live = {"n": 0, "max": 0}

    async def spy(prompt, **kwargs):
        live["n"] += 1
        live["max"] = max(live["max"], live["n"])
        await asyncio.sleep(0.05)
        live["n"] -= 1
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    base_config.queue.max_concurrent_runs = 2
    engine = WorkflowEngine(base_config)
    for rid in ("p1", "p2", "p3"):
        engine.create_run(_one_task_wf(), {}, rid, "2026-07-12T00:00:00")
        engine.enqueue(rid)

    engine.start_worker()
    for _ in range(200):
        if all(engine.store.load(r).status == "complete" for r in ("p1", "p2", "p3")):
            break
        await asyncio.sleep(0.02)
    await engine.stop_worker()

    assert live["max"] == 2                     # exactly two in flight, never three


@pytest.mark.asyncio
async def test_worker_survives_transient_scan_error(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult

    async def spy(prompt, **kwargs):
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    base_config.queue.poll_interval_seconds = 0.05     # fast back-off retry for the test
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "sv1", "2026-07-12T00:00:00")
    engine.enqueue("sv1")

    calls = {"n": 0}
    real_scan = engine.store.queued_run_ids

    def flaky_scan():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient scan failure")     # first iteration blows up
        return real_scan()

    monkeypatch.setattr(engine.store, "queued_run_ids", flaky_scan)

    engine.start_worker()
    for _ in range(200):
        if engine.store.load("sv1").status == "complete":
            break
        await asyncio.sleep(0.02)
    await engine.stop_worker()

    assert calls["n"] >= 2                              # the loop retried after the error
    assert engine.store.load("sv1").status == "complete"   # worker recovered and drained
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py::test_worker_serializes_runs_at_concurrency_one -v`
Expected: FAIL — `AttributeError: 'WorkflowEngine' object has no attribute 'start_worker'`.

- [ ] **Step 3: Write minimal implementation**

This task is **purely additive** — it adds the worker but leaves the legacy `launch()` in place so the API (which still calls `launch()` until Task 8) and its tests stay green. `launch()` is removed in Task 8, once `submit_run` no longer uses it.

In `src/atom/workflow/engine.py`, add the worker methods (place after `recover`/`_reset_interrupted_step`):

```python
    # ---- worker (drainer) ----
    def start_worker(self) -> "asyncio.Task":
        """Start the background drain loop on the current event loop. Idempotent-ish: assumes
        the caller holds the lease (see the API lifespan / CLI await_run)."""
        self._stopping = False
        self._worker_loop_task = asyncio.create_task(self.run_worker())
        return self._worker_loop_task

    async def stop_worker(self) -> None:
        """Stop draining and cancel any in-flight runs (each execute() requeues itself on
        cancellation, so nothing is lost)."""
        self._stopping = True
        self._wake.set()
        if self._worker_loop_task is not None:
            self._worker_loop_task.cancel()
            await asyncio.gather(self._worker_loop_task, return_exceptions=True)
            self._worker_loop_task = None
        for t in list(self._worker_tasks):
            t.cancel()
        if self._worker_tasks:
            await asyncio.gather(*list(self._worker_tasks), return_exceptions=True)
        self._worker_tasks.clear()
        self._inflight.clear()

    async def run_worker(self) -> None:
        poll = float(self.cfg.queue.poll_interval_seconds)
        sem = asyncio.Semaphore(max(1, self.cfg.queue.max_concurrent_runs))
        while not self._stopping:
            # Supervisor: a scan/scheduling error in one iteration must NOT kill the drainer
            # (that would silently stop draining until the next restart). Log and continue.
            try:
                self._wake.clear()
                for run_id in self.store.queued_run_ids():
                    if self._stopping:
                        break
                    if run_id in self._inflight:
                        continue
                    await sem.acquire()             # blocks when at capacity
                    if self._stopping:
                        sem.release()
                        break
                    self._inflight.add(run_id)
                    t = asyncio.create_task(self._drain_one(run_id, sem))
                    self._worker_tasks.add(t)
                    t.add_done_callback(self._worker_tasks.discard)
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=poll)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise                               # shutdown -> propagate, never swallow
            except Exception:  # noqa: BLE001 — a scan/scheduling error must not kill the drainer
                logger.exception("worker: drain loop iteration failed; continuing")
                await asyncio.sleep(poll)           # back off to avoid a hot error loop

    async def _drain_one(self, run_id: str, sem: "asyncio.Semaphore") -> None:
        try:
            await self.execute(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — execute() already terminalizes; this is belt-and-suspenders
            logger.exception("worker: run %s crashed in execute()", run_id)
        finally:
            self._inflight.discard(run_id)
            sem.release()
            if not self._stopping:
                self._wake.set()                    # a slot freed -> rescan for more work
```

Note: `run_worker`/`_drain_one` reference `self._wake`, `self._inflight`, `self._worker_tasks`, `self._worker_loop_task`, `self._stopping` — all initialized in `__init__` in Task 4.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py tests/test_workflow_engine.py -v`
Expected: PASS. (Concurrency tests pass; the legacy `launch()` and its test are still present and still pass — they are retired in Task 8.)

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_queue.py
git commit -m "feat(queue): background worker drains the queue (additive)"
```

---

## Task 8: API — lifespan (recover + worker under lease) + `submit_run` enqueues; retire `launch()`

**Files:**
- Modify: `src/atom/api/app.py`
- Modify: `src/atom/workflow/engine.py` (remove the now-unused `launch`, `_on_task_done`, `self._tasks`)
- Test: `tests/test_workflow_api.py`
- Modify: `tests/test_workflow_engine.py` (delete the obsolete `launch()` test)

**Interfaces:**
- Consumes: `engine.lease`, `engine.recover`, `engine.start_worker`, `engine.stop_worker`, `engine.enqueue` (Tasks 4–7).
- Produces: `create_app` installs a lifespan that (under the lease) recovers + starts the worker and stops it on shutdown; `POST /api/runs` returns `{"run_id", "status": "queued"}`. `launch()` is removed (the worker fully replaces the fire-and-forget path; leaving it would let a caller bypass the queue).

- [ ] **Step 1: Write the failing test**

In `tests/test_workflow_api.py`, add a DRY lifespan-aware client helper near the top (after the imports, before the first test), and one new test:

```python
from contextlib import asynccontextmanager


@asynccontextmanager
async def _client(app):
    # Drive the FastAPI lifespan so the queue worker starts/stops (ASGITransport alone does not).
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@pytest.mark.asyncio
async def test_submit_returns_queued_then_worker_drains(base_config, atom_home):
    _seed(atom_home)
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    app = create_app(base_config, engine=engine)
    async with _client(app) as client:
        r = await client.post("/api/runs", json={"workflow": "demo", "inputs": {"topic": "x"}})
        assert r.status_code == 202
        assert r.json()["status"] == "queued"           # enqueued, not immediately running
        manifest = await _poll(client, r.json()["run_id"])
        assert manifest["status"] == "complete"          # the lifespan worker drained it
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py::test_submit_returns_queued_then_worker_drains -v`
Expected: FAIL — the assertion `r.json()["status"] == "queued"` fails (currently `"pending"`), and/or the run never completes because no worker runs.

- [ ] **Step 3: Write minimal implementation**

In `src/atom/api/app.py`:

Add the import at the top:

```python
from contextlib import asynccontextmanager
```

Inside `create_app`, replace the `app = FastAPI(title="atom workflows")` line (~line 34) with a lifespan-wired app:

```python
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holds = engine.lease.acquire()          # lease first: recover + drain only if we own it
        if holds:
            engine.recover()
            engine.start_worker()
        try:
            yield
        finally:
            if holds:
                await engine.stop_worker()
                engine.lease.release()

    app = FastAPI(title="atom workflows", lifespan=lifespan)
```

Change `submit_run` (the `engine.launch(run_id)` line, ~line 67) to enqueue and report `queued`:

```python
        engine.enqueue(run_id)
        return {"run_id": run_id, "status": "queued"}
```

Now that nothing calls `launch()`, **remove the legacy fire-and-forget path** from `src/atom/workflow/engine.py` (the worker fully replaces it; leaving it would let a caller bypass the queue):

- Delete the `self._tasks: dict[asyncio.Task, str] = {}` line from `__init__` (and its two-line comment above it about strong references).
- Delete the entire `launch()` method.
- Delete the entire `_on_task_done()` method.
- Delete the now-dead `launcher` machinery (only `launch()` used it): the `launcher: Launcher | None = None` constructor parameter, the `self.launcher: Launcher = launcher or asyncio.create_task` assignment, and the `Launcher = Callable[[Awaitable], object]` type alias. If `Awaitable` is then unused in the imports, drop it from the `from typing import ...` line.

And in `tests/test_workflow_engine.py`, **delete** the obsolete test `test_launch_orphaned_task_terminalized_via_done_callback` — `launch()` no longer exists; worker terminalization is covered by `test_worker_serializes_runs_at_concurrency_one` in `tests/test_workflow_queue.py`. (Confirm nothing else references these: `grep -rn "launch\|_on_task_done\|self\._tasks\|launcher\|Launcher" src/ tests/` should return no hits after this.)

- [ ] **Step 4: Update the existing API tests to run under the lifespan**

The existing tests submit runs and poll to completion; without the worker they would hang. In each existing test in `tests/test_workflow_api.py`, replace the line:

```python
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
```

with:

```python
    async with _client(app) as client:
```

(Applies to `test_submit_run_and_fetch_results`, `test_missing_required_input_is_422`, `test_runs_list_returns_paginated_summaries`, `test_unknown_artifact_is_404`, `test_html_artifact_served_as_attachment`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_api.py tests/test_workflow_engine.py -v`
Expected: PASS (API tests all pass under the lifespan; the engine suite passes with the obsolete `launch()` test removed).

- [ ] **Step 6: Commit**

```bash
git add src/atom/api/app.py src/atom/workflow/engine.py tests/test_workflow_api.py tests/test_workflow_engine.py
git commit -m "feat(queue): API lifespan runs worker under lease; submit enqueues; retire launch()"
```

---

## Task 9: CLI — `workflow run` enqueues then awaits (drain-or-poll)

**Files:**
- Modify: `src/atom/workflow/engine.py` (add `await_run`)
- Modify: `src/atom/cli.py` (`workflow_run`)
- Test: `tests/test_workflow_queue.py`, `tests/test_workflow_cli.py`

**Interfaces:**
- Consumes: `engine.lease`, `engine.recover`, `engine.execute`, `store.load` (Tasks 3–6).
- Produces: `async WorkflowEngine.await_run(run_id: str) -> RunManifest` — blocks until `run_id` is terminal; becomes the drainer (executing only this run) when the lease is free, otherwise polls while another process drains and retries the lease so a dead drainer is taken over.

- [ ] **Step 1: Write the failing test (engine-level `await_run`)**

Add to `tests/test_workflow_queue.py`:

```python
@pytest.mark.asyncio
async def test_await_run_drains_own_run_when_lease_free(base_config, atom_home, monkeypatch):
    from atom.runtime import RunResult

    async def spy(prompt, **kwargs):
        return RunResult(thread_id=kwargs.get("thread_id", "t"), messages=[], final_text="ok", state={})

    monkeypatch.setattr(engine_mod, "run_agent", spy)
    engine = WorkflowEngine(base_config)
    engine.create_run(_one_task_wf(), {}, "cli1", "2026-07-12T00:00:00")
    engine.enqueue("cli1")

    m = await engine.await_run("cli1")
    assert m.status == "complete"
    assert engine.lease.acquire() is True          # lease was released after draining
    engine.lease.release()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py::test_await_run_drains_own_run_when_lease_free -v`
Expected: FAIL — `AttributeError: 'WorkflowEngine' object has no attribute 'await_run'`.

- [ ] **Step 3: Write minimal implementation (engine)**

In `src/atom/workflow/engine.py`, add after `await_run`'s neighbors (e.g. after `_drain_one`):

```python
    async def await_run(self, run_id: str) -> "RunManifest":
        """Block until run_id is terminal. If no drainer holds the lease, become it and execute
        THIS run (step-level resume), then release; otherwise poll while another process drains,
        retrying the lease so a dead drainer is taken over. Never runs another user's run."""
        poll = float(self.cfg.queue.poll_interval_seconds)
        while True:
            m = self.store.load(run_id)
            if m.status in ("complete", "halted"):
                return m
            if self.lease.acquire():
                try:
                    self.recover()
                    if self.store.load(run_id).status not in ("complete", "halted"):
                        await self.execute(run_id)
                finally:
                    self.lease.release()
                return self.store.load(run_id)
            await asyncio.sleep(poll)
```

- [ ] **Step 4: Write minimal implementation (CLI)**

In `src/atom/cli.py`, in `workflow_run`, replace the execution block (currently lines ~194–206, from the `try:` around `engine.create_run(...)` through the `console.print(f"\n[{color}...")` summary). Replace:

```python
    try:
        engine.create_run(wf, inputs, run_id, datetime.datetime.now().isoformat(timespec="seconds"))
    except MissingInputError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    with console.status(f"[bold]running workflow {name}…[/bold]"):
        manifest = asyncio.run(engine.execute(run_id))
```

with:

```python
    try:
        engine.create_run(wf, inputs, run_id, datetime.datetime.now().isoformat(timespec="seconds"))
    except MissingInputError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)
    engine.enqueue(run_id)

    with console.status(f"[bold]running workflow {name}…[/bold]"):
        manifest = asyncio.run(engine.await_run(run_id))
```

(The rest of `workflow_run` — the per-step print loop, the status color line, and the artifacts listing — is unchanged.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_queue.py::test_await_run_drains_own_run_when_lease_free tests/test_workflow_cli.py -v`
Expected: PASS — `await_run` drains; the existing `test_workflow_run_completes` and `test_workflow_runs_lists` still print `complete` (now via the enqueue → lease-drain path).

- [ ] **Step 6: Commit**

```bash
git add src/atom/workflow/engine.py src/atom/cli.py tests/test_workflow_queue.py
git commit -m "feat(queue): CLI enqueues then drain-or-poll via engine.await_run"
```

---

## Task 10: Docs + full regression

**Files:**
- Modify: `README.md`
- Test: whole suite

**Interfaces:** none (documentation + verification).

- [ ] **Step 1: Document the queue in README.md**

Find the workflow section of `README.md` (search for `atom serve` or `## Workflows`) and add a subsection. Use this content:

```markdown
### Workflow queue

Workflow invocations run through a durable, config-driven queue so they execute one at a time
(by default) instead of all at once — which keeps sub-agent fan-out from hitting provider rate
limits. Configure it in `config.yaml`:

```yaml
queue:
  max_concurrent_runs: 1   # how many workflow RUNS execute at once; raise as compute grows
  poll_interval_seconds: 3 # worker re-scan interval for cross-process enqueues + crash recovery
```

- **Durable:** an enqueued run is written to `$ATOM_HOME/workflows/runs/<id>/run.json` (status
  `queued`) before it starts, so it is never lost. If the server dies mid-run, the next
  `atom serve` startup re-queues interrupted runs and resumes them at step granularity (finished
  steps are skipped).
- **One drainer:** the `atom serve` process drains the queue. When no server is running,
  `atom workflow run` drains its own run in-process under a `flock` lease
  (`$ATOM_HOME/workflows/queue/worker.lock`), so a CLI run and a server can never overlap.
- **`queue.max_concurrent_runs`** caps concurrent *runs*; **`workflow.max_parallel`** (separate)
  caps concurrent *tasks within a step*.
```

- [ ] **Step 2: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — the whole suite green (existing ~210 tests + the new queue/lease tests, minus the one deleted `launch()` test).

- [ ] **Step 3: Manual smoke (optional but recommended)**

Run a standalone CLI workflow to confirm the enqueue→lease-drain path end-to-end:

```bash
.venv/bin/python -m atom workflow run notes-smoke   # or any seeded workflow
```

Expected: prints step statuses and a final `complete`/`halted` line with a run id.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(queue): document the durable workflow queue in README"
```

---

## Self-Review Notes (for the implementer)

- **Concurrency invariant:** with `max_concurrent_runs == 1`, both the `asyncio.Semaphore(1)` and
  the single-holder `flock` lease enforce one run at a time; the semaphore covers in-process,
  the lease covers cross-process (server vs. standalone CLI).
- **No lost jobs:** `enqueue()` persists `queued` atomically before returning; hard crash leaves
  `running` (→ recovered), graceful stop cancels → `execute()` requeues to `queued`.
- **`recover()` only under the lease** — enforced by both call sites (API lifespan, CLI
  `await_run`) acquiring the lease before calling it.
- **Windows:** `fcntl.flock` is POSIX; the standalone-CLI-drain path is macOS/Linux only. The
  server path (Linux deployment) is the supported production topology.
