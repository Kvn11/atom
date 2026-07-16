"""Workflow execution engine.

Orchestrates a run over its steps: steps run sequentially, tasks within a step run
concurrently (bounded by a semaphore), every task is a ``run_agent`` call bound to the run's
one shared workspace (existing-workspace mode) under its own thread id. A step progresses only
if every task succeeds; otherwise the run halts and later steps never run.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

from atom.agent import PreparedModel
from atom.config.schema import AtomConfig
from atom.notes import ensure_vault
from atom.observability import build_lead_trace, build_provider
from atom.runtime import run_agent
from atom.streaming import StreamEmitter
from atom.workflow.events import RunEventBus, channel_key
from atom.workflow.lease import WorkerLease
from atom.workflow.run_store import (
    RunManifest, RunStore, StepState, TaskState, serialize_messages,
)
from atom.workflow.schema import (
    StepDef, TaskDef, WorkflowDef, load_workflow, render_task_prompt, resolve_inputs,
)
from atom.workflow.status import compute_run_status, compute_step_status

PreparedProvider = Callable[[TaskDef, StepDef, WorkflowDef], Optional[PreparedModel]]


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _now_micros() -> str:
    return datetime.datetime.now().isoformat(timespec="microseconds")


def task_thread_id(run_id: str, step_index: int, task_id: str) -> str:
    return f"{run_id}:s{step_index}:{task_id}"


class WorkflowEngine:
    def __init__(
        self,
        cfg: AtomConfig,
        *,
        store: RunStore | None = None,
        prepared_provider: PreparedProvider | None = None,
        profile: str | None = None,
    ):
        self.cfg = cfg
        self.store = store or RunStore(cfg.home)
        self.prepared_provider = prepared_provider
        self.profile = profile or cfg.defaults.agent
        self._defs: dict[str, WorkflowDef] = {}
        # Engine-owned runs dir (workflows/runs/**) is implicitly trusted: if the user has
        # restricted sandbox.allowed_workspace_roots, make sure it still includes the runs dir
        # so tasks can bind their run's shared workspace. An empty list means "allow any" and is
        # left untouched. Built once here (never mutates self.cfg in place).
        self._task_cfg = self._build_task_cfg(cfg)
        self.bus = RunEventBus(
            max_chars=cfg.streaming.accumulator_max_chars,
            queue_max=cfg.streaming.subscriber_queue_max,
            retain_closed=cfg.streaming.retain_closed,
            heartbeat_seconds=cfg.streaming.heartbeat_seconds,
        )
        # --- durable-queue worker state ---
        self.lease = WorkerLease(self.store.queue_dir / "worker.lock")
        self._wake = asyncio.Event()
        self._inflight: set[str] = set()
        self._drain_failures: dict[str, int] = {}   # run_id -> consecutive pre-terminal drain failures
        self._quarantine: set[str] = set()          # run_ids that hit max_drain_attempts (skip until restart)
        self._worker_tasks: set[asyncio.Task] = set()
        self._worker_loop_task: asyncio.Task | None = None
        self._stopping = False
        # Build the observability provider once, before any run (logs its own status).
        self.obs_provider = build_provider(cfg)

    def _build_task_cfg(self, cfg: AtomConfig) -> AtomConfig:
        if not cfg.sandbox.allowed_workspace_roots:
            return cfg
        augmented = cfg.model_copy(deep=True)
        runs_dir = str(self.store.runs_dir)
        if runs_dir not in augmented.sandbox.allowed_workspace_roots:
            augmented.sandbox.allowed_workspace_roots.append(runs_dir)
        return augmented

    # ---- setup ----
    def create_run(
        self, workflow: WorkflowDef, inputs: dict, run_id: str, created_at: str | None = None
    ) -> RunManifest:
        resolved = resolve_inputs(workflow, inputs)          # raises MissingInputError
        steps: list[StepState] = []
        for i, step in enumerate(workflow.steps):
            tasks = [
                TaskState(
                    id=t.id, thread_id=task_thread_id(run_id, i, t.id),
                    model=t.model, thinking=t.thinking,
                )
                for t in step.tasks
            ]
            steps.append(StepState(index=i, title=step.title, tasks=tasks))
        manifest = RunManifest(
            run_id=run_id, workflow=workflow.name, inputs=resolved,
            created_at=created_at or _now(),
            workspace_path=str(self.store.workspace_dir(run_id)),
            uploads_path=str(self.store.uploads_dir(run_id)),
            steps=steps,
        )
        self._defs[run_id] = workflow
        return self.store.create(manifest)

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

    def recover(self) -> None:
        """Re-queue runs left non-terminal by a crash/shutdown so the worker resumes them.
        MUST be called only while holding the worker lease (sole-drainer guarantee)."""
        for run_id in self.store.interrupted_run_ids():
            try:
                m = self.store.load(run_id)
            except Exception:  # noqa: BLE001 — a corrupt manifest must not block recovery
                logger.exception("recover: failed to load run %s; skipping", run_id)
                continue
            if m.status in ("complete", "halted"):
                continue
            # NOTE: a "queued" run is intentionally NOT skipped here. RunStore.save() writes
            # run.json then summary.json as two separate atomic replaces; a crash between them
            # during enqueue() leaves run.json=queued but summary.json stale (pending/running).
            # The interrupted scan (summary-based) surfaces such a run; re-saving it below
            # re-syncs the summary so the worker's queued scan can see it (else it strands).
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

    # ---- worker (drainer) ----
    def start_worker(self) -> "asyncio.Task":
        """Start the background drain loop on the current event loop. Idempotent-ish: assumes
        the caller holds the lease (see the API lifespan / CLI await_run)."""
        if self._worker_loop_task is not None and not self._worker_loop_task.done():
            return self._worker_loop_task
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
                    if run_id in self._quarantine:
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
            # run_worker's scan is a snapshot (self.store.queued_run_ids() materializes a list);
            # a run already terminalized by an earlier drain in the SAME snapshot can resurface
            # here (its _inflight entry was already cleared) before the scan is retaken. Skip it
            # instead of re-running execute() -- which would crash (its cached WorkflowDef was
            # already popped after the first pass) and clobber a "complete" status with "halted".
            if self.store.load(run_id).status in ("complete", "halted"):
                return
            await self.execute(run_id)
            self._drain_failures.pop(run_id, None)   # progress made -> reset the failure count
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — execute() normally terminalizes the run itself; reaching
            # here means it failed BEFORE writing a terminal status (e.g. an unreadable run.json),
            # leaving the run queued and re-pickable. Bound the retries so a poison run can't hot-loop.
            n = self._drain_failures.get(run_id, 0) + 1
            self._drain_failures[run_id] = n
            if n >= max(1, self.cfg.queue.max_drain_attempts):
                self._quarantine.add(run_id)
                logger.error(
                    "worker: run %s failed to drain %d× without terminalizing (e.g. unreadable "
                    "run.json); quarantining -- it will NOT be retried until restart", run_id, n,
                )
            else:
                logger.exception("worker: run %s crashed in execute() (attempt %d)", run_id, n)
        finally:
            self._inflight.discard(run_id)
            sem.release()
            if not self._stopping:
                self._wake.set()                    # a slot freed -> rescan for more work

    async def await_run(self, run_id: str) -> RunManifest:
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

    # ---- execution ----
    async def execute(self, run_id: str) -> RunManifest:
        try:
            manifest = self.store.load(run_id)
        except Exception:
            logger.exception("workflow run %s: failed to load manifest", run_id)
            raise
        try:
            workflow = self._defs.get(run_id) or load_workflow(manifest.workflow, self.cfg.home)
            manifest.status = "running"
            self.store.save(manifest)

            notes_binding = None
            if workflow.notes.enabled:
                try:
                    notes_binding = ensure_vault(self.cfg.home, workflow.name, workflow.notes)
                except Exception as exc:  # noqa: BLE001 — notes setup failure halts the run cleanly
                    if manifest.steps and manifest.steps[0].tasks:
                        manifest.steps[0].tasks[0].status = "failed"
                        manifest.steps[0].tasks[0].error = (
                            f"persistent notes setup failed: {type(exc).__name__}: {exc}")
                        manifest.steps[0].status = "failed"
                    manifest.status = "halted"
                    manifest.ended_at = _now()
                    self.store.save(manifest)
                    return manifest

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

            manifest.status = compute_run_status([s.status for s in manifest.steps])
            manifest.ended_at = _now()
            self.store.save(manifest)
            return manifest
        except asyncio.CancelledError:
            # Worker stop / Ctrl-C: put the run back on the queue so the next startup resumes it
            # (step-level). Do NOT mark it halted — halted is terminal and would strand the run.
            manifest.status = "queued"
            try:
                self.store.save(manifest)
            except Exception:  # noqa: BLE001
                logger.exception("workflow run %s: failed to requeue on cancel", run_id)
            raise
        except BaseException:
            # Belt-and-suspenders: _run_task never raises (it guards its own failures), but
            # this also covers the load_workflow fallback above, a save() I/O error, and any
            # other real error — none of those may leave the run non-terminal. CancelledError
            # is handled by the earlier except-clause, not here; we still re-raise so real
            # errors keep propagating.
            manifest.status = "halted"
            manifest.ended_at = _now()
            try:
                self.store.save(manifest)
            except Exception:
                logger.exception("workflow run %s: failed to persist halted status", run_id)
            raise
        finally:
            self._defs.pop(run_id, None)
            # Flush the active backend's trace queue before the process can exit, so the run's
            # final batch is uploaded and downloadable. No-op for NullProvider; a flush failure
            # must never mask a propagating exception.
            try:
                self.obs_provider.flush()
            except Exception:  # noqa: BLE001
                pass

    async def _run_task(
        self, manifest: RunManifest, workflow: WorkflowDef,
        step_state: StepState, step_def: StepDef, ts: TaskState, td: TaskDef,
        notes: "object | None" = None,
    ) -> None:
        """Runs one task. Must NEVER raise: this runs concurrently with sibling tasks under
        asyncio.gather(), and one task's failure (including its own store.save() I/O errors)
        escaping here would propagate out of gather and abandon still-running siblings, which
        would keep mutating the shared manifest after the run has already moved on.
        """
        key = channel_key(manifest.run_id, step_state.index, ts.id)
        emitter: "StreamEmitter | None" = None
        timeout: Optional[float] = None
        try:
            ts.status = "running"
            ts.started_at = _now()
            self.store.save(manifest)

            trace = build_lead_trace(
                workflow=workflow.name, run_id=manifest.run_id,
                step_index=step_state.index, step_title=step_state.title, task_id=ts.id,
                session_id=ts.thread_id, obs=self.cfg.observability,
            )
            t = self.cfg.workflow.task_timeout_seconds
            # 0 or negative explicitly disables the per-task timeout (documented sentinel;
            # see WorkflowConfig.task_timeout_seconds and the workflow: block in config.yaml).
            timeout = t if (isinstance(t, (int, float)) and t > 0) else None
            # Both of these can raise (bad Jinja template / a misbehaving prepared_provider);
            # keep them inside the try so a bad task fails just that task instead of escaping
            # _run_task and wedging the whole run.
            prompt = render_task_prompt(td, manifest.inputs)
            prepared = self.prepared_provider(td, step_def, workflow) if self.prepared_provider else None
            if self.cfg.streaming.enabled:
                emitter = StreamEmitter(
                    lambda e, k=key: self.bus.publish(k, e),
                    coalesce_ms=self.cfg.streaming.coalesce_ms,
                    coalesce_chars=self.cfg.streaming.coalesce_chars,
                )
            coro = run_agent(
                prompt, config=self._task_cfg, profile=self.profile,
                override_model=td.model, override_thinking=td.thinking,
                workspace=manifest.workspace_path, uploads=manifest.uploads_path,
                thread_id=ts.thread_id, trace=trace, prepared=prepared,
                notes=notes.as_prompt_ctx() if notes else None,
                on_event=(emitter.emit if emitter else None),
                obs_provider=self.obs_provider,
            )
            result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            self.store.save_chat(
                manifest.run_id, step_state.index, ts.id, serialize_messages(result.messages)
            )
            presented = (result.state or {}).get("artifacts", [])
            ts.artifacts = self.store.capture_artifacts(
                manifest.run_id, step_state.index, ts.id, presented,
            )
            ts.status = "succeeded"
        except asyncio.CancelledError:
            ts.status = "failed"
            ts.error = "cancelled"
            ts.ended_at = _now()
            try:
                self.store.save(manifest)
            except Exception:
                pass  # best-effort: cancellation cleanup must not mask the cancellation
            raise
        except asyncio.TimeoutError:
            ts.status = "failed"
            ts.error = f"task exceeded {timeout}s timeout"
        except Exception as exc:  # noqa: BLE001 — any task failure (including a save() I/O
            # error above) fails just this task; it must never escape and wedge a sibling.
            ts.status = "failed"
            ts.error = f"{type(exc).__name__}: {exc}"
        ts.ended_at = _now()
        if emitter is not None:
            try:
                await emitter.aclose()
            except Exception:
                pass
            try:
                await self.bus.close(key, error=(ts.error if ts.status == "failed" else None))
            except Exception:
                pass
        try:
            self.store.save(manifest)
        except Exception:
            pass  # best-effort: this method must never raise
