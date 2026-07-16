"""Subagent delegation: delegate_task runs a child that shares the workspace + limit truncation."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from atom.middleware.subagent import SubagentLimitMiddleware
from atom.runtime import run_agent
from tests.conftest import make_prepared


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


@pytest.mark.asyncio
async def test_delegate_task_runs_child(base_config):
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc(
            "delegate_task",
            {"description": "compute", "prompt": "reply with CHILD_RESULT",
             "subagent_type": "general-purpose"},
            "d1",
        )]),
        AIMessage(content="CHILD_RESULT"),                    # the child's answer
        AIMessage(content="The sub-agent returned CHILD_RESULT."),  # lead's final
    ])
    result = await run_agent("delegate something", config=base_config, prepared=prepared)

    tool_msgs = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert any("CHILD_RESULT" in m.content for m in tool_msgs)
    assert "CHILD_RESULT" in result.final_text


def test_subagent_limit_truncates_excess_calls():
    mw = SubagentLimitMiddleware(max_concurrent=2)
    calls = [_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}") for i in range(4)]
    calls.append(_tc("read_file", {"path": "x"}, "r1"))
    msg = AIMessage(content="", tool_calls=calls, id="ai1")
    out = mw.after_model({"messages": [msg]}, runtime=None)
    kept = out["messages"][0].tool_calls
    task_calls = [c for c in kept if c["name"] == "delegate_task"]
    assert len(task_calls) == 2  # truncated from 4 to the cap
    assert any(c["name"] == "read_file" for c in kept)  # non-task calls preserved
    assert out["messages"][0].id == "ai1"  # same id -> replaces in place


def test_subagent_limit_clamps_to_2_4_band():
    # Below the floor: a config of 1 must still allow 2.
    mw1 = SubagentLimitMiddleware(max_concurrent=1)
    calls = [_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}") for i in range(4)]
    out1 = mw1.after_model({"messages": [AIMessage(content="", tool_calls=calls, id="a")]}, runtime=None)
    assert len([c for c in out1["messages"][0].tool_calls if c["name"] == "delegate_task"]) == 2
    # Above the ceiling: a config of 10 must cap at 4.
    mw2 = SubagentLimitMiddleware(max_concurrent=10)
    calls6 = [_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}") for i in range(6)]
    out2 = mw2.after_model({"messages": [AIMessage(content="", tool_calls=calls6, id="a")]}, runtime=None)
    assert len([c for c in out2["messages"][0].tool_calls if c["name"] == "delegate_task"]) == 4


def test_subagent_limit_strips_orphaned_tool_use_blocks():
    # Anthropic-shaped AIMessage: tool_use blocks live in list content, mirrored in tool_calls.
    content = [{"type": "text", "text": "spawning"}]
    calls = []
    for i in range(4):
        content.append({"type": "tool_use", "id": f"d{i}", "name": "delegate_task", "input": {}})
        calls.append(_tc("delegate_task", {"prompt": f"t{i}"}, f"d{i}"))
    msg = AIMessage(content=content, tool_calls=calls, id="ai1")
    out = SubagentLimitMiddleware(max_concurrent=2).after_model({"messages": [msg]}, runtime=None)
    new = out["messages"][0]
    kept_call_ids = {c["id"] for c in new.tool_calls}
    tool_use_ids = {b["id"] for b in new.content if isinstance(b, dict) and b.get("type") == "tool_use"}
    # No tool_use block may survive without a matching (kept) tool_call, else the next turn 400s.
    assert tool_use_ids == kept_call_ids
    assert len(tool_use_ids) == 2


def test_child_config_tags_subagent():
    from atom.subagent import SubagentRunner
    # Construct minimally; _child_config only reads self.recursion_limit.
    runner = SubagentRunner.__new__(SubagentRunner)
    runner.recursion_limit = 300
    cfg = runner._child_config("child-1")
    assert cfg["metadata"] == {"atom_subagent": True}
    assert cfg["configurable"]["thread_id"] == "child-1"
    assert cfg["recursion_limit"] == 300


def test_child_agent_has_skill_tools_and_catalog(atom_home):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(
        model=None, home=str(atom_home), context_window=100_000, bash_enabled=True,
        skill_catalog=[{"name": "logseq-cli", "description": "Operate Logseq"}],
        has_skill_library=True,
    )
    for st in ("general-purpose", "bash"):
        names = [t.name for t in runner._child_tools(st)]
        assert "load_skill" in names and "search_skills" in names
    sys = runner._child_system("general-purpose")
    assert "logseq-cli" in sys and "Operate Logseq" in sys


def test_bash_child_prompt_includes_notes_vault(atom_home):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(
        model=None, home=str(atom_home), context_window=100_000, bash_enabled=True,
        notes={"provider": "logseq", "root_dir": "/n/notes-smoke", "graph": "smoke-graph"},
    )
    bash_sys = runner._child_system("bash")
    assert "Persistent notes" in bash_sys
    assert "smoke-graph" in bash_sys and "/n/notes-smoke" in bash_sys
    # General-purpose children have no bash and workspace-confined file tools, so they cannot reach
    # the out-of-workspace vault via the logseq CLI -> they must NOT get a misleading vault block.
    gp_sys = runner._child_system("general-purpose")
    assert "Persistent notes" not in gp_sys and "smoke-graph" not in gp_sys


def test_bash_child_prompt_omits_notes_block_when_absent(atom_home):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(
        model=None, home=str(atom_home), context_window=100_000, bash_enabled=True,
    )  # notes unset -> no vault block, template stays valid under StrictUndefined
    assert "Persistent notes" not in runner._child_system("bash")


def test_child_middleware_includes_skill_library_when_catalog(atom_home):
    from atom.middleware.skill_library import SkillLibraryMiddleware
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(atom_home), context_window=100_000,
                            bash_enabled=False, skill_catalog=[{"name": "x", "description": "y"}])
    assert any(isinstance(m, SkillLibraryMiddleware) for m in runner._child_middleware())


def test_child_agent_no_skill_tools_when_none(atom_home):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(atom_home), context_window=100_000, bash_enabled=False)
    names = [t.name for t in runner._child_tools("general-purpose")]
    assert "load_skill" not in names and "search_skills" not in names


def test_child_agent_cannot_delegate(base_config):
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(base_config.home), context_window=100_000,
                            bash_enabled=True)
    for st in ("general-purpose", "bash"):
        names = [t.name for t in runner._child_tools(st)]
        assert "delegate_task" not in names  # no nested delegation (recursion guard)


@pytest.mark.asyncio
async def test_subagent_usage_attributed_to_parent(base_config):
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[_tc(
            "delegate_task",
            {"description": "c", "prompt": "reply", "subagent_type": "general-purpose"}, "d1")]),
        AIMessage(content="CHILD",
                  usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
        AIMessage(content="done"),
    ])
    result = await run_agent("delegate", config=base_config, prepared=prepared)
    assert result.state.get("usage", {}).get("total_tokens") == 15  # child tokens folded into parent


@pytest.mark.asyncio
async def test_runner_unregistered_after_run(base_config):
    from atom.subagent import get_runner

    prepared = make_prepared([AIMessage(content="hi")])
    result = await run_agent("hi", config=base_config, prepared=prepared)
    assert get_runner(result.thread_id) is None  # no per-thread runner leak


def test_child_middleware_pins_and_uses_atom_summary_prompt():
    from atom.middleware.compaction import PinnedSummarizationMiddleware
    from atom.middleware.instruction_pin import InstructionPinMiddleware
    from atom.subagent import SubagentRunner
    from tests.conftest import ScriptedChatModel

    model = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    runner = SubagentRunner(
        model=model,
        home="/tmp",
        context_window=200_000,
        bash_enabled=False,
        summarizer=model,
        summary_input_tokens=8000,
        summary_prompt="ATOM-SUMMARY {messages}",
    )
    mw = runner._child_middleware()
    assert any(isinstance(m, InstructionPinMiddleware) for m in mw)      # pin wired (#3)
    comp = next(m for m in mw if isinstance(m, PinnedSummarizationMiddleware))
    assert comp.trim_tokens_to_summarize == 8000                         # trim wired (#3)
    assert "ATOM-SUMMARY" in comp.summary_prompt                         # atom prompt used (#2)


@pytest.mark.asyncio
async def test_subagent_child_config_carries_trace(base_config):
    from atom.config.schema import ObservabilityConfig
    from atom.subagent import SubagentRunner
    from tests.conftest import ScriptedChatModel

    model = ScriptedChatModel(responses=[AIMessage(content="CHILD_DONE")],
                              profile={"max_input_tokens": 100_000})
    base_trace = {
        "run_name": "wf/Draft/t", "tags": ["role:lead", "workflow:wf"],
        "metadata": {"session_id": "p1", "workflow": "wf", "run_id": "r1",
                     "step_title": "Draft", "task_id": "t", "agent_role": "lead",
                     "is_subagent": False},
    }
    runner = SubagentRunner(
        model=model, home=str(base_config.home), context_window=100_000,
        bash_enabled=False, base_trace=base_trace, observability=ObservabilityConfig(),
    )

    captured = {}

    class _StubAgent:
        async def ainvoke(self, inp, config=None, context=None):
            captured["config"] = config
            return {"messages": [AIMessage(content="CHILD_DONE")]}

    runner._child_agent = lambda st, system=None: _StubAgent()

    text, _usage = await runner.run("p1", "do the thing", "go", "general-purpose")
    assert text == "CHILD_DONE"
    cfg = captured["config"]
    assert cfg["configurable"]["thread_id"].startswith("p1:sub:")  # child keeps its own state id
    md = cfg["metadata"]
    assert md["is_subagent"] is True and md["agent_role"] == "subagent"
    assert md["session_id"] == "p1"                 # grouped into the lead's thread
    assert md["parent_thread_id"] == "p1"
    assert md["subagent_type"] == "general-purpose"
    assert "role:subagent" in cfg["tags"] and "subagent_type:general-purpose" in cfg["tags"]
    assert md["atom_subagent"] is True   # _child_config's marker must survive _apply_trace's metadata merge


@pytest.mark.asyncio
async def test_subagent_no_base_trace_is_untraced(base_config):
    from atom.subagent import SubagentRunner
    from tests.conftest import ScriptedChatModel

    model = ScriptedChatModel(responses=[AIMessage(content="OK")],
                              profile={"max_input_tokens": 100_000})
    runner = SubagentRunner(model=model, home=str(base_config.home),
                            context_window=100_000, bash_enabled=False)  # no base_trace

    captured = {}

    class _StubAgent:
        async def ainvoke(self, inp, config=None, context=None):
            captured["config"] = config
            return {"messages": [AIMessage(content="OK")]}

    runner._child_agent = lambda st, system=None: _StubAgent()
    await runner.run("p1", "d", "go", "general-purpose")
    cfg = captured["config"]
    # Child runs always have the atom_subagent marker for filtering (see _child_config).
    # But when there's no base_trace, no observability metadata is attached.
    assert cfg["metadata"] == {"atom_subagent": True}
    assert "tags" not in cfg


def test_child_middleware_includes_llm_error_retry(atom_home):
    from atom.middleware.llm_error import LLMErrorHandlingMiddleware, RetryPolicy
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(
        model=None, home=str(atom_home), context_window=100_000, bash_enabled=False,
        retry=RetryPolicy(max_retries=9),
    )
    mws = runner._child_middleware()
    llm = [m for m in mws if isinstance(m, LLMErrorHandlingMiddleware)]
    assert llm and llm[0].policy.max_retries == 9


def test_child_middleware_retry_defaults_when_unset(atom_home):
    from atom.middleware.llm_error import LLMErrorHandlingMiddleware
    from atom.subagent import SubagentRunner

    runner = SubagentRunner(model=None, home=str(atom_home), context_window=100_000,
                            bash_enabled=False)  # retry unset -> default policy
    llm = [m for m in runner._child_middleware() if isinstance(m, LLMErrorHandlingMiddleware)]
    assert llm and llm[0].policy.max_retries == 20
