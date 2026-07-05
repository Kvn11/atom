"""Pure step/run status classification."""
from __future__ import annotations

from atom.workflow.status import compute_run_status, compute_step_status


def test_step_complete_only_when_all_succeeded():
    assert compute_step_status(["succeeded", "succeeded"]) == "complete"


def test_step_failed_when_any_failed():
    assert compute_step_status(["succeeded", "failed"]) == "failed"
    assert compute_step_status(["failed", "failed"]) == "failed"


def test_step_running_and_pending():
    assert compute_step_status(["running", "pending"]) == "running"
    assert compute_step_status(["pending", "pending"]) == "pending"
    assert compute_step_status([]) == "pending"


def test_run_halts_on_any_failed_step():
    assert compute_run_status(["complete", "failed"]) == "halted"


def test_run_complete_and_running():
    assert compute_run_status(["complete", "complete"]) == "complete"
    assert compute_run_status(["complete", "running"]) == "running"
    assert compute_run_status(["pending", "pending"]) == "pending"
