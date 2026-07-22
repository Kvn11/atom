from pathlib import Path

import yaml

from atom.workflow.schema import WorkflowDef

WF = Path(__file__).resolve().parents[1] / "workflows" / "api-security-assessment.yaml"


def _load():
    return WorkflowDef.model_validate(yaml.safe_load(WF.read_text()))


def _all_tasks():
    return [t for s in _load().steps for t in s.tasks]


def test_coordinator_tasks_raise_recursion_limit():
    for t in _all_tasks():
        assert t.recursion_limit == 600


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
    assert [s.title for s in wf.steps] == ["Setup", "Hypothesize", "Test", "Confirm"]
    hyp = wf.steps[1].tasks
    tst = wf.steps[2].tasks
    assert [t.id for t in hyp] == ["hypothesize"]
    assert [t.id for t in tst] == ["test"]
    assert hyp[0].model == "gemini-3.5-flash" and tst[0].model == "gemini-3.5-flash"


def test_has_confirm_step():
    wf = _load()
    assert wf.steps[3].title == "Confirm"
    conf = wf.steps[3].tasks
    assert [t.id for t in conf] == ["confirm"]
    assert conf[0].model == "gemini-3.5-flash"


def test_confirm_prompt_reproduces_and_gates():
    p = _task("confirm")
    assert "COORDINATOR" in p and "delegate_task" in p and 'subagent_type="bash"' in p
    assert "findings.py list" in p and "findings.py show" in p
    assert "findings.py confirm" in p and "findings.py discard" in p
    assert "confirmed-findings.jsonl" in p and "discarded-findings.jsonl" in p
    assert "verbatim" in p.lower()                 # evidence commands run exactly as recorded
    assert "0 findings" in p or "nothing to confirm" in p.lower()   # zero-finding graceful path
    # the deliverable must exist even when EVERY finding is discarded (not just when zero were emitted)
    assert "even if" in p.lower() and "discarded" in p.lower()
    # the Confirm sub-agent has its OWN destructive gate (the lead can't see per-finding evidence)
    assert "SAFETY GATE" in p and "not re-sent" in p.lower()


def test_test_findings_evidence_mints_any_credential():
    # evidence must never hardcode a live credential — cookies/headers are minted inline too
    p = _task("test")
    assert "--field cookie" in p


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


def test_test_step_emits_findings_jsonl():
    p = _task("test")
    assert "findings.py add" in p
    assert "{{ workspace }}/findings.jsonl" in p
    # emitted evidence is tokenless (mint inline), and the test-log heading is date-stamped
    assert "## Test log — {{ date }}" in p
    assert 'TOKEN=$(' in p and "--field authorization" in p


def test_test_prompt_is_safe_by_default_with_antibot_and_blockers():
    p = _task("test")
    assert "COORDINATOR" in p and "delegate_task" in p and 'subagent_type="bash"' in p
    assert "destructive-skipped" in p and "safe-by-default" in p.lower()
    assert "burp.py identities" in p          # capture is the identity roster (T3)
    assert "burp.py cred" in p and "$(" in p  # raw token only via $(...) capture
    assert "mint-once" in p.lower()           # anti-bot rules present
    assert "vault_note.py blocker" in p and "[[BLK-" in p


# NOTE: named `_task_at` (not `_task`) to avoid clobbering the string-keyed `_task(name)`
# helper above, which several pinned tests already rely on.
def _task_at(step_idx, task_idx=0):
    return _load().steps[step_idx].tasks[task_idx].prompt


def test_build_sdk_and_hypothesize_are_thin_coordinators():
    build_sdk = _task_at(0, 1)   # Setup step, second task
    hypothesize = _task_at(1, 0)  # Hypothesize step
    for p in (build_sdk, hypothesize):
        assert "COORDINATOR CONTRACT" in p
        assert "in ONE message" in p          # single-turn fan-out discipline
        assert "Do NOT inspect" in p
        assert 'subagent_type="bash"' in p


def test_confirm_is_thin_and_inlines_antibot():
    p = _task_at(3, 0)  # Confirm step
    assert "COORDINATOR CONTRACT" in p
    assert "in ONE message" in p
    assert "<RULES>" not in p                 # anti-bot rules are literal, not lead-injected
    assert "mint-once" in p                   # the rules appear literally
    # preserved gates from the confirm phase
    assert "SAFETY GATE" in p and "verbatim" in p.lower()


def test_test_lead_builds_roster_file_and_subagent_self_selects():
    p = _task_at(2, 0)  # Test step
    assert "COORDINATOR CONTRACT" in p and "in ONE message" in p
    # lead writes the roster ONCE to a file (endpoint-independent) — still uses burp.py identities
    assert "burp.py identities" in p and "identities.json" in p
    # lead no longer computes a per-endpoint attacker/victim mapping
    assert "You will pass the relevant identities" not in p
    assert "<RULES>" not in p                      # anti-bot rules are literal in the sub-agent block
    # the sub-agent self-selects from the roster
    assert "Pick your identities from the roster" in p
    assert "source_indices" in p and "user_ids" in p
    # preserved: tokenless idiom + cookie minting + findings emission + safety
    assert "TOKEN=$(" in p and "--field authorization" in p and "--field cookie" in p
    assert "findings.py add" in p and "{{ workspace }}/findings.jsonl" in p
    assert "## Test log — {{ date }}" in p
    assert "destructive-skipped" in p and "safe-by-default" in p.lower()
