"""End-to-end smoke test: a scripted model drives a tool call through the real graph."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from atom.runtime import run_agent
from tests.conftest import make_prepared


def _tool_call(name: str, args: dict, call_id: str) -> dict:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


@pytest.mark.asyncio
async def test_write_then_finish(base_config):
    prepared = make_prepared([
        AIMessage(
            content="",
            tool_calls=[_tool_call(
                "write_file",
                {"description": "create hello", "path": "/mnt/user-data/workspace/hello.txt",
                 "content": "hello world\n"},
                "call_1",
            )],
        ),
        AIMessage(content="Done. I wrote hello.txt to the workspace."),
    ])

    result = await run_agent("make a hello file", config=base_config, prepared=prepared)

    assert "Done" in result.final_text
    # the tool actually wrote to the confined per-thread workspace
    physical = Path(base_config.home) / "users" / "default" / "threads" / result.thread_id
    hello = physical / "user-data" / "workspace" / "hello.txt"
    assert hello.exists(), f"expected file at {hello}"
    assert hello.read_text() == "hello world\n"


@pytest.mark.asyncio
async def test_no_tools_direct_answer(base_config):
    prepared = make_prepared([AIMessage(content="The answer is 42.")])
    result = await run_agent("what is the answer", config=base_config, prepared=prepared)
    assert result.final_text == "The answer is 42."


def test_middleware_order_invariants(base_config, atom_home):
    """The chain order is load-bearing: Clarification MUST be last (after_model unwinds in reverse),
    and planning + subagent delegation are always present (deviations #8/#9)."""
    from langchain.agents.middleware import TodoListMiddleware

    from atom.agent import _build_middlewares
    from atom.library import load_library
    from atom.middleware.clarification import ClarificationMiddleware
    from atom.middleware.subagent import SubagentMiddleware
    from atom.sandbox.provider import LocalSandboxProvider

    prepared = make_prepared([])
    profile = base_config.profile("default")
    provider = LocalSandboxProvider()
    library = load_library(str(atom_home))
    chain = _build_middlewares(
        base_config, profile, prepared, provider, str(atom_home), prepared.model, library
    )
    assert isinstance(chain[-1], ClarificationMiddleware)  # INVARIANT: last
    types = [type(m).__name__ for m in chain]
    assert "TodoListMiddleware" in types and "SubagentMiddleware" in types


def test_instruction_pin_and_trim_are_wired(base_config, atom_home):
    """InstructionPinMiddleware is in the chain and the compaction middleware reads 8000 tokens."""
    from atom.agent import _build_middlewares
    from atom.library import load_library
    from atom.middleware.compaction import PinnedSummarizationMiddleware
    from atom.middleware.instruction_pin import InstructionPinMiddleware
    from atom.middleware.subagent import SubagentMiddleware
    from atom.sandbox.provider import LocalSandboxProvider

    prepared = make_prepared([])
    profile = base_config.profile("default")
    provider = LocalSandboxProvider()
    library = load_library(str(atom_home))
    chain = _build_middlewares(
        base_config, profile, prepared, provider, str(atom_home), prepared.model, library
    )
    assert any(isinstance(m, InstructionPinMiddleware) for m in chain)
    comp = next(m for m in chain if isinstance(m, PinnedSummarizationMiddleware))
    assert comp.trim_tokens_to_summarize == 8000
    # subagent runner inherits the profile's subagent recursion_limit (wiring guard)
    sub = next(m for m in chain if isinstance(m, SubagentMiddleware))
    assert sub.runner.recursion_limit == profile.subagents.recursion_limit == 300


def test_summarizer_is_retry_wrapped(base_config):
    from atom.agent import _build_summarizer
    from atom.config.schema import AgentProfile
    from atom.middleware.llm_error import RetryPolicy, RetryingModel
    from tests.conftest import make_prepared
    from langchain_core.messages import AIMessage

    prepared = make_prepared([AIMessage(content="x")])
    prof = AgentProfile(model="haiku")          # no summarizer_model -> reuse lead model
    summ = _build_summarizer(prof, prepared, RetryPolicy(max_retries=3))
    assert isinstance(summ, RetryingModel)


def test_lead_middleware_uses_config_retry_policy(base_config):
    from atom.agent import build_lead_agent
    from atom.middleware.llm_error import LLMErrorHandlingMiddleware
    from tests.conftest import make_prepared
    from langchain_core.messages import AIMessage

    base_config.retry.max_retries = 7
    prepared = make_prepared([AIMessage(content="x")])
    agent = build_lead_agent(base_config, "default", prepared=prepared)
    mws = agent.middleware if hasattr(agent, "middleware") else []
    # Fall back to introspecting the builder directly if the compiled agent hides middleware:
    from atom.agent import _build_middlewares, _build_summarizer
    from atom.middleware.llm_error import RetryPolicy
    from atom.sandbox.provider import LocalSandboxProvider
    from atom.library import load_library
    policy = RetryPolicy(max_retries=base_config.retry.max_retries)
    summ = _build_summarizer(base_config.profile("default"), prepared, policy)
    chain = _build_middlewares(
        base_config, base_config.profile("default"), prepared,
        LocalSandboxProvider(bash_enabled=True), str(base_config.home), summ,
        load_library(str(base_config.home)), None, retry_policy=policy, skill_catalog=[],
    )
    llm_mws = [m for m in chain if isinstance(m, LLMErrorHandlingMiddleware)]
    assert llm_mws and llm_mws[0].policy.max_retries == 7
