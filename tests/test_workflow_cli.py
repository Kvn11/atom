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


def _seed_filewf(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "docwf.yaml").write_text(
        "name: docwf\n"
        "inputs:\n  - name: doc\n    type: file\n    required: true\n"
        "steps:\n  - title: Read\n    tasks:\n      - id: t1\n        prompt: \"summarize {{ doc }}\"\n"
    )


def _patch_fake_agent(monkeypatch):
    import atom.workflow.engine as engine_mod
    from atom.runtime import RunResult
    from langchain_core.messages import AIMessage

    async def fake_run_agent(prompt, **kwargs):
        return RunResult(thread_id=kwargs.get("thread_id", "t"),
                         messages=[AIMessage(content="did it")], final_text="did it", state={})
    monkeypatch.setattr(engine_mod, "run_agent", fake_run_agent)


def test_workflow_run_with_file_persists_and_resolves(atom_home, tmp_path, monkeypatch):
    _seed_filewf(atom_home)
    _patch_fake_agent(monkeypatch)
    src = tmp_path / "report.txt"
    src.write_text("hello\n")

    result = runner.invoke(app, ["workflow", "run", "docwf", "--file", f"doc={src}"])
    assert result.exit_code == 0, result.stdout

    from atom.workflow.run_store import RunStore
    store = RunStore(str(atom_home))
    runs = store.list()
    assert len(runs) == 1
    m = runs[0]
    assert m.inputs["doc"] == "/mnt/user-data/uploads/doc.txt"
    assert (store.uploads_dir(m.run_id) / "doc.txt").read_bytes() == b"hello\n"


def test_workflow_run_file_undeclared_input_errors(atom_home, tmp_path):
    _seed_filewf(atom_home)
    src = tmp_path / "x.txt"; src.write_text("x")
    result = runner.invoke(app, ["workflow", "run", "docwf", "--file", f"ghost={src}"])
    assert result.exit_code != 0
    assert "ghost" in result.stdout


def test_workflow_run_missing_required_file_errors(atom_home):
    _seed_filewf(atom_home)
    result = runner.invoke(app, ["workflow", "run", "docwf"])   # required file 'doc' not provided
    assert result.exit_code != 0
    assert "doc" in result.stdout or "missing" in result.stdout.lower()


def test_workflow_run_malformed_file_token_errors(atom_home):
    _seed_filewf(atom_home)
    result = runner.invoke(app, ["workflow", "run", "docwf", "--file", "doc"])  # missing =path
    assert result.exit_code != 0
    assert "NAME=PATH" in result.stdout or "=" in result.stdout


def _isolated_cfg(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("notes:\n  expose_to_logseq: false\n")
    return str(p)


def test_workflow_notes_clear_removes_vault(atom_home, tmp_path):
    from atom.notes import notes_root
    root = notes_root(str(atom_home), "demo")
    (root / "pages").mkdir(parents=True)
    result = runner.invoke(
        app, ["workflow", "notes", "clear", "demo", "--yes", "--config", _isolated_cfg(tmp_path)])
    assert result.exit_code == 0
    assert not root.exists()
    assert "Cleared" in result.stdout


def test_workflow_notes_clear_noop_when_absent(atom_home, tmp_path):
    result = runner.invoke(
        app, ["workflow", "notes", "clear", "ghost", "--yes", "--config", _isolated_cfg(tmp_path)])
    assert result.exit_code == 0
    assert "No notes vault" in result.stdout


def test_workflow_notes_clear_busy_exits_1(atom_home, tmp_path, monkeypatch):
    import atom.notes as notes_mod

    def _busy(*a, **k):
        raise notes_mod.VaultBusyError("graph 'atom.demo' is open in the Logseq desktop app")

    monkeypatch.setattr(notes_mod, "clear_vault", _busy)
    result = runner.invoke(
        app, ["workflow", "notes", "clear", "demo", "--yes", "--config", _isolated_cfg(tmp_path)])
    assert result.exit_code == 1
    # Collapse Rich's console-width-dependent line wrapping before the substring check.
    assert "open in the Logseq desktop app" in " ".join(result.stdout.split())


def test_workflow_notes_clear_refuses_when_active_run(atom_home):
    from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState
    store = RunStore(str(atom_home))
    m = RunManifest(
        run_id="cc1", workflow="demo", created_at="2026-07-18T00:00:00",
        workspace_path=str(store.workspace_dir("cc1")),
        steps=[StepState(index=0, title="S", tasks=[TaskState(id="t1", thread_id="cc1:s0:t1")])],
    )
    m.status = "running"
    store.create(m)
    (store.home / "notes" / "demo").mkdir(parents=True, exist_ok=True)
    result = runner.invoke(app, ["workflow", "notes", "clear", "demo", "--yes"])
    assert result.exit_code == 1
    assert "active" in result.stdout.lower()
    assert (store.home / "notes" / "demo").exists()   # not touched
