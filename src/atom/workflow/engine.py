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
from typing import Awaitable, Callable, Optional

from langchain_core.tracers.langchain import wait_for_all_tracers

logger = logging.getLogger(__name__)

from atom.agent import PreparedModel
from atom.config.schema import AtomConfig
from atom.notes import ensure_vault
from atom.observability import apply_observability_env, build_lead_trace, tracing_active
from atom.runtime import run_agent
from atom.workflow.run_store import (
    RunManifest, RunStore, StepState, TaskState, serialize_messages,
)
from atom.workflow.schema import (
    StepDef, TaskDef, WorkflowDef, load_workflow, render_task_prompt, resolve_inputs,
)
from atom.workflow.status import compute_run_status, compute_step_status

PreparedProvider = Callable[[TaskDef, StepDef, WorkflowDef], Optional[PreparedModel]]
Launcher = Callable[[Awaitable], object]


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def task_thread_id(run_id: str, step_index: int, task_id: str) -> str:
    return f"{run_id}:s{step_index}:{task_id}"


class WorkflowEngine:
    def __init__(
        self,
        cfg: AtomConfig,
        *,
        store: RunStore | None = None,
        prepared_provider: PreparedProvider | None = None,
        launcher: Launcher | None = None,
        profile: str | None = None,
    ):
        self.cfg = cfg
        self.store = store or RunStore(cfg.home)
        self.prepared_provider = prepared_provider
        self.launcher: Launcher = launcher or asyncio.create_task
        self.profile = profile or cfg.defaults.agent
        self._defs: dict[str, WorkflowDef] = {}
        # Strong references to fire-and-forget launch() tasks: keeps them from being GC'd
        # mid-run and lets _on_task_done retrieve their exceptions (see launch()).
        self._tasks: dict[asyncio.Task, str] = {}
        # Engine-owned runs dir (workflows/runs/**) is implicitly trusted: if the user has
        # restricted sandbox.allowed_workspace_roots, make sure it still includes the runs dir
        # so tasks can bind their run's shared workspace. An empty list means "allow any" and is
        # left untouched. Built once here (never mutates self.cfg in place).
        self._task_cfg = self._build_task_cfg(cfg)
        # Map observability config -> LANGSMITH_* env once, before any run (idempotent).
        status = apply_observability_env(cfg)
        if status.active:
            logger.info("observability: tracing active -> project %r", status.project)
        elif status.reason == "enabled-but-no-api-key":
            logger.warning(
                "observability: observability.enabled but LANGSMITH_API_KEY missing "
                "-- traces will NOT be uploaded"
            )

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
            workspace_path=str(self.store.workspace_dir(run_id)), steps=steps,
        )
        self._defs[run_id] = workflow
        return self.store.create(manifest)

    def launch(self, run_id: str):
        """Schedule execute() on the event loop (default asyncio.create_task).

        The caller (e.g. the API's submit_run) discards the return value — this is
        fire-and-forget. So we keep our own strong reference to the Task (self._tasks;
        otherwise it could be garbage-collected mid-run) and attach a done-callback that
        retrieves any exception it raised (otherwise it's logged as "never retrieved" and
        the run silently never reaches a terminal state).
        """
        task = self.launcher(self.execute(run_id))
        if isinstance(task, asyncio.Task):
            self._tasks[task] = run_id
            task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: "asyncio.Task") -> None:
        run_id = self._tasks.pop(task, None)
        if task.cancelled():
            return
        exc = task.exception()  # retrieves it, so it's not "never retrieved"
        if exc is None or not run_id:
            return
        try:
            manifest = self.store.load(run_id)
            if manifest.status not in ("complete", "halted"):
                manifest.status = "halted"
                manifest.ended_at = _now()
                self.store.save(manifest)
        except Exception:
            pass  # best-effort: this callback must never raise

    # ---- execution ----
    async def execute(self, run_id: str) -> RunManifest:
        manifest = self.store.load(run_id)
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
                step_state.status = "running"
                self.store.save(manifest)

                async def run_one(ts: TaskState, td: TaskDef, sd: StepDef, ss: StepState):
                    async with sem:
                        await self._run_task(manifest, workflow, ss, sd, ts, td, notes=notes_binding)

                await asyncio.gather(*[
                    run_one(ts, td, step_def, step_state)
                    for ts, td in zip(step_state.tasks, step_def.tasks)
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
        except BaseException:
            # Belt-and-suspenders: _run_task never raises (it guards its own failures), but
            # this also covers the load_workflow fallback above, a save() I/O error, and
            # asyncio.CancelledError (e.g. server shutdown) — none of those may leave the run
            # non-terminal. Note: except BaseException (not Exception) so CancelledError is
            # covered too; we still re-raise so cancellation/real errors keep propagating.
            manifest.status = "halted"
            manifest.ended_at = _now()
            try:
                self.store.save(manifest)
            except Exception:
                pass  # best-effort
            raise
        finally:
            self._defs.pop(run_id, None)
            # Flush LangSmith's background trace queue before the process can exit, so the run's
            # final batch is guaranteed uploaded and downloadable. No-op when tracing is off.
            if tracing_active():
                try:
                    wait_for_all_tracers()
                except Exception:  # noqa: BLE001 — a flush failure must never mask a propagating exception
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
            coro = run_agent(
                prompt, config=self._task_cfg, profile=self.profile,
                override_model=td.model, override_thinking=td.thinking,
                workspace=manifest.workspace_path, thread_id=ts.thread_id,
                trace=trace, prepared=prepared,
                notes=notes.as_prompt_ctx() if notes else None,
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
        except asyncio.TimeoutError:
            ts.status = "failed"
            ts.error = f"task exceeded {timeout}s timeout"
        except Exception as exc:  # noqa: BLE001 — any task failure (including a save() I/O
            # error above) fails just this task; it must never escape and wedge a sibling.
            ts.status = "failed"
            ts.error = f"{type(exc).__name__}: {exc}"
        ts.ended_at = _now()
        try:
            self.store.save(manifest)
        except Exception:
            pass  # best-effort: this method must never raise
