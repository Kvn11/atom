import pytest
from langchain_core.messages import ToolMessage

from atom.middleware.tool_output_cap import ToolOutputCapMiddleware


def test_truncates_long_string_output_with_marker():
    mw = ToolOutputCapMiddleware(max_chars=100)

    def handler(req):
        return ToolMessage(content="Z" * 5000, tool_call_id="c1")

    out = mw.wrap_tool_call(object(), handler)
    assert len(out.content) < 5000
    assert "truncated to fit context" in out.content
    assert out.tool_call_id == "c1"          # identity preserved


def test_small_output_untouched():
    mw = ToolOutputCapMiddleware(max_chars=100)

    def handler(req):
        return ToolMessage(content="small", tool_call_id="c1")

    assert mw.wrap_tool_call(object(), handler).content == "small"


def test_caps_command_update_messages():
    mw = ToolOutputCapMiddleware(max_chars=50)

    class _Cmd:
        def __init__(self, messages):
            self.update = {"messages": messages}

    def handler(req):
        return _Cmd([ToolMessage(content="Q" * 2000, tool_call_id="c1")])

    out = mw.wrap_tool_call(object(), handler)
    assert "truncated to fit context" in out.update["messages"][0].content


def test_caps_list_content_text_block():
    mw = ToolOutputCapMiddleware(max_chars=50)

    def handler(req):
        return ToolMessage(content=[{"type": "text", "text": "W" * 2000}], tool_call_id="c1")

    out = mw.wrap_tool_call(object(), handler)
    assert "truncated to fit context" in out.content[0]["text"]


@pytest.mark.asyncio
async def test_async_truncates_long_string_output():
    mw = ToolOutputCapMiddleware(max_chars=100)

    async def handler(req):
        return ToolMessage(content="Z" * 5000, tool_call_id="c1")

    out = await mw.awrap_tool_call(object(), handler)
    assert len(out.content) < 5000 and "truncated to fit context" in out.content
