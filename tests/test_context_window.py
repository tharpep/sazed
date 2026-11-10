from unittest.mock import AsyncMock, patch

from app.agent import loop
from app.agent.client import CACHE_1H
from app.agent.tools import get_tool_schemas, tools_from_names

WINDOW = 15


def _history(n_pairs: int) -> list[dict]:
    out = []
    for i in range(n_pairs):
        out.append({"role": "user", "content": f"q{i}"})
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


def _tool_turn(i: int) -> list[dict]:
    return [
        {"role": "user", "content": f"q{i}"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"t{i}", "name": "x", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "r"}],
        },
        {"role": "assistant", "content": f"a{i}"},
    ]


def _pool(summary=None, through=0):
    pool = AsyncMock()
    pool.fetchrow.return_value = {"context_summary": summary, "summarized_through": through}
    return pool


async def _apply(pool, messages):
    with patch.object(loop, "compress_context", AsyncMock(return_value="S")) as compress:
        out = await loop._apply_context_window(pool, "sid", messages)
    return out, compress


async def test_window_never_starts_on_an_orphan_tool_result():
    messages = _history(7) + _tool_turn(7) + _history(6) + [{"role": "user", "content": "last"}]
    assert len(messages) > 2 * WINDOW
    plain = len(messages) - WINDOW
    assert messages[plain]["role"] == "user" and isinstance(messages[plain]["content"], list)

    out, _ = await _apply(_pool(), messages)

    first = out[2]
    assert first["role"] == "user"
    assert isinstance(first["content"], str)


async def test_no_summary_call_while_history_is_within_twice_the_window():
    messages = _history(WINDOW)
    out, compress = await _apply(_pool(), messages)
    compress.assert_not_called()
    assert out == messages


async def test_cut_moves_once_then_holds_between_chunks():
    messages = _history(WINDOW + 1)
    pool = _pool()
    out, compress = await _apply(pool, messages)
    compress.assert_awaited_once()
    new_cut = pool.execute.await_args.args[2]
    assert messages[new_cut]["role"] == "user"
    assert WINDOW - 2 <= len(messages) - new_cut <= WINDOW
    assert out[2:] == messages[new_cut:]

    # Next message adds one pair; the stored cut is reused and Haiku stays idle.
    longer = messages + _history(1)
    pool2 = _pool("S", new_cut)
    out2, compress2 = await _apply(pool2, longer)
    compress2.assert_not_called()
    pool2.execute.assert_not_called()
    assert out2[2:] == longer[new_cut:]
    assert out2[:2] == out[:2]


async def test_summary_covers_only_messages_after_the_previous_cut():
    messages = _history(40)
    pool = _pool("old", 20)
    _, compress = await _apply(pool, messages)
    summarized = compress.await_args.args[0]
    assert summarized == messages[20 : loop._clean_cut(messages, len(messages) - WINDOW)]
    assert compress.await_args.args[1] == "old"


def test_location_stays_out_of_the_system_prompt_blocks():
    import asyncio

    async def build():
        with (
            patch.object(loop, "load_prompt_memory", AsyncMock(return_value=[])),
            patch.object(loop.settings, "procedural_memory_enabled", False),
        ):
            return await loop._build_system_prompt("chat", "near me")

    blocks = asyncio.run(build())
    assert "Lat:" not in "".join(b["text"] for b in blocks)
    assert [b.get("cache_control") for b in blocks] == [CACHE_1H, CACHE_1H]


def test_tools_breakpoint_uses_the_one_hour_ttl():
    assert get_tool_schemas()[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert tools_from_names(["memory_update"])[-1]["cache_control"] == CACHE_1H
