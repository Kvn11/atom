from pathlib import Path

import yaml

from atom.workflow.schema import WorkflowDef

WF = Path(__file__).resolve().parents[1] / "workflows" / "api-security-assessment.yaml"


def _load():
    return WorkflowDef.model_validate(yaml.safe_load(WF.read_text()))


def test_workflow_loads_and_has_expected_shape():
    wf = _load()
    assert wf.name == "api-security-assessment"
    assert wf.notes.enabled is True
    assert wf.notes.vault == "api-security-assessment"
    names = {i.name: i for i in wf.inputs}
    assert names["targets"].type == "file" and names["targets"].required
    assert names["capture"].type == "file" and names["capture"].required
    assert len(wf.steps) == 1
    task_ids = {t.id for t in wf.steps[0].tasks}
    assert task_ids == {"capture_recon", "build_sdk"}


def test_tasks_use_gemini_pro_never_gemini_3():
    for t in _load().steps[0].tasks:
        assert t.model == "gemini-pro"
        assert "gemini-3" not in (t.model or "")


def test_prompts_reference_the_shipped_toolkit_and_vault():
    tasks = {t.id: t for t in _load().steps[0].tasks}
    recon = tasks["capture_recon"].prompt
    sdk = tasks["build_sdk"].prompt
    # both tasks invoke the shipped CLI by its absolute mount path
    assert "/mnt/skill_library/api-recon-toolkit/scripts/" in recon
    assert "/mnt/skill_library/api-recon-toolkit/scripts/" in sdk
    # recon files notes into the domain-split vault; sdk lands in the shared workspace
    assert "vault_note.py" in recon and "api-security-assessment" in recon
    assert "{{ workspace }}/sdk/" in sdk
