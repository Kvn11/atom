"""run_agent trace config merge."""
from __future__ import annotations

import pytest
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.messages import AIMessage

from atom.observability.provider import LangFuseProvider
from atom.runtime import _apply_trace, run_agent
from tests.conftest import make_prepared


def test_apply_trace_merges_keys():
    cfg = {"configurable": {"thread_id": "t"}, "recursion_limit": 100}
    out = _apply_trace(cfg, {"run_name": "wf/s/t", "tags": ["a"], "metadata": {"x": 1}})
    assert out["run_name"] == "wf/s/t"
    assert out["tags"] == ["a"]
    assert out["metadata"] == {"x": 1}


def test_apply_trace_none_is_noop():
    cfg = {"configurable": {}}
    assert _apply_trace(cfg, None) == {"configurable": {}}


@pytest.mark.asyncio
async def test_run_agent_accepts_trace(base_config):
    prepared = make_prepared([AIMessage(content="hello")])
    result = await run_agent(
        "hi", config=base_config, prepared=prepared,
        trace={"run_name": "wf/s/t", "tags": ["atom-workflow"], "metadata": {"run_id": "r1"}},
    )
    assert result.final_text == "hello"


class _FakeHandler(BaseCallbackHandler):
    """Minimal handler double: subclasses BaseCallbackHandler so the real LangChain
    AsyncCallbackManager (which reads .run_inline/.raise_error/ignore_* off every
    attached handler even for a plain identity check) can dispatch to it without
    error when run_agent actually invokes the graph, matching how LangFuse's real
    CallbackHandler behaves."""


class _FakeLFClient:
    def flush(self):
        pass


def test_build_run_config_decorates_with_provider():
    from atom.runtime import build_run_config
    handler = _FakeHandler()
    prov = LangFuseProvider(_FakeLFClient(), handler)
    cfg = build_run_config("r1:s0:t0", 100, {"metadata": {"run_id": "r1"}}, prov)
    assert cfg["callbacks"] == [handler]
    assert cfg["metadata"]["langfuse_session_id"] == "r1"
    assert cfg["configurable"]["thread_id"] == "r1:s0:t0"


def test_build_run_config_no_provider_is_plain():
    from atom.runtime import build_run_config
    cfg = build_run_config("t", 100, {"metadata": {"run_id": "r1"}})
    assert "callbacks" not in cfg


@pytest.mark.asyncio
async def test_run_agent_accepts_obs_provider(base_config):
    prepared = make_prepared([AIMessage(content="hello")])
    prov = LangFuseProvider(_FakeLFClient(), _FakeHandler())
    result = await run_agent(
        "hi", config=base_config, prepared=prepared,
        trace={"run_name": "wf/s/t", "tags": ["atom-workflow"], "metadata": {"run_id": "r1"}},
        obs_provider=prov,
    )
    assert result.final_text == "hello"
