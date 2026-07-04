"""Build a LangSmith trace config (run_name/tags/metadata) for a workflow task.

LangSmith activates purely from env vars (LANGSMITH_TRACING / LANGSMITH_API_KEY /
LANGSMITH_PROJECT). When unset, this dict is harmless metadata on the run config.
"""
from __future__ import annotations


def build_trace(*, workflow: str, run_id: str, step_index: int, step_title: str, task_id: str) -> dict:
    return {
        "run_name": f"{workflow}/{step_title}/{task_id}",
        "tags": [
            "atom-workflow",
            f"workflow:{workflow}",
            f"step:{step_title}",
            f"task:{task_id}",
            f"run:{run_id}",
        ],
        "metadata": {
            "workflow": workflow,
            "run_id": run_id,
            "step_index": step_index,
            "step_title": step_title,
            "task_id": task_id,
        },
    }
