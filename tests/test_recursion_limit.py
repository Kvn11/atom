"""The per-run recursion limit is configurable and defaults high enough for real multi-step tasks.

atom's ~18-middleware chain compiles to ~11 LangGraph super-steps per agent turn, so a low
recursion_limit (the old hardcoded 100) killed legitimate ~14-turn tasks (e.g. a refiner that
reads 3 files, rewrites each, compiles an anthology, and presents them) mid-work with a
GraphRecursionError. These pin the configurable limit and its threading into the run configs.
"""

from __future__ import annotations

from atom.config.schema import AgentProfile, SubagentConfig
from tests.conftest import ScriptedChatModel


def test_agent_profile_recursion_limit_default():
    assert AgentProfile().recursion_limit == 400


def test_subagent_recursion_limit_default():
    assert SubagentConfig().recursion_limit == 300


def test_build_run_config_carries_recursion_limit():
    from atom.runtime import build_run_config

    cfg = build_run_config("t1", 400)
    assert cfg["configurable"]["thread_id"] == "t1"
    assert cfg["recursion_limit"] == 400


def test_build_run_config_merges_trace_without_dropping_limit():
    from atom.runtime import build_run_config

    cfg = build_run_config("t2", 250, {"run_name": "r", "tags": ["x"], "metadata": {"k": 1}})
    assert cfg["recursion_limit"] == 250
    assert cfg["run_name"] == "r" and cfg["tags"] == ["x"] and cfg["metadata"] == {"k": 1}


def test_subagent_child_config_uses_its_recursion_limit():
    from atom.subagent import SubagentRunner

    m = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    runner = SubagentRunner(
        model=m, home="/tmp", context_window=200_000, bash_enabled=False, recursion_limit=321,
    )
    conf = runner._child_config("cid")
    assert conf["recursion_limit"] == 321
    assert conf["configurable"]["thread_id"] == "cid"


import pytest
from langchain_core.messages import AIMessage

from tests.conftest import make_prepared


def _spy_build_run_config(monkeypatch, seen):
    from atom import runtime
    real = runtime.build_run_config

    def spy(thread_id, recursion_limit, trace=None, obs_provider=None):
        seen["limit"] = recursion_limit
        return real(thread_id, recursion_limit, trace, obs_provider)

    monkeypatch.setattr(runtime, "build_run_config", spy)


@pytest.mark.asyncio
async def test_run_agent_honors_override_recursion_limit(base_config, monkeypatch):
    from atom import runtime

    seen: dict = {}
    _spy_build_run_config(monkeypatch, seen)
    prepared = make_prepared([AIMessage(content="ok")])
    await runtime.run_agent(
        "hi", config=base_config, prepared=prepared, override_recursion_limit=777
    )
    assert seen["limit"] == 777


@pytest.mark.asyncio
async def test_run_agent_defaults_to_profile_recursion_limit(base_config, monkeypatch):
    from atom import runtime

    seen: dict = {}
    _spy_build_run_config(monkeypatch, seen)
    prepared = make_prepared([AIMessage(content="ok")])
    await runtime.run_agent("hi", config=base_config, prepared=prepared)
    assert seen["limit"] == base_config.profile("default").recursion_limit
