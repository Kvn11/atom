# tests/test_self_improve_workflow.py
"""The bundled self-improve.yaml is a valid WorkflowDef with the inputs the trigger stages,
and it ships as a built-in (resolvable with an empty $ATOM_HOME — no manual install)."""
from __future__ import annotations

from atom.workflow.schema import BUILTIN_WORKFLOWS_DIR, load_workflow, resolve_workflow_path


def test_self_improve_is_a_bundled_builtin(atom_home):
    # atom_home is a fresh tmp dir with no workflows/ — the built-in must still resolve.
    path = resolve_workflow_path("self-improve", str(atom_home))
    assert path is not None
    assert path == BUILTIN_WORKFLOWS_DIR / "self-improve.yaml"


def test_self_improve_yaml_is_valid_workflowdef(atom_home):
    wf = load_workflow("self-improve", str(atom_home))
    assert wf.name == "self-improve"
    names = {i.name: i for i in wf.inputs}
    assert names["run_log"].type == "file" and names["run_log"].required
    assert names["target_workflow"].type == "file" and names["target_workflow"].required
    assert {"workflow_name", "source_run_id", "run_status"} <= set(names)


def test_self_improve_has_analyze_then_improve_steps(atom_home):
    wf = load_workflow("self-improve", str(atom_home))
    assert len(wf.steps) == 2
    assert len(wf.steps[0].tasks) >= 2            # parallel analysis tasks
    assert len(wf.steps[1].tasks) == 1            # single synthesis task
    # the synthesis prompt names both deliverables
    synth = wf.steps[1].tasks[0].prompt
    assert "improved-" in synth and "suggestions.md" in synth
