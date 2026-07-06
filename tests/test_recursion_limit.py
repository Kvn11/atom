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
