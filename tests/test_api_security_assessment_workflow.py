from pathlib import Path

import yaml

from atom.workflow.schema import WorkflowDef

WF = Path(__file__).resolve().parents[1] / "workflows" / "api-security-assessment.yaml"


def test_workflow_loads_and_has_expected_shape():
    wf = WorkflowDef.model_validate(yaml.safe_load(WF.read_text()))
    assert wf.name == "api-security-assessment"
    assert wf.notes.enabled is True
    assert wf.notes.vault == "api-security-assessment"
    names = {i.name: i for i in wf.inputs}
    assert names["targets"].type == "file" and names["targets"].required
    assert names["capture"].type == "file" and names["capture"].required
    assert len(wf.steps) == 1
    task_ids = {t.id for t in wf.steps[0].tasks}
    assert task_ids == {"capture_recon", "build_sdk"}
    for t in wf.steps[0].tasks:
        assert t.model == "gemini-pro"          # never gemini-3
        assert "gemini-3" not in (t.model or "")


def test_example_targets_is_valid_json():
    import json
    json.loads((Path(__file__).resolve().parents[1] / "examples" / "targets.json").read_text())
