"""Workflow execution engine.

Orchestrates a run over its steps: steps run sequentially, tasks within a step run
concurrently (bounded by a semaphore), every task is a ``run_agent`` call bound to the run's
one shared workspace (existing-workspace mode) under its own thread id. A step progresses only
if every task succeeds; otherwise the run halts and later steps never run.
"""
from __future__ import annotations

import asyncio
import datetime
from typing import Awaitable, Callable, Optional

from atom.agent import PreparedModel
from atom.config.schema import AtomConfig
from atom.runtime import run_agent
from atom.workflow.observability import build_trace
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
        # Engine-owned runs dir (workflows/runs/**) is implicitly trusted: if the user has
        # restricted sandbox.allowed_workspace_roots, make sure it still includes the runs dir
        # so tasks can bind their run's shared workspace. An empty list means "allow any" and is
        # left untouched. Built once here (never mutates self.cfg in place).
        self._task_cfg = self._build_task_cfg(cfg)

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
        """Schedule execute() on the event loop (default asyncio.create_task)."""
        return self.launcher(self.execute(run_id))

    # ---- execution ----
    async def execute(self, run_id: str) -> RunManifest:
        manifest = self.store.load(run_id)
        workflow = self._defs.get(run_id) or load_workflow(manifest.workflow, self.cfg.home)
        manifest.status = "running"
        self.store.save(manifest)

        sem = asyncio.Semaphore(max(1, self.cfg.workflow.max_parallel))
        try:
            for step_state, step_def in zip(manifest.steps, workflow.steps):
                step_state.status = "running"
                self.store.save(manifest)

                async def run_one(ts: TaskState, td: TaskDef, sd: StepDef, ss: StepState):
                    async with sem:
                        await self._run_task(manifest, workflow, ss, sd, ts, td)

                await asyncio.gather(*[
                    run_one(ts, td, step_def, step_state)
                    for ts, td in zip(step_state.tasks, step_def.tasks)
                ])

                step_state.status = compute_step_status([t.status for t in step_state.tasks])
                self.store.save(manifest)
                if step_state.status != "complete":
                    manifest.status = "halted"
                    manifest.ended_at = _now()
                    self.store.save(manifest)
                    return manifest
        except Exception:
            # Belt-and-suspenders: _run_task catches per-task failures itself, but if some
            # unexpected exception still escapes the loop, never leave the run non-terminal.
            manifest.status = "halted"
            manifest.ended_at = _now()
            self.store.save(manifest)
            raise

        manifest.status = compute_run_status([s.status for s in manifest.steps])
        manifest.ended_at = _now()
        self.store.save(manifest)
        return manifest

    async def _run_task(
        self, manifest: RunManifest, workflow: WorkflowDef,
        step_state: StepState, step_def: StepDef, ts: TaskState, td: TaskDef,
    ) -> None:
        ts.status = "running"
        ts.started_at = _now()
        self.store.save(manifest)

        trace = build_trace(
            workflow=workflow.name, run_id=manifest.run_id,
            step_index=step_state.index, step_title=step_state.title, task_id=ts.id,
        )
        timeout = self.cfg.workflow.task_timeout_seconds or None
        try:
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
            )
            result = await (asyncio.wait_for(coro, timeout) if timeout else coro)
            self.store.save_chat(
                manifest.run_id, step_state.index, ts.id, serialize_messages(result.messages)
            )
            ts.status = "succeeded"
        except asyncio.TimeoutError:
            ts.status = "failed"
            ts.error = f"task exceeded {timeout}s timeout"
        except Exception as exc:  # noqa: BLE001 — any task failure halts the step
            ts.status = "failed"
            ts.error = f"{type(exc).__name__}: {exc}"
        ts.ended_at = _now()
        self.store.save(manifest)
