import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.notes import filer
from app.notes.filer import file_text, messages_for, parse_filing, tag_topic
from app.notes.filing import FilerError
from app.notes.prompt import SYSTEM

TOPICS = {"Homelab": 3, "New job": 1}


def answer(**over):
    data = {"topic": "Homelab", "kind": "idea", "reason": "Because it names the server."}
    data.update(over)
    return json.dumps(data)


def fake_client(text, calls):
    async def create(**kwargs):
        calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=50)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=usage)

    return SimpleNamespace(messages=SimpleNamespace(create=create))


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(settings, "llm_cost_tracking", False)
    monkeypatch.setattr(filer, "get_client", lambda: fake_client(answer(), seen))
    return seen


def test_tag_matches_existing_topic_in_another_spelling():
    assert tag_topic("fix it #new-job today", TOPICS) == ("New job", "new-job")


def test_tag_new_topic_is_spaced_and_capitalised():
    assert tag_topic("#side_project idea", TOPICS) == ("Side project", "side_project")


def test_no_tag():
    assert tag_topic("nothing to see", TOPICS) is None


def test_hash_in_url_fragment_or_after_word_is_not_a_tag():
    assert tag_topic("see https://x.dev/page#intro", TOPICS) is None
    assert tag_topic("issue#12 and C# and &#39; and ##x", TOPICS) is None
    assert tag_topic("call #1 now", TOPICS) is None


def test_messages_with_and_without_topics():
    msg = messages_for("hello", TOPICS)
    assert msg == [
        {
            "role": "user",
            "content": "Existing topics, with how many notes each holds: Homelab (3), "
            "New job (1)\n\n<note>\nhello\n</note>",
        }
    ]
    assert "(none yet)\n\n<note>" in messages_for("hello", {})[0]["content"]


def test_messages_cut_the_note_at_2000_characters():
    content = messages_for("a" * 2500, {})[0]["content"]
    assert "a" * 2000 + "\n</note>" in content
    assert "a" * 2001 not in content


def test_parse_fenced_json():
    filing = parse_filing(f"```json\n{answer()}\n```", TOPICS)
    assert (filing.topic, filing.new_topic, filing.kind, filing.by_tag) == (
        "Homelab",
        False,
        "idea",
        False,
    )


def test_parse_existing_topic_in_another_case():
    filing = parse_filing(answer(topic=" HOMELAB "), TOPICS)
    assert (filing.topic, filing.new_topic) == ("Homelab", False)


def test_parse_new_topic_ignores_model_flag():
    filing = parse_filing(answer(topic="Garden", new_topic=False), TOPICS)
    assert (filing.topic, filing.new_topic) == ("Garden", True)


@pytest.mark.parametrize("topic", ["", "-x", "x" * 41, "a;b", 5, None])
def test_parse_bad_topic(topic):
    with pytest.raises(FilerError):
        parse_filing(answer(topic=topic), TOPICS)


@pytest.mark.parametrize("raw", ["[1]", "nope", '"x"'])
def test_parse_non_object(raw):
    with pytest.raises(FilerError):
        parse_filing(raw, TOPICS)


@pytest.mark.parametrize("reason", ["", "  \n ", None, 3])
def test_parse_bad_reason(reason):
    with pytest.raises(FilerError):
        parse_filing(answer(reason=reason), TOPICS)


def test_parse_reason_is_folded_and_cut():
    filing = parse_filing(answer(reason="Because\n  a  " + "b" * 300), TOPICS)
    assert filing.reason.startswith("Because a b")
    assert len(filing.reason) == 200


def test_parse_unknown_kind_is_none_and_kind_is_lowered():
    assert parse_filing(answer(kind="poem"), TOPICS).kind is None
    assert parse_filing(answer(kind="TODO"), TOPICS).kind == "todo"


async def test_tagged_note_makes_no_model_call(calls):
    filing, usage = await file_text("  buy #homelab disk ", TOPICS)
    assert calls == []
    assert usage is None
    assert (filing.topic, filing.new_topic, filing.kind, filing.by_tag) == (
        "Homelab",
        False,
        None,
        True,
    )
    assert filing.reason == "Because you typed #homelab."
    new, _ = await file_text("#garden plan", TOPICS)
    assert (new.topic, new.new_topic) == ("Garden", True)


async def test_untagged_note_calls_haiku_once(calls):
    filing, usage = await file_text("check the proxmox box", TOPICS)
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == settings.haiku_model
    assert call["max_tokens"] == 300
    assert call["system"] == SYSTEM
    assert call["messages"] == messages_for("check the proxmox box", TOPICS)
    assert filing.topic == "Homelab"
    assert (usage.input_tokens, usage.output_tokens) == (1000, 50)
    assert usage.cost_usd is not None and usage.cost_usd > 0


async def test_answer_with_no_text_block_is_a_filer_error(monkeypatch):
    async def create(**kwargs):
        return SimpleNamespace(content=[], usage=None)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(settings, "llm_cost_tracking", False)
    monkeypatch.setattr(filer, "get_client", lambda: client)
    with pytest.raises(FilerError, match="no text"):
        await file_text("check the proxmox box", TOPICS)


async def test_empty_text(calls):
    with pytest.raises(ValueError, match="text is empty"):
        await file_text("  \n", TOPICS)
    assert calls == []
