"""LangSmith trace config builder."""
from __future__ import annotations

from atom.workflow.observability import build_trace


def test_build_trace_shape():
    t = build_trace(workflow="poems", run_id="r1", step_index=0, step_title="Draft", task_id="poet_a")
    assert t["run_name"] == "poems/Draft/poet_a"
    assert "atom-workflow" in t["tags"]
    assert "workflow:poems" in t["tags"] and "step:Draft" in t["tags"] and "task:poet_a" in t["tags"]
    assert t["metadata"] == {
        "workflow": "poems", "run_id": "r1", "step_index": 0,
        "step_title": "Draft", "task_id": "poet_a",
    }
