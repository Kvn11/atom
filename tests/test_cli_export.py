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
