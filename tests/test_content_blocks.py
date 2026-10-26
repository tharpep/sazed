"""Content-block helpers: thinking blocks must survive the tool loop, cache marks the copy only."""

from types import SimpleNamespace

from app.agent import loop, think_loop
from app.agent.client import tool_sig, with_message_cache


def _block(type_, **fields):
    dumped = {"type": type_, **fields}
    return SimpleNamespace(type=type_, model_dump=lambda exclude_none=True: dumped, **fields)


def _response_blocks():
    return [
        _block("thinking", thinking="", signature="sig-1"),
        _block("text", text="Checking."),
        _block("tool_use", id="tu_1", name="get_events", input={"days": 1}),
    ]


def test_chat_loop_keeps_thinking_blocks():
    dicts = loop._content_to_dicts(_response_blocks())
    assert dicts[0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}
    assert [d["type"] for d in dicts] == ["thinking", "text", "tool_use"]


def test_think_loop_keeps_thinking_blocks():
    # Opus 5.5 always thinks; dropping the block would break its tool loop.
    dicts = think_loop._content_to_dicts(_response_blocks())
    assert [d["type"] for d in dicts] == ["thinking", "text", "tool_use"]
    assert dicts[0]["signature"] == "sig-1"


def test_message_cache_marks_last_block_of_a_copy():
    messages = [{"role": "user", "content": "hi"}]
    cached = with_message_cache(messages)
    assert cached[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert messages == [{"role": "user", "content": "hi"}]


def test_message_cache_skips_a_trailing_thinking_block():
    messages = [{"role": "assistant", "content": [{"type": "thinking", "thinking": ""}]}]
    assert with_message_cache(messages) is messages


def test_tool_sig_ignores_argument_order():
    assert tool_sig("x", {"a": 1, "b": 2}) == tool_sig("x", {"b": 2, "a": 1})
