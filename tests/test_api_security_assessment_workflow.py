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
    assert wf.steps[0].title == "Setup"
    task_ids = {t.id for t in wf.steps[0].tasks}
    assert task_ids == {"capture_recon", "build_sdk"}


def test_tasks_use_gemini_3_5_flash():
    for t in _load().steps[0].tasks:
        assert t.model == "gemini-3.5-flash"


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


def test_leads_delegate_per_endpoint_to_bash_subagents():
    # Both leads must COORDINATE, not inspect individual APIs (avoids recursion-limit blowups):
    # they delegate per-endpoint work to bash sub-agents.
    for t in _load().steps[0].tasks:
        p = t.prompt
        assert "delegate_task" in p
        assert 'subagent_type="bash"' in p          # bash children get shell + the vault CLI
        assert "COORDINATOR" in p and "Do NOT inspect" in p


def test_has_hypothesize_and_test_steps():
    wf = _load()
    assert [s.title for s in wf.steps] == ["Setup", "Hypothesize", "Test"]
    hyp = wf.steps[1].tasks
    tst = wf.steps[2].tasks
    assert [t.id for t in hyp] == ["hypothesize"]
    assert [t.id for t in tst] == ["test"]
    assert hyp[0].model == "gemini-3.5-flash" and tst[0].model == "gemini-3.5-flash"


def _task(name):
    return {t.id: t for s in _load().steps for t in s.tasks}[name].prompt


def test_setup_and_hypothesize_are_rerun_safe():
    tasks = {t.id: t for s in _load().steps for t in s.tasks}
    recon = tasks["capture_recon"].prompt
    hyp = tasks["hypothesize"].prompt
    # endpoint notes are create-if-missing (never re-clobber a prior assessment's note)
    assert "--if-missing" in recon and "--overwrite" not in recon
    assert "--if-missing" in hyp
    # recon.md accumulates a dated section instead of overwriting
    assert "vault_note.py append" in recon and "--kind recon" in recon
    assert "## Recon — {{ date }}" in recon
    # appended endpoint sections are date-stamped so re-assessment stacks, not duplicates
    assert "## Hypotheses — {{ date }}" in hyp


def test_hypothesize_prompt_delegates_and_covers_privacy():
    p = _task("hypothesize")
    assert "COORDINATOR" in p and "delegate_task" in p and 'subagent_type="bash"' in p
    assert "## Hypotheses" in p
    assert "PII" in p and "privacy" in p.lower()
    assert "vault_note.py append" in p


def test_test_prompt_is_safe_by_default_with_antibot_and_blockers():
    p = _task("test")
    assert "COORDINATOR" in p and "delegate_task" in p and 'subagent_type="bash"' in p
    assert "destructive-skipped" in p and "safe-by-default" in p.lower()
    assert "burp.py identities" in p          # capture is the identity roster (T3)
    assert "burp.py cred" in p and "$(" in p  # raw token only via $(...) capture
    assert "mint-once" in p.lower()           # anti-bot rules present
    assert "vault_note.py blocker" in p and "[[BLK-" in p
