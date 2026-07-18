# tests/test_self_improve_workflow.py
"""The shipped self-improve.yaml is a valid WorkflowDef with the inputs the trigger stages."""
from __future__ import annotations

from pathlib import Path

import yaml

from atom.workflow.schema import WorkflowDef

_YAML = Path(__file__).resolve().parents[1] / "workflows" / "self-improve.yaml"


def test_self_improve_yaml_is_valid_workflowdef():
    wf = WorkflowDef.model_validate(yaml.safe_load(_YAML.read_text()))
    assert wf.name == "self-improve"
    names = {i.name: i for i in wf.inputs}
    assert names["run_log"].type == "file" and names["run_log"].required
    assert names["target_workflow"].type == "file" and names["target_workflow"].required
    assert {"workflow_name", "source_run_id", "run_status"} <= set(names)


def test_self_improve_has_analyze_then_improve_steps():
    wf = WorkflowDef.model_validate(yaml.safe_load(_YAML.read_text()))
    assert len(wf.steps) == 2
    assert len(wf.steps[0].tasks) >= 2            # parallel analysis tasks
    assert len(wf.steps[1].tasks) == 1            # single synthesis task
    # the synthesis prompt names both deliverables
    synth = wf.steps[1].tasks[0].prompt
    assert "improved-" in synth and "suggestions.md" in synth
