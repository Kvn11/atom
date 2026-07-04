"""CLI wiring: workspace persistence across chat turns + runtime override flags."""

from __future__ import annotations

import atom.cli as cli
from atom.runtime import RunResult


def _patch_run_agent(monkeypatch):
    calls: list[dict] = []

    async def fake_run_agent(task, **kw):
        calls.append({"task": task, **kw})
        return RunResult(thread_id="T", messages=[], final_text="ok", state={})

    monkeypatch.setattr(cli, "run_agent", fake_run_agent)
    return calls


def test_chat_preserves_existing_workspace_across_turns(monkeypatch, tmp_path):
    calls = _patch_run_agent(monkeypatch)
    inputs = iter(["do one", "do two", "exit"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(inputs))

    from typer.testing import CliRunner

    result = CliRunner().invoke(cli.app, ["chat", "--workspace", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    # The bound external workspace must persist to turn 2 (not silently switch to "new").
    assert calls[0]["workspace"] == str(tmp_path)
    assert calls[1]["workspace"] == str(tmp_path)


def test_run_forwards_thinking_and_system_prompt_overrides(monkeypatch):
    calls = _patch_run_agent(monkeypatch)
    from typer.testing import CliRunner

    result = CliRunner().invoke(
        cli.app,
        ["run", "do it", "--thinking", "high", "--system-prompt", "You are terse."],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["override_thinking"] == "high"
    assert calls[0]["override_system_prompt"] == "You are terse."


def test_prepare_model_applies_thinking_override(monkeypatch):
    import atom.agent as agent_mod
    from atom.config.schema import AgentProfile

    captured: dict = {}

    def fake_build_model(key, *, thinking=None, **kw):
        captured["thinking"] = thinking
        return object()

    monkeypatch.setattr(agent_mod, "build_model", fake_build_model)
    monkeypatch.setattr(agent_mod, "model_caps", lambda m, s: {"supports_vision": False})
    monkeypatch.setattr(agent_mod, "resolve_context_window", lambda m, s: 200_000)

    prof = AgentProfile(model="haiku", thinking="low")
    agent_mod.prepare_model(prof, override_thinking="high")
    assert captured["thinking"] == "high"  # override wins over profile.thinking
