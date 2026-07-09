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
from pathlib import Path
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
