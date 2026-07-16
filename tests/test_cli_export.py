"""CLI wiring for `atom workflow export` (export_run/resolve_run_ids are stubbed)."""
from __future__ import annotations

import atom.observability.export as export_mod
from atom.cli import app
from atom.observability.export import ExportResult
from typer.testing import CliRunner

runner = CliRunner()


def _ok(run_id, **kw):
    return ExportResult(run_id=run_id, path=f"/x/{run_id}/export.json",
                        complete=kw.get("complete", True),
                        expected_roots=kw.get("expected", 1), fetched_roots=kw.get("fetched", 1))


def _ok_task(run_id, task_id="writer", **kw):
    return ExportResult(run_id=run_id, path=f"/x/{run_id}/exports/s0__{task_id}.json",
                        complete=kw.get("complete", True), expected_roots=1,
                        fetched_roots=kw.get("fetched", 1), task_id=task_id)


def test_export_single_run(monkeypatch):
    seen = {}
    monkeypatch.setattr(export_mod, "resolve_run_ids",
                        lambda home, **kw: [kw.get("run_id")] if kw.get("run_id") else [])
    def fake_export_run(home, run_id, *, project, **kw):
        seen["run_id"] = run_id; seen["project"] = project
        return _ok(run_id)
    monkeypatch.setattr(export_mod, "export_run", fake_export_run)

    res = runner.invoke(app, ["workflow", "export", "abc123", "--project", "proj"])
    assert res.exit_code == 0
    assert seen == {"run_id": "abc123", "project": "proj"}
    assert "exported abc123" in res.stdout


def test_export_requires_a_selector(monkeypatch):
    def boom(home, **kw):
        raise ValueError("provide exactly one of: <run_id>, --latest <workflow>, --all <workflow>")
    monkeypatch.setattr(export_mod, "resolve_run_ids", boom)
    res = runner.invoke(app, ["workflow", "export", "--project", "proj"])
    assert res.exit_code == 1
    assert "exactly one" in res.stdout


def test_export_no_traces_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    monkeypatch.setattr(export_mod, "export_run",
                        lambda home, rid, *, project, **kw: _ok(rid, fetched=0, complete=False))
    res = runner.invoke(app, ["workflow", "export", "r1", "--project", "proj"])
    assert res.exit_code == 1
    assert "no traces found" in res.stdout


def test_export_partial_warns_but_exits_0(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    monkeypatch.setattr(export_mod, "export_run",
                        lambda home, rid, *, project, **kw: _ok(rid, fetched=1, expected=2, complete=False))
    res = runner.invoke(app, ["workflow", "export", "r1", "--project", "proj"])
    assert res.exit_code == 0
    assert "partial" in res.stdout


def test_export_missing_key_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    def no_key(home, rid, *, project, **kw):
        raise RuntimeError("LANGSMITH_API_KEY is not set — cannot export from LangSmith")
    monkeypatch.setattr(export_mod, "export_run", no_key)
    res = runner.invoke(app, ["workflow", "export", "r1", "--project", "proj"])
    assert res.exit_code == 1
    assert "LANGSMITH_API_KEY" in res.stdout


def test_export_api_error_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    def boom(home, rid, *, project, **kw):
        raise ConnectionError("langsmith unreachable")
    monkeypatch.setattr(export_mod, "export_run", boom)
    res = runner.invoke(app, ["workflow", "export", "r1", "--project", "proj"])
    assert res.exit_code == 1
    assert "export failed for r1" in res.stdout


def test_export_run_not_found_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["ghost"])
    def missing(home, rid, *, project, **kw):
        raise FileNotFoundError(rid)
    monkeypatch.setattr(export_mod, "export_run", missing)
    res = runner.invoke(app, ["workflow", "export", "ghost", "--project", "proj"])
    assert res.exit_code == 1
    assert "not found" in res.stdout


# --- per-task export (--task <step>:<task_id>) ---

def test_export_task_success(monkeypatch):
    seen = {}
    monkeypatch.setattr(export_mod, "resolve_run_ids",
                        lambda home, **kw: [kw["run_id"]] if kw.get("run_id") else [])
    def fake(home, run_id, step_index, task_id, *, project, **kw):
        seen.update(run_id=run_id, step=step_index, task=task_id, project=project)
        return _ok_task(run_id, task_id)
    monkeypatch.setattr(export_mod, "export_task", fake)
    res = runner.invoke(app, ["workflow", "export", "abc123", "--task", "0:writer", "--project", "proj"])
    assert res.exit_code == 0
    assert seen == {"run_id": "abc123", "step": 0, "task": "writer", "project": "proj"}
    assert "exported abc123 task 0:writer" in res.stdout


def test_export_task_rejects_with_all(monkeypatch):
    res = runner.invoke(app, ["workflow", "export", "--all", "wf", "--task", "0:writer", "--project", "proj"])
    assert res.exit_code == 1
    assert "--all" in res.stdout


def test_export_task_malformed_selector_exits_1(monkeypatch):
    res = runner.invoke(app, ["workflow", "export", "abc123", "--task", "garbage", "--project", "proj"])
    assert res.exit_code == 1
    assert "step_index" in res.stdout


def test_export_task_non_terminal_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    def not_done(home, rid, step, tid, *, project, **kw):
        raise ValueError("task 'writer' has not completed (status: running)")
    monkeypatch.setattr(export_mod, "export_task", not_done)
    res = runner.invoke(app, ["workflow", "export", "r1", "--task", "0:writer", "--project", "proj"])
    assert res.exit_code == 1
    assert "has not completed" in res.stdout


def test_export_task_unknown_task_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    def missing(home, rid, step, tid, *, project, **kw):
        raise KeyError(f"task {tid!r} not found in step {step} of run {rid!r}")
    monkeypatch.setattr(export_mod, "export_task", missing)
    res = runner.invoke(app, ["workflow", "export", "r1", "--task", "0:ghost", "--project", "proj"])
    assert res.exit_code == 1
    assert "not found" in res.stdout


def test_export_task_no_traces_exits_1(monkeypatch):
    monkeypatch.setattr(export_mod, "resolve_run_ids", lambda home, **kw: ["r1"])
    monkeypatch.setattr(export_mod, "export_task",
                        lambda home, rid, step, tid, *, project, **kw: _ok_task(rid, tid, fetched=0, complete=False))
    res = runner.invoke(app, ["workflow", "export", "r1", "--task", "0:writer", "--project", "proj"])
    assert res.exit_code == 1
    assert "no traces found" in res.stdout


# --- provider dispatch (LangSmith vs LangFuse) ---

import atom.cli as cli
import atom.observability.langfuse_export as lf_mod
from atom.config.schema import AtomConfig, ObservabilityConfig


def test_export_dispatches_to_langfuse(monkeypatch):
    """provider=langfuse -> `workflow export` calls the LangFuse exporter, gated on LANGFUSE keys."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    cfg = AtomConfig(observability=ObservabilityConfig(provider="langfuse"))
    monkeypatch.setattr(cli, "load_config", lambda config: cfg)
    monkeypatch.setattr(lf_mod, "resolve_run_ids",
                        lambda home, **kw: [kw["run_id"]] if kw.get("run_id") else [])
    seen = {}
    def fake_run(home, run_id, *, project, **kw):
        seen["run_id"] = run_id
        return _ok(run_id)
    monkeypatch.setattr(lf_mod, "export_run", fake_run)

    res = runner.invoke(app, ["workflow", "export", "abc123"])   # no --project needed for langfuse
    assert res.exit_code == 0
    assert seen["run_id"] == "abc123"
    assert "exported abc123" in res.stdout


def test_export_langfuse_missing_keys_exits_1(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    cfg = AtomConfig(observability=ObservabilityConfig(provider="langfuse"))
    monkeypatch.setattr(cli, "load_config", lambda config: cfg)
    res = runner.invoke(app, ["workflow", "export", "abc123"])
    assert res.exit_code == 1
    assert "LANGFUSE" in res.stdout
