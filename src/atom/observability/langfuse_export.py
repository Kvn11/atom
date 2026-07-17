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


def _resolve_keys(cfg: Any) -> tuple[str | None, str | None, str | None]:
    """(public, secret, host) from config.yaml with LANGFUSE_* env fallback, mirroring the
    live-tracing path so a config-only user can both trace AND export. cfg=None -> env only."""
    if cfg is None:
        return (os.environ.get("LANGFUSE_PUBLIC_KEY"),
                os.environ.get("LANGFUSE_SECRET_KEY"),
                os.environ.get("LANGFUSE_HOST"))
    from atom.observability.provider import resolve_langfuse_keys
    return resolve_langfuse_keys(cfg.observability)


def _require_keys(cfg: Any = None) -> None:
    public, secret, _ = _resolve_keys(cfg)
    if not (public and secret):
        raise RuntimeError(
            "LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are not set (and not in observability.langfuse) "
            "— cannot export from LangFuse"
        )


def _default_client(cfg: Any = None) -> Any:
    from langfuse import Langfuse
    public, secret, host = _resolve_keys(cfg)
    if public and secret:
        return Langfuse(public_key=public, secret_key=secret, host=host)
    return Langfuse()


def _langfuse_sdk_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("langfuse")
    except Exception:  # noqa: BLE001
        return None


def _as_dict(obj: Any) -> dict:
    """Coerce a LangFuse SDK object (or fake) to a plain, JSON-safe dict.

    The result flows straight into ``json.dumps(envelope, ...)`` in ``export_run``/``export_task``,
    so every value must be JSON-native. Two object shapes matter, and they need different handling:

    - **pydantic v2** (e.g. a fake, or any v2 model): ``model_dump(mode="json")`` converts
      datetime/UUID fields to strings. Tried first.
    - **pydantic v1** (the REAL langfuse object): ``client.api.trace.get`` returns a Fern-generated
      ``TraceWithFullDetails`` built on ``pydantic.v1.BaseModel``, which has NO ``model_dump`` and
      whose ``.dict()`` keeps NATIVE ``datetime`` values (``timestamp``, observation start/end) —
      those blow up ``json.dumps`` with ``TypeError: Object of type datetime is not JSON
      serializable``. Its ``.json()`` DOES emit ISO strings, so round-tripping ``json.loads(.json())``
      yields a JSON-safe dict. This is the path real exports take; a no-arg ``model_dump()``/``.dict()``
      fallback would silently reintroduce the datetime and break the whole export.
    """
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            return dump(mode="json")
        except TypeError:
            pass                        # v1-style dump without a `mode` kwarg -> try .json() below
    to_json = getattr(obj, "json", None)
    if callable(to_json):
        try:
            return json.loads(to_json())        # pydantic v1: ISO datetimes, JSON-safe
        except (TypeError, ValueError):
            pass
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
            full = client.api.trace.get(_item_id(it))
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


def _lead_identities(traces: list[dict]) -> set[tuple]:
    """Distinct ``(step_index, task_id)`` over LEAD traces — the SET of tasks that produced a lead.

    Completeness must be judged on distinct task identities, not the raw lead-trace count: crash
    recovery re-runs a non-succeeded task (engine.execute), emitting a SECOND lead trace with the
    same ``(step_index, task_id)``. A raw count would let that duplicate satisfy ``>= expected``
    while a DIFFERENT task's lead is still absent (ingestion lag), writing a ``complete`` export
    that silently omits a whole task. Counting identities requires every executed task to appear.
    """
    return {
        (_metadata(t).get("step_index"), _metadata(t).get("task_id"))
        for t in traces if _is_lead(t)
    }


def export_run(
    home: str | None,
    run_id: str,
    *,
    project: str | None = None,          # unused for LangFuse; kept for signature parity
    cfg: Any = None,                     # optional AtomConfig -> resolve keys from config.yaml
    client: Any | None = None,
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ExportResult:
    """Download ``run_id``'s LangFuse traces to ``runs/<run_id>/export.json``.

    Polls until #lead-traces matches #executed tasks (local manifest) or ``poll_timeout`` elapses.
    Writes nothing when no LEAD traces are found (matching the LangSmith exporter's parity).
    """
    store = RunStore(home)
    manifest = store.load(run_id)                        # FileNotFoundError if unknown locally
    _require_keys(cfg)

    client = client or _default_client(cfg)
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    expected = expected_root_count(manifest)
    deadline = monotonic() + poll_timeout
    traces: list[dict] = []
    while True:
        traces = fetch_session_traces(client, run_id)
        if expected == 0 or len(_lead_identities(traces)) >= expected:
            break
        if monotonic() >= deadline:
            break
        sleep(poll_interval)

    # Count DISTINCT (step, task) lead identities, not raw lead traces, so a duplicate lead from a
    # crash-recovered re-run can't mask a genuinely missing task (see _lead_identities).
    fetched = len(_lead_identities(traces))
    # Guard on the lead count, not on `traces` being empty: a fetched session may contain only
    # sub-agent traces (lead not yet uploaded), which must NOT be written as a bogus zero-lead
    # export — matching export.py's `if fetched == 0`.
    if fetched == 0:
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
    cfg: Any = None,                     # optional AtomConfig -> resolve keys from config.yaml
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
    _require_keys(cfg)

    client = client or _default_client(cfg)
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    def _for_task(traces: list[dict]) -> list[dict]:
        # Scope by BOTH task_id and step_index: task ids are unique only within a step, so a task
        # id reused across steps would otherwise pull in the other step's lead + sub-agent traces.
        return [
            t for t in traces
            if _metadata(t).get("task_id") == task_id
            and _metadata(t).get("step_index") == step_index
        ]

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
    # Guard on the LEAD count: a match set with only a sub-agent trace (lead not yet uploaded)
    # must not be written as `complete` with zero lead roots.
    if fetched == 0:
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
