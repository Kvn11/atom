"""Pure step/run status classification for workflow runs."""
from __future__ import annotations


def compute_step_status(task_statuses: list[str]) -> str:
    """A step is complete only if every task succeeded; any failed ⇒ failed."""
    if not task_statuses:
        return "pending"
    if any(s in ("pending", "running") for s in task_statuses):
        return "pending" if all(s == "pending" for s in task_statuses) else "running"
    if all(s == "succeeded" for s in task_statuses):
        return "complete"
    return "failed"


def compute_run_status(step_statuses: list[str]) -> str:
    """The run halts if any step failed; completes only if every step completed."""
    if not step_statuses:
        return "pending"
    if any(s == "failed" for s in step_statuses):
        return "halted"
    if all(s == "complete" for s in step_statuses):
        return "complete"
    if all(s == "pending" for s in step_statuses):
        return "pending"
    return "running"
