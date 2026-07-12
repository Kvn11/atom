# Durable Workflow Queue — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan
**Branch (planned):** `feat/workflow-queue`

## Goal

Add a **durable, config-driven queue** in front of workflow execution so that back-to-back
workflow invocations run **one at a time** (raisable later), instead of all firing
concurrently. This:

1. **Serializes runs** — e.g. one workflow invoked 3× with 3 different API groups tests each
   group one at a time, rather than three runs racing at once.
2. **Fixes subagent fan-out / rate-limiting** — the current unbounded run-level concurrency
   multiplies into `N runs × max_parallel tasks × their subagents`; a single-run lane caps it.
3. **Survives crashes** — queued and in-flight jobs are persisted offline; if the platform
   dies, they are re-queued and resumed on the next startup. **No queued job is ever lost.**

## Background — current state & the gap

Two entry points execute workflows today:

- **API** (`src/atom/api/app.py::submit_run`): `engine.create_run()` persists a manifest
  (`status: "pending"`), then `engine.launch()` fire-and-forgets
  `asyncio.create_task(execute())`. **There is no cap on concurrent runs** — every POST
  immediately schedules another concurrent `execute()`.
- **CLI** (`src/atom/cli.py::workflow_run`): synchronous `asyncio.run(engine.execute())` —
  blocks to completion in an ephemeral process.

Within a run, task concurrency is *already* bounded by the `workflow.max_parallel` semaphore
(`engine.py:168`). What is unbounded is **run-level** (invocation-level) concurrency. That is
the missing lane.

**Persistence exists but recovery does not.** `RunStore` (`src/atom/workflow/run_store.py`)
already writes each run durably to `$ATOM_HOME/workflows/runs/<run_id>/run.json` with atomic
`tmp + os.replace` saves, lifecycle `pending → running → complete|halted`. But:

- Nothing ever re-reads a `pending`/`running` manifest after a crash. `launch()` only fires
  in-process. If the server dies, those runs are orphaned in a non-terminal state forever.
- There is **no FastAPI lifespan/startup hook** — `serve` just calls
  `uvicorn.run(create_app(...))` (`cli.py:298`). There is nowhere for a worker to start or for
  recovery to run.
- No queue, worker, or lock concept exists anywhere in `src/` (verified by grep).

## Locked decisions

1. **The queue IS the run store.** No second source of truth. Add one status, `queued`; the
   queue is "all runs with `status == "queued"`, FIFO." Reuses the existing crash-safe atomic
   persistence for free.
2. **Config-driven concurrency, default 1.** New `queue.max_concurrent_runs` (default `1`),
   separate from the existing task-level `workflow.max_parallel`. Raise it when compute grows.
3. **Single drainer, guaranteed across processes by a durable lease.** A POSIX `flock` lease at
   `$ATOM_HOME/workflows/queue/worker.lock`. The server holds it for its lifetime and drains.
   If no server holds it, `atom workflow run` acquires it and drains in-process, then releases —
   so the CLI still works standalone and can **never** overlap a server. `flock` auto-releases
   on process death, so a crashed holder's lease is freed by the OS (crash-safe, no stale
   locks). *(Decision A — durable-lock executor model.)*
4. **Interrupted runs resume at step granularity.** On restart, an in-flight run skips
   already-`complete` steps and re-runs only the interrupted step's not-yet-`succeeded` tasks.
   Least wasted work, least duplicate side-effects (matters for e.g. a security-testing
   workflow re-hitting APIs). *(Decision B.)*
5. **Boot recovery re-queues, never drops.** On startup: `queued` runs stay queued;
   `running` runs (interrupted) are reset to `queued` for resume; `complete`/`halted` are
   terminal and untouched.
6. **Best-effort FIFO ordering.** Order by `(enqueued_at, run_id)` where `enqueued_at` is a
   microsecond-precision timestamp stamped at enqueue. Strict global sequencing across a
   sub-microsecond burst is explicitly *not* a requirement (one-at-a-time + no-loss is).

## Architecture

One durable queue, one bounded drain loop, one cross-process lease, plus boot recovery and a
step-level resume in the engine. The centerpiece is the `WorkflowEngine` growing a worker.

```
        POST /api/runs ─┐
                        ├─► engine.enqueue(run_id)  ──►  run.json {status:"queued", enqueued_at}
   atom workflow run ───┘         (durable, atomic — job is safe the instant it is accepted)
                                        │
                                        ▼
   ┌─────────────────────── worker lease (flock: $ATOM_HOME/workflows/queue/worker.lock) ───────┐
   │  held by the server for its lifetime  (or, if none, grabbed by a standalone CLI run)        │
   │                                                                                             │
   │   engine.run_worker():  loop                                                                │
   │     wait on enqueue-Event  OR  poll_interval_seconds (catches cross-process enqueues)       │
   │     while capacity (Semaphore = queue.max_concurrent_runs) and queued runs exist:           │
   │         pick oldest queued  ──►  execute(run_id)  under the semaphore                        │
   └─────────────────────────────────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                       execute(run_id):  status → running
                         for step in steps:
                           if step.complete: skip            ◄── step-level resume
                           run only tasks where status != "succeeded"
                         status → complete | halted

   startup (FastAPI lifespan):  recover()  ──►  running→queued, queued stays  ──►  start run_worker()
   shutdown:                    cancel worker (execute() requeues in-flight → queued) ; release lease
```

## Components

### 6.1 Config — `QueueConfig`

New block in `src/atom/config/schema.py`, wired into `AtomConfig`, documented in `config.yaml`:

```python
class QueueConfig(_Base):
    # How many workflow RUNS execute at once (distinct from workflow.max_parallel, which caps
    # TASKS within a step). Default 1 = strictly one workflow at a time. Raise as compute grows.
    max_concurrent_runs: int = 1
    # How often the worker re-scans the store for cross-process enqueues + orphans. In-process
    # API enqueues wake it instantly via an event; this only bounds cross-process latency.
    poll_interval_seconds: float = 3.0
```

`config.yaml` gains:

```yaml
queue:
  max_concurrent_runs: 1
  poll_interval_seconds: 3
```

`max_concurrent_runs` is clamped to `>= 1` at read time.

### 6.2 Manifest changes — `run_store.py`

- `RunManifest.status` docstring/vocabulary becomes `queued | running | complete | halted`.
  `queued` replaces the old initial `pending`. (`_ACTIVE` becomes `("queued", "running")`.)
- Add `RunManifest.enqueued_at: Optional[str] = None` — microsecond-precision ISO timestamp,
  the primary FIFO sort key.
- `RunStore` gains a helper to list queued runs efficiently:
  `list_queued() -> list[str]` returning run_ids with `status == "queued"` sorted by
  `(enqueued_at, run_id)`, read from the cheap `summary.json` cache (fall back to `run.json`).
  `RunSummary` gains `enqueued_at` so the queue can be ordered without loading full manifests.
- Boot recovery needs interrupted runs: `list_by_status("running")` (same cached scan).

### 6.3 `engine.enqueue()` + wake event

```python
def enqueue(self, run_id: str) -> None:
    m = self.store.load(run_id)
    if m.status in ("complete", "halted"):   # never re-open a terminal run
        return
    m.status = "queued"
    m.enqueued_at = _now_micros()
    self.store.save(m)                        # durable + atomic; job is now safe
    self._wake.set()                          # instant pickup if a worker runs in this process
```

`self._wake` is an `asyncio.Event` created lazily on the running loop. `create_run()` no longer
implies execution; callers explicitly `enqueue()`. The old `launch()` (fire-and-forget
`create_task`) is **removed** — the worker owns execution now.

### 6.4 The worker drain loop — `engine.run_worker()`

An async loop, started once per process that holds the lease:

```python
async def run_worker(self):
    sem = asyncio.Semaphore(max(1, self.cfg.queue.max_concurrent_runs))
    inflight: set[str] = set()
    while True:
        for run_id in self.store.list_queued():
            if run_id in inflight:
                continue
            await sem.acquire()                       # blocks when at capacity
            inflight.add(run_id)
            task = asyncio.create_task(self._drain_one(run_id, sem, inflight))
            self._worker_tasks.add(task)              # strong ref; discard in done-cb
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.queue.poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()
```

`_drain_one` marks the run in-flight, `await execute(run_id)`, then releases the semaphore and
removes it from `inflight` in a `finally` (so a crash in one run never leaks a slot). `execute()`
already never raises for task-level failures and marks the run terminal itself. With
`max_concurrent_runs == 1` the semaphore serializes everything; `> 1` allows that many in flight.

### 6.5 The worker lease — cross-process single-drainer

A small `WorkerLease` wrapper (new `src/atom/workflow/lease.py`) over `fcntl.flock`:

```python
lease = WorkerLease(store.queue_dir / "worker.lock")
if lease.acquire():        # LOCK_EX | LOCK_NB — non-blocking
    try: ... run_worker() ...
    finally: lease.release()
else:
    # someone else (a server) is draining; do not start a second worker
```

- **Server:** acquires on lifespan startup; if acquired it is THE worker; if not (another
  server already holds it), it still serves the API + enqueues, but does not drain.
- **CLI standalone:** see 6.10.
- **Crash-safety:** `flock` is released by the OS when the holding process dies — no stale-lock
  cleanup needed. A run left `running` by the crash is recovered via 6.6, not via the lock.

### 6.6 Boot recovery — `engine.recover()`

**Invariant: `recover()` only ever runs while the caller holds the lease.** Otherwise it could
flip a run that another process is legitimately executing (`running`) back to `queued` and cause
a double-drain. Both call sites (server lifespan, CLI standalone) acquire the lease first.

Runs once, synchronously, immediately after the lease is acquired and *before* the worker loop
starts:

```python
def recover(self) -> None:
    for run_id in self.store.list_by_status("running"):
        m = self.store.load(run_id)
        m.status = "queued"                    # interrupted mid-flight → resume
        self._reset_interrupted_step(m)        # running tasks → pending; succeeded tasks kept
        self.store.save(m)
    # queued runs need no action — the worker will pick them up.
```

`_reset_interrupted_step`: for the step whose status is `running`, set any `running` task back to
`pending` (it will fully re-run) and recompute the step status; keep `succeeded` tasks as-is.

### 6.7 Step-level resume in `execute()`

`execute()` (`engine.py:142`) is modified so it is **resume-safe** — a small change to the step
loop (lines 169–188):

```python
for step_state, step_def in zip(manifest.steps, workflow.steps):
    if step_state.status == "complete":
        continue                                       # already done in a prior life — skip
    step_state.status = "running"; self.store.save(manifest)
    pending = [(ts, td) for ts, td in zip(step_state.tasks, step_def.tasks)
               if ts.status != "succeeded"]            # skip tasks that already succeeded
    await asyncio.gather(*[run_one(ts, td, step_def, step_state) for ts, td in pending],
                         return_exceptions=True)
    step_state.status = compute_step_status([t.status for t in step_state.tasks])  # all tasks
    ...
```

Because `execute()` already falls back to `load_workflow(manifest.workflow, home)` when
`self._defs` is empty (`engine.py:149`), resume works across a full process restart — the
workflow YAML and the inputs are both on disk. Resumed tasks reuse their original `thread_id`
(assigned once in `create_run`).

### 6.8 FastAPI lifespan wiring — `api/app.py`

`create_app` gains a `lifespan` context manager. **Lease first, then recover, then worker** —
recovery and draining only happen in the process that holds the lease:

```python
@asynccontextmanager
async def lifespan(app):
    holds = engine.lease.acquire()     # LOCK_EX | LOCK_NB
    if holds:
        engine.recover()               # safe: we are the sole drainer
        engine.start_worker()          # asyncio.create_task(engine.run_worker())
    yield
    if holds:
        await engine.stop_worker()     # cancel task; execute() requeues in-flight run → queued
        engine.lease.release()
```

A second server on the same `$ATOM_HOME` fails to acquire the lease, so it skips recovery and the
worker and serves read/enqueue only — no double-drain.

`serve` (`cli.py:298`) is unchanged — `uvicorn.run` drives the lifespan. On shutdown, `stop_worker`
cancels the in-flight run; `execute()` gains an explicit `except asyncio.CancelledError` branch
that sets the run **back to `queued`** (not `halted` — that is terminal and would strand it), so
the next startup resumes it at step granularity. A hard crash (no chance to run that branch)
leaves the run `running`, which boot recovery re-queues instead.

### 6.9 API `submit_run` change

`engine.create_run(...)` then `engine.enqueue(run_id)` (was `engine.launch(run_id)`). Returns
`{"run_id": ..., "status": "queued"}`. Still `async def`.

### 6.10 CLI `atom workflow run` change

Preserves today's blocking UX (enqueue, wait, print), now via the shared lane. A single
**progress-or-takeover** loop guarantees the run always makes progress no matter who (if anyone)
is draining. Unlike the server worker, the CLI executes **only its own run** — it never runs
another user's queued job:

```python
engine.create_run(wf, inputs, run_id, now)
engine.enqueue(run_id)
asyncio.run(_await_cli_run(engine, run_id))    # blocks until terminal, then print summary

async def _await_cli_run(engine, run_id):
    while True:
        m = engine.store.load(run_id)
        if m.status in ("complete", "halted"):
            return m                             # a server (or a prior takeover) finished it
        if engine.lease.acquire():               # no active drainer → we become it
            try:
                engine.recover()                 # rescue our own orphan (running→queued) + others
                if engine.store.load(run_id).status not in ("complete", "halted"):
                    await engine.execute(run_id) # run OUR run to terminal (step-level resume)
            finally:
                engine.lease.release()
            return engine.store.load(run_id)
        await asyncio.sleep(engine.cfg.queue.poll_interval_seconds)   # someone drains; wait + retry
```

**Why the loop, not a one-shot branch:** if a server holds the lease it drains our run and we
return when it goes terminal. If that server *dies* mid-run (or two standalone CLIs race), the
`flock` frees, our next `acquire()` succeeds, `recover()` flips our now-orphaned run back to
`queued`, and `execute()` resumes it. So the run can never get stranded. The CLI prints the same
summary as today once the run is terminal.

## Data flow

**Normal (server up):** POST → `enqueue` writes `queued` manifest + sets event → worker wakes,
sees a free slot, `execute()` → `running` → `complete`. Second POST during the first run stays
`queued` on disk until the slot frees. CLI submissions land in the same queue and are drained by
the server's worker (CLI polls).

**Crash mid-run:** run is `running` on disk when the process dies. Next `serve` startup →
`recover()` flips it to `queued` + resets its interrupted step → worker resumes it, skipping
completed steps/tasks. Queued-but-never-started runs are picked up untouched. Nothing lost.

**CLI standalone (no server):** enqueue → CLI grabs the lease → recovers orphans → executes
**its own** run to completion → releases. If a server starts meanwhile, its lease acquisition
fails until the CLI releases, so they never overlap. Two standalone CLIs: the first holds the
lease and runs its job; the second waits, retrying the lease, and takes over the moment the first
releases (or dies) — running its own job then. No job is stranded; none runs another's job.

## Concurrency & correctness invariants

- **At most `max_concurrent_runs` runs execute across the whole machine**, enforced by
  (a) the in-process `Semaphore` and (b) the single-drainer `flock` lease (only one process
  drains at a time). Default 1 ⇒ strictly one workflow at a time.
- **A job is durable the instant `enqueue()` returns** (atomic `run.json` write) — before any
  worker sees it.
- **`execute()` and `_run_task` still never raise** for task failures (unchanged contract); the
  worker's `finally` guarantees the semaphore slot and in-flight entry are always released.
- **Terminal runs are never re-opened** (`enqueue`/`recover` skip `complete`/`halted`).

## Error handling & edge cases

- **Worker task dies unexpectedly:** `_drain_one`'s `finally` frees the slot; the run's own
  terminal-state guarantee (belt-and-suspenders `except BaseException` at `engine.py:194`)
  ensures it doesn't stay `running`. A supervisor wrapper restarts `run_worker` if the loop
  itself throws, logging the error.
- **Corrupt/partial manifest during recovery scan:** `list_by_status` skips unreadable manifests
  (mirrors `RunStore.list`'s existing `except: continue`), so one bad run can't block recovery.
- **Workflow YAML changed between crash and resume:** resume uses the current on-disk
  definition; documented limitation (acceptable — the manifest still carries the original
  inputs and per-step progress).
- **Re-running a `running` task on resume** repeats that single task from scratch (task-level,
  not mid-task) — its side effects may duplicate. Accepted limitation; step-level resume already
  bounds this to one step. (LangGraph checkpoint-based mid-task resume is a possible future
  enhancement — thread_ids are stable — but is neither relied upon nor guaranteed in v1.)
- **Two servers, same `$ATOM_HOME`:** the lease ensures only one drains; the other serves
  read/enqueue only. Correct, no double-drain.

## Testing plan

Follow the existing engine test style (`tests/test_workflow_engine.py` — injects `store` and a
custom `launcher`; uses the `memory` checkpointer). New/updated tests:

- **`test_workflow_queue.py`** (new):
  - enqueue persists `status: "queued"` + `enqueued_at`; `list_queued` FIFO order.
  - worker with `max_concurrent_runs=1` runs two enqueued runs strictly sequentially (assert
    non-overlap via a per-run start/stop probe); with `=2` allows two in flight.
  - `recover()` flips a hand-written `running` manifest to `queued` and resets its interrupted
    step (running task → pending, succeeded task kept).
  - step-level resume: a manifest with step 0 `complete` + step 1 partially done runs only the
    unfinished tasks of step 1 (assert the completed step's task is not re-invoked, via a
    counting fake agent).
  - terminal runs are not re-enqueued/re-opened.
- **`test_workflow_lease.py`** (new): `WorkerLease` is mutually exclusive across two handles in
  the same process (second `acquire()` returns False); releases on `release()`.
- **`test_workflow_api.py`** (update): `submit_run` returns `status: "queued"`; a lifespan
  smoke test that the worker starts, drains an enqueued run to `complete`, and stops cleanly.
- **`test_workflow_cli.py`** (update): `workflow run` standalone (no server) still runs a
  workflow to completion via the lease-drain path and prints the summary.
- **`test_workflow_config.py`** (update): `QueueConfig` defaults + clamp (`max_concurrent_runs`
  floored at 1); `config.yaml` parses.

Run with `.venv/bin/python -m pytest` (NOT bare `pytest` — repo-root import requirement).

## Out of scope / YAGNI

- Priorities, per-workflow queues, cancellation-of-queued, reordering, max-queue-depth limits.
- A separate queue daemon / broker (Redis, Celery, RQ). The file store + flock is sufficient
  for single-flight and small N; revisit only if a multi-host deployment appears.
- Mid-task (checkpoint) resume. Step-level resume is the committed granularity.
- A persistent monotonic sequence counter — `(enqueued_at, run_id)` is sufficient FIFO.

## Accepted tradeoffs / follow-ups

- Best-effort FIFO (microsecond timestamp) rather than a strict global sequence.
- Resume repeats an interrupted step's in-flight task from scratch (bounded duplicate work).
- A standalone CLI executes only its own run; other queued jobs wait for a server (or another
  CLI) to drain them — durable, never lost, just deferred.
- Cross-process enqueue latency is bounded by `poll_interval_seconds` (in-process is instant).
