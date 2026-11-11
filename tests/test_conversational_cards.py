"""Conversational cards: screen context in the stored prefix, ConfirmCard, voice cards, effort."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import loop
from app.agent.confirm_card import build_confirm_card
from app.config import settings

SID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class FakePool:
    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        return "OK"


def _tool_use(name, tool_input):
    return SimpleNamespace(type="tool_use", id="tu1", name=name, input=tool_input)


def _response(stop_reason, *blocks):
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=None)


class FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def gen():
            for block in self.response.content:
                yield SimpleNamespace(type="content_block_start", content_block=block)
                if block.type == "text":
                    yield SimpleNamespace(type="text", text=block.text)

        return gen()

    @property
    def text_stream(self):
        async def gen():
            for block in self.response.content:
                if block.type == "text":
                    yield block.text

        return gen()

    async def get_final_message(self):
        return self.response


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.responses.pop(0))


@pytest.fixture
def stream_env(monkeypatch):
    async def noop(*args, **kwargs):
        return None

    async def setup_turn(*args, **kwargs):
        return [{"role": "user", "content": "hi"}], [], []

    monkeypatch.setattr(loop, "get_pool", lambda: FakePool())
    monkeypatch.setattr(loop, "_setup_turn", setup_turn)
    monkeypatch.setattr(loop, "_save_message", noop)
    monkeypatch.setattr(loop, "_save_assistant", noop)
    monkeypatch.setattr(loop, "_finalize_turn", noop)
    monkeypatch.setattr(settings, "llm_cost_tracking", False)

    def install(responses):
        client = FakeClient(responses)
        monkeypatch.setattr(loop, "get_client", lambda: client)
        return client

    return install


async def _events(mode):
    out = []
    async for chunk in loop.run_turn_stream(str(SID), "hi", mode=mode):
        name, data = chunk.strip().split("\n", 1)
        out.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


@pytest.mark.parametrize("mode", ["chat", "voice"])
async def test_confirm_card_follows_awaiting_confirmation(stream_env, mode):
    stream_env(
        [
            _response("tool_use", _tool_use("restart_container", {"vmid": 104})),
            _response("end_turn", SimpleNamespace(type="text", text="Say yes to confirm.")),
        ]
    )
    events = await _events(mode)
    names = [name for name, _ in events]
    done = names.index("tool_done")
    assert events[done][1]["status"] == "awaiting_confirmation"
    assert names[done + 1] == "ui_block"
    card = events[done + 1][1]
    assert card["component"] == "ConfirmCard"
    assert card["props"]["tool"] == "restart_container"
    assert card["props"]["title"] == "Restart container 104"
    assert card["props"]["details"] == [{"label": "Container", "value": "104"}]
    assert card["props"]["expires_at"]


async def test_mapped_ui_block_is_emitted_in_voice(stream_env, monkeypatch):
    stream_env(
        [
            _response("tool_use", _tool_use("get_tasks", {})),
            _response("end_turn", SimpleNamespace(type="text", text="Two tasks.")),
        ]
    )
    result = SimpleNamespace(
        content=json.dumps({"tasks": [{"title": "a"}], "count": 1}),
        status="success",
        error=None,
    )

    async def fake_exec(pool, sid, blocks, counts, turn):
        for block in blocks:
            yield block, result, None

    monkeypatch.setattr(loop, "_execute_regular_tools_streaming", fake_exec)
    events = await _events("voice")
    cards = [data for name, data in events if name == "ui_block"]
    assert cards == [{"component": "TaskWidget", "props": {"tasks": [{"title": "a"}], "count": 1}}]


async def test_voice_runs_sonnet_at_low_effort(stream_env):
    client = stream_env([_response("end_turn", SimpleNamespace(type="text", text="Hi."))])
    await _events("voice")
    call = client.calls[0]
    assert call["model"] == settings.sonnet_model
    assert call["extra_body"]["output_config"] == {"effort": "low"}


async def test_chat_keeps_medium_effort(stream_env):
    client = stream_env([_response("end_turn", SimpleNamespace(type="text", text="Hi."))])
    await _events("chat")
    assert client.calls[0]["extra_body"]["output_config"] == {"effort": "medium"}


def test_escalated_voice_turn_keeps_medium_effort():
    assert loop._voice_effort("voice", settings.opus_model) == "medium"


def test_confirm_card_titles_for_sensitive_tools():
    email = build_confirm_card("send_email", {"to": "a@b.co", "subject": "Hi", "body": "x"})
    assert email["props"]["title"] == "Send an email to a@b.co"
    assert email["props"]["details"] == [
        {"label": "To", "value": "a@b.co"},
        {"label": "Subject", "value": "Hi"},
    ]
    event = build_confirm_card("delete_event", {"event_id": "ev1"})
    assert event["props"]["title"] == "Delete a calendar event"
    assert event["props"]["details"] == [{"label": "Event ID", "value": "ev1"}]


def test_confirm_card_generic_fallback_lists_args():
    card = build_confirm_card("delete_task", {"task_id": "t1", "list_id": "l1"})
    assert card["props"]["title"] == "Run delete task"
    assert card["props"]["details"] == [
        {"label": "Task id", "value": "t1"},
        {"label": "List id", "value": "l1"},
    ]


async def test_screen_lines_are_capped_and_stored_in_the_prefix(monkeypatch):
    saved = []

    async def save(pool, sid, role, content):
        saved.append(content)

    async def passthrough(pool, sid, messages):
        return messages

    async def system_prompt(mode="chat", user_message=""):
        return [{"type": "text", "text": "SYSTEM"}]

    async def tools(*args):
        return []

    async def no_title(*args):
        return None

    monkeypatch.setattr(loop, "_save_message", save)
    monkeypatch.setattr(loop, "_apply_context_window", passthrough)
    monkeypatch.setattr(loop, "_build_system_prompt", system_prompt)
    monkeypatch.setattr(loop, "_select_tools_resilient", tools)
    monkeypatch.setattr(loop, "_generate_session_title", no_title)

    screen = ["* Jellyfin is down"] + [f"row {i}" for i in range(20)]
    screen[1] = "x" * 500
    messages, system, _ = await loop._setup_turn(
        FakePool(), SID, "restart that", "voice", "UTC", None, screen
    )
    text = messages[-1]["content"]
    assert saved == [text]
    assert text.endswith("restart that")
    block = text.split("[On screen]\n", 1)[1].rsplit("\nrestart that", 1)[0]
    lines = block.split("\n")
    assert len(lines) == 12
    assert lines[0] == "* Jellyfin is down"
    assert lines[1] == "- " + "x" * 200
    assert "On screen" not in json.dumps(system)


def test_no_screen_leaves_the_prefix_unchanged():
    assert loop._screen_block(None) == ""
    assert loop._screen_block(["", "  "]) == ""


async def test_done_event_carries_timing_stamps(stream_env):
    """A normal streamed turn ends with a done event holding all three integer timing stamps."""
    stream_env([_response("end_turn", SimpleNamespace(type="text", text="Hi."))])
    events = await _events("voice")
    name, data = events[-1]
    assert name == "done"
    assert set(data["timing"]) == {"setup_ms", "llm_ttft_ms", "first_text_ms"}
    assert all(isinstance(v, int) and v >= 0 for v in data["timing"].values())
    assert [d["text"] for n, d in events if n == "text_delta"] == ["Hi."]
