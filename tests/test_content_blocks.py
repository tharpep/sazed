"""Content-block helpers: thinking blocks must survive the tool loop, cache marks the copy only."""

import copy
from types import SimpleNamespace

from app.agent import loop, think_loop
from app.agent.client import elide_old_tool_results, tool_sig, with_message_cache


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


def _tool_turn(n: int, output: str) -> list[dict]:
    return [
        {"role": "user", "content": f"question {n}"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": f"sig-{n}"},
                {"type": "tool_use", "id": f"tu_{n}", "name": "get_email", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"tu_{n}", "content": output}],
        },
        {"role": "assistant", "content": [{"type": "text", "text": f"answer {n}"}]},
    ]


def _paired(messages: list[dict]) -> bool:
    # Every tool_result answers a tool_use in the assistant message right before it.
    for i, m in enumerate(messages):
        if m["role"] != "user" or isinstance(m["content"], str):
            continue
        result_ids = [b["tool_use_id"] for b in m["content"] if b["type"] == "tool_result"]
        if not result_ids:
            continue
        prev = messages[i - 1]["content"]
        use_ids = [b["id"] for b in prev if b["type"] == "tool_use"]
        if result_ids != use_ids:
            return False
    return True


def test_old_tool_results_are_stubbed_on_a_copy():
    body = "x" * 5000
    messages = _tool_turn(1, body) + _tool_turn(2, body) + _tool_turn(3, body)
    messages[2]["content"][0]["is_error"] = True
    stored = copy.deepcopy(messages)

    sent = elide_old_tool_results(messages)

    old = sent[2]["content"][0]
    assert old["content"] == "[Output omitted: get_email returned 5000 bytes]"
    assert old["tool_use_id"] == "tu_1" and old["is_error"] is True
    # The last two turns keep their outputs, thinking blocks and all.
    assert sent[6]["content"][0]["content"] == body
    assert sent[10]["content"][0]["content"] == body
    assert sent[4:] == stored[4:]
    assert len(sent) == len(messages) and _paired(sent)
    # The stored conversation is untouched.
    assert messages == stored


def test_elision_counts_turns_from_user_prompts_not_tool_results():
    # The current turn is mid tool loop: its tool_result message isn't a new turn.
    body = "y" * 500
    messages = _tool_turn(1, body) + _tool_turn(2, body)[:3]
    assert elide_old_tool_results(messages) is messages


def test_elision_leaves_short_results_alone():
    messages = _tool_turn(1, "ok") + _tool_turn(2, "ok") + _tool_turn(3, "ok")
    assert elide_old_tool_results(messages) == messages


def test_tool_sig_ignores_argument_order():
    assert tool_sig("x", {"a": 1, "b": 2}) == tool_sig("x", {"b": 2, "a": 1})
