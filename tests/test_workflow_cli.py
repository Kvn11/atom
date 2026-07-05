"""CLI workflow subcommands."""
from __future__ import annotations

from typer.testing import CliRunner

from atom.cli import app

runner = CliRunner()


def _seed(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "demo.yaml").write_text(
        "name: demo\ndescription: A demo.\n"
        "steps:\n  - title: Draft\n    tasks:\n      - id: t1\n        prompt: \"hello\"\n"
    )


def test_workflow_list(atom_home):
    _seed(atom_home)
    result = runner.invoke(app, ["workflow", "list"])
    assert result.exit_code == 0
    assert "demo" in result.stdout


def test_workflow_run_completes(atom_home, monkeypatch):
    _seed(atom_home)
    import atom.workflow.engine as engine_mod
    from atom.runtime import RunResult

    async def fake_run_agent(prompt, **kwargs):
        from langchain_core.messages import AIMessage
        return RunResult(
            thread_id=kwargs.get("thread_id", "t"),
            messages=[AIMessage(content="did it")], final_text="did it", state={},
        )

    monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)
    result = runner.invoke(app, ["workflow", "run", "demo"])
    assert result.exit_code == 0
    assert "complete" in result.stdout.lower()


def test_workflow_runs_lists(atom_home, monkeypatch):
    _seed(atom_home)
    import atom.workflow.engine as engine_mod
    from atom.runtime import RunResult

    async def fake_run_agent(prompt, **kwargs):
        from langchain_core.messages import AIMessage
        return RunResult(thread_id="t", messages=[AIMessage(content="x")], final_text="x", state={})

    monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)
    runner.invoke(app, ["workflow", "run", "demo"])
    result = runner.invoke(app, ["workflow", "runs"])
    assert result.exit_code == 0
    assert "demo" in result.stdout


def test_workflow_run_unknown_name_clean_error(atom_home):
    result = runner.invoke(app, ["workflow", "run", "does-not-exist"])
    assert result.exit_code != 0
    assert "Traceback" not in result.stdout            # clean message, not a raw traceback
    assert "does-not-exist" in result.stdout or "not found" in result.stdout.lower()


def test_workflow_run_malformed_input_errors(atom_home):
    _seed(atom_home)                                   # seeds a "demo" workflow (see existing tests)
    result = runner.invoke(app, ["workflow", "run", "demo", "--input", "topic"])   # missing =value
    assert result.exit_code != 0
    assert "KEY=VALUE" in result.stdout or "=" in result.stdout
