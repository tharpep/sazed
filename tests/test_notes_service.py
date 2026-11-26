import anthropic
import httpx
import pytest

from app.config import settings
from app.notes import service
from app.notes.client import NotesError
from app.notes.filer import tag_topic
from app.notes.filing import FilerError, FilerUsage, Filing

TOPICS = {"Homelab": 4, "Recipes": 2}


def model_filing(topic="Homelab"):
    return Filing(
        topic=topic, new_topic=False, kind="idea", reason="Names the server.", by_tag=False
    )


def tag_filing():
    return Filing(
        topic="Homelab",
        new_topic=False,
        kind=None,
        reason="Because you typed #homelab.",
        by_tag=True,
    )


def api_error():
    request = httpx.Request("POST", "http://anthropic")
    return anthropic.APIConnectionError(request=request)


@pytest.fixture
def notes(monkeypatch):
    """Notes keyed by path, in the order the listing returns them (newest first)."""
    state = {"texts": {}, "calls": [], "limit": None, "file_errors": {}, "model_error": None}

    async def fake_list(folder=None, topic="", query="", limit=50, offset=0):
        state["calls"].append(("list", folder))
        state["limit"] = limit
        return {"notes": [{"path": p} for p in state["texts"]], "total": len(state["texts"])}

    async def fake_read(path):
        state["calls"].append(("read", path))
        return {"path": path, "text": state["texts"][path]}

    async def fake_topics():
        state["calls"].append(("topics",))
        return dict(TOPICS), 1

    async def fake_file_text(text, names):
        state["calls"].append(("file_text", text))
        if tag_topic(text, names):
            return tag_filing(), None
        if state["model_error"]:
            raise state["model_error"]
        return model_filing(), FilerUsage(100, 20, 0.001)

    async def fake_file_under(path, filing):
        state["calls"].append(("file_under", path))
        if path in state["file_errors"]:
            raise state["file_errors"][path]
        return {
            "path": f"{filing.topic}/{path.split('/')[-1]}",
            "topic": filing.topic,
            "from": path,
        }

    monkeypatch.setattr(service, "list_notes", fake_list)
    monkeypatch.setattr(service, "read_note", fake_read)
    monkeypatch.setattr(service, "topics", fake_topics)
    monkeypatch.setattr(service, "file_text", fake_file_text)
    monkeypatch.setattr(service, "file_under", fake_file_under)
    return state


async def test_file_note_with_the_model(notes):
    notes["texts"]["inbox/a.md"] = "set up the nas"

    result = await service.file_note("inbox/a.md")

    assert [c[0] for c in notes["calls"]] == ["read", "topics", "file_text", "file_under"]
    assert result == {
        "path": "Homelab/a.md",
        "from": "inbox/a.md",
        "topic": "Homelab",
        "new_topic": False,
        "kind": "idea",
        "reason": "Names the server.",
        "by_tag": False,
        "filer": "1",
        "usage": {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.001},
    }


async def test_file_note_by_tag(notes):
    notes["texts"]["inbox/a.md"] = "fix the router #homelab"

    result = await service.file_note("inbox/a.md", allow_model=False)

    assert result["by_tag"] is True
    assert result["usage"] is None
    assert result["kind"] is None
    assert result["from"] == "inbox/a.md"


async def test_file_note_outside_the_inbox(notes):
    with pytest.raises(ValueError, match="only an inbox note can be filed"):
        await service.file_note("Homelab/a.md")

    assert notes["calls"] == []


async def test_file_note_without_the_model_and_without_a_tag(notes):
    notes["texts"]["inbox/a.md"] = "no tag here"

    with pytest.raises(service.ModelNeeded):
        await service.file_note("inbox/a.md", allow_model=False)

    assert [c[0] for c in notes["calls"]] == ["read", "topics"]


async def test_sweep_files_oldest_first(notes):
    for name in ("new", "mid", "old"):
        notes["texts"][f"inbox/{name}.md"] = name

    result = await service.sweep_once()

    assert [f["from"] for f in result["filed"]] == ["inbox/old.md", "inbox/mid.md", "inbox/new.md"]
    assert result["filed"][0] == {
        "path": "Homelab/old.md",
        "from": "inbox/old.md",
        "topic": "Homelab",
        "by_tag": False,
    }
    assert (result["seen"], result["waiting"], result["failed"], result["model_down"]) == (
        3,
        0,
        0,
        False,
    )


async def test_sweep_passes_the_batch_limit(notes, monkeypatch):
    monkeypatch.setattr(settings, "notes_sweep_batch", 7)

    await service.sweep_once()
    assert notes["limit"] == 7
    assert notes["calls"][0] == ("list", "inbox")

    await service.sweep_once(limit=3)
    assert notes["limit"] == 3


async def test_sweep_with_the_model_down_still_files_tagged_notes(notes):
    notes["model_error"] = api_error()
    notes["texts"]["inbox/c.md"] = "late tagged #recipes"
    notes["texts"]["inbox/b.md"] = "untagged two"
    notes["texts"]["inbox/a.md"] = "untagged one"

    result = await service.sweep_once()

    assert [f["from"] for f in result["filed"]] == ["inbox/c.md"]
    assert result["filed"][0]["by_tag"] is True
    assert (result["waiting"], result["failed"], result["model_down"]) == (2, 0, True)
    model_calls = [c for c in notes["calls"] if c[0] == "file_text"]
    assert [c[1] for c in model_calls] == ["untagged one", "late tagged #recipes"]


async def test_sweep_goes_on_after_a_failed_note(notes):
    notes["texts"]["inbox/c.md"] = "three"
    notes["texts"]["inbox/b.md"] = "two"
    notes["texts"]["inbox/a.md"] = "one"
    notes["file_errors"]["inbox/a.md"] = NotesError(409, "changed")
    notes["file_errors"]["inbox/b.md"] = FilerError("topic is missing or malformed")

    result = await service.sweep_once()

    assert [f["from"] for f in result["filed"]] == ["inbox/c.md"]
    assert (result["failed"], result["waiting"], result["model_down"]) == (2, 0, False)


async def test_sweep_does_not_catch_a_failed_listing(monkeypatch):
    async def fake_list(**kwargs):
        raise NotesError(503, "down")

    monkeypatch.setattr(service, "list_notes", fake_list)

    with pytest.raises(NotesError):
        await service.sweep_once()
