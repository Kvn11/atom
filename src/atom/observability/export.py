"""Download a workflow run's LangSmith traces to disk for offline evaluation.

The exporter is read-only: it fetches a run's root runs by the run-wide ``run_id`` metadata
(a run spans one thread per task, so ``session_id`` would only capture one task), hydrates each
root's full child tree (sub-agent + per-LLM-call runs, with thinking blocks intact), and writes a
thin envelope around the verbatim LangSmith ``Run`` dicts. The local run manifest is the completeness
oracle: ``#root runs`` should equal the number of tasks that actually executed.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

from atom.workflow.run_store import RunManifest, RunStore

_EXECUTED = ("running", "succeeded", "failed")


@dataclass
class ExportResult:
    run_id: str
    path: str            # where export.json was written ("" when nothing was exported)
    complete: bool       # fetched_roots >= expected_roots
    expected_roots: int
    fetched_roots: int


def expected_root_count(manifest: RunManifest) -> int:
    """How many lead-task root runs LangSmith should hold for this run.

    One lead root per task that reached execution; sub-agents nest under their lead (not extra roots),
    and a pending/never-ran task (e.g. after a halt) emits no trace.
    """
    return sum(1 for s in manifest.steps for t in s.tasks if t.status in _EXECUTED)


def build_envelope(
    run_id: str, workflow: str, project: str, manifest: RunManifest, roots: list[dict],
    *, complete: bool, expected: int, fetched: int, now: str,
) -> dict:
    """The on-disk export.json: a thin, self-describing wrapper around the raw LangSmith trees."""
    import langsmith

    return {
        "run_id": run_id,
        "workflow": workflow,
        "project": project,
        "exported_at": now,
        "langsmith_sdk": getattr(langsmith, "__version__", None),
        "complete": complete,
        "expected_roots": expected,
        "fetched_roots": fetched,
        "atom_manifest": manifest.model_dump(mode="json"),
        "roots": roots,
    }


def _default_client() -> Any:
    from langsmith import Client
    return Client()


def fetch_run_tree(client: Any, project: str, run_id: str) -> list[dict]:
    """Fetch a run's root runs (by run_id metadata) and hydrate each full child tree.

    Sub-agents nest under their lead root, so ``load_child_runs=True`` brings back the whole
    lead + sub-agent + per-LLM-call tree (with thinking blocks) for each root.
    """
    flt = f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))'
    roots = list(client.list_runs(project_name=project, is_root=True, filter=flt))
    trees: list[dict] = []
    for r in roots:
        full = client.read_run(r.id, load_child_runs=True)
        trees.append(full.model_dump(mode="json"))
    return trees


def export_run(
    home: str | None,
    run_id: str,
    *,
    project: str | None = None,
    client: Any | None = None,
    poll_timeout: float = 30.0,
    poll_interval: float = 2.0,
    now: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> ExportResult:
    """Download ``run_id``'s LangSmith trace tree to ``runs/<run_id>/export.json``.

    Polls until the number of fetched roots matches the number of executed tasks (from the local
    manifest) or ``poll_timeout`` elapses, absorbing LangSmith's async-ingestion lag. Writes nothing
    when no traces are found (returns ``fetched_roots == 0``, ``path == ""``).
    """
    if not project:
        raise ValueError("no LangSmith project — set observability.project or pass project=")
    store = RunStore(home)
    manifest = store.load(run_id)          # FileNotFoundError if the run is unknown locally
    if not os.environ.get("LANGSMITH_API_KEY"):
        raise RuntimeError("LANGSMITH_API_KEY is not set — cannot export from LangSmith")

    client = client or _default_client()
    now = now or (lambda: datetime.datetime.now().isoformat(timespec="seconds"))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic

    expected = expected_root_count(manifest)
    deadline = monotonic() + poll_timeout
    roots: list[dict] = []
    while True:
        roots = fetch_run_tree(client, project, run_id)
        if expected == 0 or len(roots) >= expected:
            break
        if monotonic() >= deadline:
            break
        sleep(poll_interval)

    fetched = len(roots)
    if fetched == 0:
        return ExportResult(run_id=run_id, path="", complete=False,
                            expected_roots=expected, fetched_roots=0)

    complete = fetched >= expected
    envelope = build_envelope(
        run_id, manifest.workflow, project, manifest, roots,
        complete=complete, expected=expected, fetched=fetched, now=now(),
    )
    path = store.run_dir(run_id) / "export.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("export.json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    os.replace(tmp, path)                  # atomic, matching RunStore.save
    return ExportResult(run_id=run_id, path=str(path), complete=complete,
                        expected_roots=expected, fetched_roots=fetched)


def resolve_run_ids(
    home: str | None, *, run_id: str | None = None,
    latest: str | None = None, all_workflow: str | None = None,
) -> list[str]:
    """Resolve exactly one selector to run ids. ``--latest`` -> newest matching run; ``--all`` -> all."""
    provided = [x for x in (run_id, latest, all_workflow) if x]
    if len(provided) != 1:
        raise ValueError("provide exactly one of: <run_id>, --latest <workflow>, --all <workflow>")
    if run_id:
        return [run_id]
    name = latest or all_workflow
    matches = [m.run_id for m in RunStore(home).list() if m.workflow == name]  # newest-first
    if not matches:
        raise ValueError(f"no runs found for workflow {name!r}")
    return matches[:1] if latest else matches
