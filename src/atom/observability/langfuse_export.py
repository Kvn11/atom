"""Download a workflow run's LangFuse traces to disk for offline evaluation.

Read-only. LangFuse groups by session, and atom's session == the whole run_id, so one session
list returns every trace of the run — task LEAD traces and sub-agent traces as siblings (unlike
LangSmith, where sub-agents nest under the lead root). The completeness oracle counts lead traces
only (== executed tasks); the envelope's ``roots`` holds all traces, each hydrated with its
observation tree.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from typing import Any, Callable

from atom.observability.export import (
    ExportResult,
    _TERMINAL,
    build_envelope,
    expected_root_count,
    resolve_run_ids,    # noqa: F401 — dispatched CLI/API import this from here too
)
from atom.workflow.run_store import RunStore


def _require_keys() -> None:
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        raise RuntimeError(
            "LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are not set — cannot export from LangFuse"
        )


def _default_client() -> Any:
    from langfuse import Langfuse
    return Langfuse()


def _langfuse_sdk_version() -> str | None:
    try:
        import langfuse
        return getattr(langfuse, "__version__", None)
    except Exception:  # noqa: BLE001
        return None


def _as_dict(obj: Any) -> dict:
    """Coerce a LangFuse SDK object (or fake) to a plain, JSON-safe dict.

    Prefers ``model_dump(mode="json")`` so datetime/UUID-typed fields (real LangFuse trace
    objects carry them) come back as JSON-native values before they flow into
    ``json.dumps(envelope, ...)`` in ``export_run``/``export_task`` — mirroring the LangSmith
    exporter's ``model_dump(mode="json")`` (see export.py). Falls back to a no-arg call for
    objects whose ``model_dump``/``dict`` don't accept the ``mode`` kwarg.
    """
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            return dump()
    d = getattr(obj, "dict", None)
    if callable(d):
        return d()
    return dict(vars(obj))


def _item_id(item: Any) -> str:
    val = getattr(item, "id", None)
    if val is not None:
        return val
    return item["id"] if isinstance(item, dict) else None


def fetch_session_traces(client: Any, run_id: str) -> list[dict]:
    """List every trace in the run's session and hydrate each with its observation tree.

    Pages until an empty page is returned (works for the real paginated API and simple fakes).
    """
    trees: list[dict] = []
    page = 1
    while True:
        resp = client.api.trace.list(session_id=run_id, page=page)
        items = list(getattr(resp, "data", resp) or [])
        if not items:
            break
        for it in items:
            full = client.api.trace.get(_item_id(it), fields="core,io,observations")
            trees.append(_as_dict(full))
        page += 1
    return trees


def _metadata(trace: dict) -> dict:
    md = trace.get("metadata")
    return md if isinstance(md, dict) else {}


def _is_lead(trace: dict) -> bool:
    md = _metadata(trace)
    if "agent_role" in md:
        return md["agent_role"] == "lead"
    return not md.get("is_subagent", False)


def _lead_count(traces: list[dict]) -> int:
    return sum(1 for t in traces if _is_lead(t))


def export_run(
    home: str | None,
    run_id: str,
    *,
    project: str | None = None,          # unused for LangFuse; kept for signature parity
    client: Any | None = None,
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ExportResult:
    """Download ``run_id``'s LangFuse traces to ``runs/<run_id>/export.json``.

    Polls until #lead-traces matches #executed tasks (local manifest) or ``poll_timeout`` elapses.
    Writes nothing when no traces are found.
    """
    store = RunStore(home)
    manifest = store.load(run_id)                        # FileNotFoundError if unknown locally
    _require_keys()

    client = client or _default_client()
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    expected = expected_root_count(manifest)
    deadline = monotonic() + poll_timeout
    traces: list[dict] = []
    while True:
        traces = fetch_session_traces(client, run_id)
        if expected == 0 or _lead_count(traces) >= expected:
            break
        if monotonic() >= deadline:
            break
        sleep(poll_interval)

    fetched = _lead_count(traces)
    if not traces:
        return ExportResult(run_id=run_id, path="", complete=False,
                            expected_roots=expected, fetched_roots=0)

    complete = fetched >= expected
    envelope = build_envelope(
        run_id, manifest.workflow, project or "", manifest, traces,
        complete=complete, expected=expected, fetched=fetched, now=now(),
        provider="langfuse", sdk_version=_langfuse_sdk_version(),
    )
    path = store.run_dir(run_id) / "export.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("export.json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return ExportResult(run_id=run_id, path=str(path), complete=complete,
                        expected_roots=expected, fetched_roots=fetched)


def export_task(
    home: str | None,
    run_id: str,
    step_index: int,
    task_id: str,
    *,
    project: str | None = None,          # unused for LangFuse; kept for signature parity
    client: Any | None = None,
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ExportResult:
    """Download one task's LangFuse traces (its lead + sub-agent traces) to
    ``runs/<run_id>/exports/s<step>__<task>.json``. The task must be terminal.
    """
    store = RunStore(home)
    manifest = store.load(run_id)

    step = next((s for s in manifest.steps if s.index == step_index), None)
    if step is None:
        raise KeyError(f"step {step_index} not found in run {run_id!r}")
    task = next((t for t in step.tasks if t.id == task_id), None)
    if task is None:
        raise KeyError(f"task {task_id!r} not found in step {step_index} of run {run_id!r}")
    if task.status not in _TERMINAL:
        raise ValueError(f"task {task_id!r} has not completed (status: {task.status})")
    _require_keys()

    client = client or _default_client()
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    def _for_task(traces: list[dict]) -> list[dict]:
        return [t for t in traces if _metadata(t).get("task_id") == task_id]

    deadline = monotonic() + poll_timeout
    selected: list[dict] = []
    while True:
        selected = _for_task(fetch_session_traces(client, run_id))
        if _lead_count(selected) >= 1:
            break
        if monotonic() >= deadline:
            break
        sleep(poll_interval)

    fetched = _lead_count(selected)
    if not selected:
        return ExportResult(run_id=run_id, path="", complete=False,
                            expected_roots=1, fetched_roots=0, task_id=task_id)

    envelope = build_envelope(
        run_id, manifest.workflow, project or "", manifest, selected,
        complete=True, expected=1, fetched=fetched, now=now(),
        task_id=task_id, session_id=task.thread_id,
        provider="langfuse", sdk_version=_langfuse_sdk_version(),
    )
    path = store.task_export_path(run_id, step_index, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return ExportResult(run_id=run_id, path=str(path), complete=True,
                        expected_roots=1, fetched_roots=fetched, task_id=task_id)
