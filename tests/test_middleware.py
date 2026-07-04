"""Unit tests for individual middleware behaviors (ordering, loops, deferred, skills, images)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from atom.middleware.dangling_tool_call import DanglingToolCallMiddleware
from atom.middleware.loop_detection import LoopDetectionMiddleware


def _tc(name, cid, args=None):
    return {"name": name, "args": args or {}, "id": cid, "type": "tool_call"}


def _ai_call(name, args, cid):
    return AIMessage(content="", tool_calls=[_tc(name, cid, args)])


def test_dangling_placeholder_immediately_follows_its_aimessage():
    # Resume flow: a HumanMessage follows the AIMessage whose tool_call was never answered.
    ai = AIMessage(content="", tool_calls=[_tc("ask_clarification", "c1")], id="ai1")
    resume = HumanMessage(content="JSON please", id="h2")
    msgs = [HumanMessage(content="export", id="h1"), ai, resume]

    out = DanglingToolCallMiddleware().before_model({"messages": msgs}, runtime=None)
    result = out["messages"]

    # History is rewritten (RemoveMessage sentinel first), then the reordered list.
    assert isinstance(result[0], RemoveMessage)
    rebuilt = result[1:]
    tool_idx = next(
        i for i, m in enumerate(rebuilt)
        if isinstance(m, ToolMessage) and m.tool_call_id == "c1"
    )
    ai_idx = next(i for i, m in enumerate(rebuilt) if m is ai)
    human_idx = next(
        i for i, m in enumerate(rebuilt) if isinstance(m, HumanMessage) and m.content == "JSON please"
    )
    # ToolMessage must sit right after its AIMessage and BEFORE the resume human message.
    assert tool_idx == ai_idx + 1
    assert tool_idx < human_idx


def test_dangling_noop_when_all_calls_answered():
    ai = AIMessage(content="", tool_calls=[_tc("read_file", "r1")], id="ai1")
    answered = ToolMessage("ok", tool_call_id="r1")
    out = DanglingToolCallMiddleware().before_model(
        {"messages": [ai, answered]}, runtime=None
    )
    assert out is None


def test_loop_detection_triggers_on_consecutive_identical_calls():
    mw = LoopDetectionMiddleware(max_repeats=3)
    msgs = []
    for i in range(3):  # same bash(pwd) three times, results interleaved
        msgs.append(_ai_call("bash", {"command": "pwd"}, f"b{i}"))
        msgs.append(ToolMessage("/ws", tool_call_id=f"b{i}"))
    out = mw.after_model({"messages": msgs}, runtime=None)
    assert out is not None and out.get("jump_to") == "end"


def test_loop_detection_ignores_scattered_reuse():
    # A benign command reused across a long thread of DISTINCT work must not force-stop the run.
    mw = LoopDetectionMiddleware(max_repeats=3)
    msgs = []
    for i in range(6):
        msgs.append(_ai_call("bash", {"command": "pwd"}, f"p{i}"))     # same signature
        msgs.append(_ai_call("write_file", {"path": f"f{i}"}, f"w{i}"))  # distinct, breaks the run
    out = mw.after_model({"messages": msgs}, runtime=None)
    assert out is None


def test_view_image_middleware_clears_after_injection():
    from atom.middleware.view_image import ViewImageMiddleware
    from atom.reducers import CLEAR

    mw = ViewImageMiddleware()
    # After a model call that could see the image, the persisted base64 is cleared (transient).
    out = mw.after_model(
        {"viewed_images": {"/a.png": {"base64": "AAA", "mime_type": "image/png"}}}, runtime=None
    )
    assert out == {"viewed_images": CLEAR}
    assert mw.after_model({"viewed_images": {}}, runtime=None) is None


def test_view_image_rejects_oversized_before_reading(atom_home, monkeypatch):
    import pathlib

    import atom.tools.view_image as vi
    from atom.sandbox import LocalSandboxProvider, registry, thread_paths

    sb = LocalSandboxProvider().acquire(thread_paths("u", "vimg"))
    registry.register("vimg", sb)
    sb.write_text("big.png", "x" * 100)
    monkeypatch.setattr(vi, "_MAX_BYTES", 10)  # pretend the 100-byte file is "oversized"

    def _forbid_read(self):  # reading a multi-GB file before the size check would OOM
        raise AssertionError("read_bytes called before the size check")

    monkeypatch.setattr(pathlib.Path, "read_bytes", _forbid_read)
    rt = SimpleNamespace(context={"thread_id": "vimg"}, tool_call_id="t1")
    with pytest.raises(ValueError, match="exceeds"):
        vi.view_image.func(rt, "big.png")
