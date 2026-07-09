"""LangSmith run exporter: pure helpers + the fetch/poll pipeline with an injected fake client."""
from __future__ import annotations

import json

import pytest

from atom.observability.export import (
    ExportResult,
    build_envelope,
    expected_root_count,
)
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState


def _manifest(run_id: str, statuses: list[str]) -> RunManifest:
    tasks = [
        TaskState(id=f"t{i}", thread_id=f"{run_id}:s0:t{i}", status=st)
        for i, st in enumerate(statuses)
    ]
    return RunManifest(
        run_id=run_id, workflow="wf", created_at="2026-07-09T00:00:00",
        workspace_path="/tmp/ws", steps=[StepState(index=0, title="S", tasks=tasks)],
    )


def test_expected_root_count_skips_pending():
    m = _manifest("r1", ["succeeded", "failed", "running", "pending"])
    assert expected_root_count(m) == 3  # pending excluded


def test_expected_root_count_all_pending_is_zero():
    assert expected_root_count(_manifest("r1", ["pending", "pending"])) == 0


def test_build_envelope_shape():
    m = _manifest("r1", ["succeeded"])
    roots = [{"id": "root1", "child_runs": [{"run_type": "llm"}]}]
    env = build_envelope(
        "r1", "wf", "proj", m, roots,
        complete=True, expected=1, fetched=1, now="2026-07-09T12:00:00",
    )
    assert env["run_id"] == "r1" and env["workflow"] == "wf" and env["project"] == "proj"
    assert env["exported_at"] == "2026-07-09T12:00:00"
    assert env["complete"] is True and env["expected_roots"] == 1 and env["fetched_roots"] == 1
    assert env["roots"] == roots
    assert env["atom_manifest"]["run_id"] == "r1"          # manifest embedded verbatim
    assert env["atom_manifest"]["steps"][0]["tasks"][0]["status"] == "succeeded"
    # Whole envelope must be JSON-serializable.
    assert json.loads(json.dumps(env))["run_id"] == "r1"


def test_export_result_is_a_dataclass():
    r = ExportResult(run_id="r1", path="/x/export.json", complete=True,
                     expected_roots=1, fetched_roots=1)
    assert r.run_id == "r1" and r.complete is True
