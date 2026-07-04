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
