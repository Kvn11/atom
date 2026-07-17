"""A FAILED workflow task must still persist its (partial) transcript.

Before the fix, ``engine._run_task`` only called ``save_chat`` on the success path, so a task that
raised mid-run left no chat behind and the UI showed "No messages yet". The fix has ``run_agent``
recover whatever the checkpointer holds when the agent loop fails and hand it to an ``on_transcript``
callback, which the engine wires to ``save_chat`` — so the transcript survives a failure.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from atom.agent import PreparedModel
from atom.runtime import run_agent
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef
from tests.conftest import DEFAULT_PROFILE_DATA, ScriptedChatModel


def _tc(name, args, cid):
    return {"name": name, "args": args, "id": cid, "type": "tool_call"}


class _FailAfterFirstTurn(ScriptedChatModel):
    """Emits its scripted first turn (a tool call, so the agent loop checkpoints a partial
    transcript), then raises on the next model call to simulate a mid-run failure."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self._i >= 1:
            raise RuntimeError("model boom on turn 2")
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _failing_prepared() -> PreparedModel:
    model = _FailAfterFirstTurn(
        responses=[AIMessage(
            content="listing files",
            tool_calls=[_tc("ls", {"description": "list", "path": "."}, "l1")],
        )],
        profile=DEFAULT_PROFILE_DATA,
    )
    caps = {
        "context_window": model.profile["max_input_tokens"],
        "max_output_tokens": model.profile["max_output_tokens"],
        "supports_vision": model.profile["image_inputs"],
        "supports_reasoning": model.profile["reasoning_output"],
        "has_profile": True,
    }
    return PreparedModel(model=model, caps=caps, context_window=caps["context_window"])


@pytest.mark.asyncio
async def test_run_agent_invokes_on_transcript_with_partial_on_failure(base_config, atom_home):
    """run_agent recovers the checkpointed partial transcript and hands it to on_transcript."""
    captured: list = []
    with pytest.raises(Exception):
        await run_agent(
            "do the thing", config=base_config, prepared=_failing_prepared(),
            on_transcript=lambda msgs: captured.append(list(msgs)),
        )
    assert captured, "on_transcript must be called when the agent loop fails"
    recovered = captured[-1]
    # the checkpointer preserved the WHOLE first turn, not just the opening prompt: the human
    # task turn plus the AI turn that ran before the crash.
    assert any(isinstance(m, HumanMessage) for m in recovered)
    assert any(isinstance(m, AIMessage) for m in recovered)


@pytest.mark.asyncio
async def test_engine_persists_partial_transcript_for_failed_task(base_config, atom_home):
    """End-to-end: a task that crashes mid-run still has a loadable chat afterwards."""
    wf = WorkflowDef.model_validate({
        "name": "demo",
        "steps": [{"title": "Draft", "tasks": [{"id": "crasher", "prompt": "please crash"}]}],
    })
    engine = WorkflowEngine(base_config, prepared_provider=lambda td, sd, wf: _failing_prepared())
    engine.create_run(wf, {}, "runcrash", "2026-07-03T00:00:00")
    manifest = await engine.execute("runcrash")

    assert manifest.status == "halted"
    assert manifest.steps[0].tasks[0].status == "failed"

    chat = engine.store.load_chat("runcrash", 0, "crasher")
    assert chat is not None, "a failed task must still persist its transcript"
    roles = [m.get("role") for m in chat]
    assert "task" in roles          # the relabeled opening prompt
    assert "ai" in roles            # ...and the work done before the crash, not just the prompt
