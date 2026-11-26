import httpx
import pytest

from app.config import settings
from app.notes import client
from app.notes.client import NotesError, file_under, list_notes, read_note, topics
from app.notes.filing import Filing


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def _send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response

    async def get(self, url, **kwargs):
        return await self._send("get", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self._send("post", url, **kwargs)


@pytest.fixture
def fake(monkeypatch):
    def install(status=200, json=None, content=None, error=None):
        if content is not None:
            response = httpx.Response(status, content=content)
        else:
            response = httpx.Response(status, json=json)
        fc = FakeClient(response, error)
        monkeypatch.setattr(client, "get_client", lambda: fc)
        return fc

    monkeypatch.setattr(settings, "gateway_url", "http://gw")
    monkeypatch.setattr(settings, "gateway_api_key", "k")
    return install


FILING = Filing(
    topic="Homelab", new_topic=False, kind="idea", reason="Names the server.", by_tag=False
)

CALLS = [
    lambda: list_notes(),
    lambda: read_note("a.md"),
    lambda: topics(),
    lambda: file_under("a.md", FILING),
]


def check_request(fc, method, path):
    sent_method, url, kwargs = fc.calls[0]
    assert (sent_method, url) == (method, f"http://gw/vault{path}")
    assert kwargs["headers"] == {"X-API-Key": "k"}
    assert kwargs["timeout"] == 30.0
    return kwargs


async def test_list_notes_sends_only_set_params(fake):
    fc = fake(json={"notes": [], "total": 0})
    assert await list_notes() == {"notes": [], "total": 0}
    assert check_request(fc, "get", "/notes")["params"] == {"limit": 50, "offset": 0}


async def test_list_notes_sends_every_set_param(fake):
    fc = fake(json={"notes": [{"path": "a.md"}], "total": 9})
    result = await list_notes("Inbox", "Homelab", "nas", limit=5, offset=10)
    assert result == {"notes": [{"path": "a.md"}], "total": 9}
    assert check_request(fc, "get", "/notes")["params"] == {
        "limit": 5,
        "offset": 10,
        "folder": "Inbox",
        "topic": "Homelab",
        "q": "nas",
    }


async def test_read_note(fake):
    note = {"path": "a.md", "title": "T", "text": "body", "version": "v1", "filed": False}
    fc = fake(json=note)
    assert await read_note("a.md") == note
    assert check_request(fc, "get", "/note")["params"] == {"path": "a.md"}


async def test_topics_keeps_order_and_unfiled(fake):
    fc = fake(json={"topics": [{"name": "B", "count": 2}, {"name": "A", "count": 5}], "unfiled": 3})
    result = await topics()
    assert result == ({"B": 2, "A": 5}, 3)
    assert list(result[0]) == ["B", "A"]
    assert "params" not in check_request(fc, "get", "/topics")


async def test_file_under_posts_the_filing(fake):
    answer = {"path": "Homelab/a.md", "topic": "Homelab", "from": "a.md"}
    fc = fake(json=answer)
    assert await file_under("a.md", FILING) == answer
    assert check_request(fc, "post", "/file")["json"] == {
        "path": "a.md",
        "topic": "Homelab",
        "reason": "Names the server.",
        "kind": "idea",
        "filer": FILING.filer,
    }


async def test_file_under_sends_absent_kind_as_empty_string(fake):
    fc = fake(json={})
    filing = Filing(topic="Garden", new_topic=True, kind=None, reason="r", by_tag=True)
    await file_under("a.md", filing)
    assert fc.calls[0][2]["json"]["kind"] == ""


@pytest.mark.parametrize("call", CALLS)
async def test_json_detail_becomes_the_error(fake, call):
    fake(status=403, json={"detail": "Not allowed"})
    with pytest.raises(NotesError) as caught:
        await call()
    assert (caught.value.status, caught.value.detail, str(caught.value)) == (
        403,
        "Not allowed",
        "Not allowed",
    )


@pytest.mark.parametrize("call", CALLS)
async def test_plain_body_is_cut_to_200_characters(fake, call):
    fake(status=502, content="x" * 300)
    with pytest.raises(NotesError) as caught:
        await call()
    assert (caught.value.status, caught.value.detail) == (502, "x" * 200)


async def test_non_string_detail_falls_back_to_the_body(fake):
    fake(status=422, json={"detail": [{"msg": "bad"}]})
    with pytest.raises(NotesError) as caught:
        await read_note("a.md")
    assert caught.value.detail == '{"detail":[{"msg":"bad"}]}'


@pytest.mark.parametrize("call", CALLS)
async def test_connection_error_is_a_503(fake, call):
    fake(error=httpx.ConnectError("down"))
    with pytest.raises(NotesError) as caught:
        await call()
    assert (caught.value.status, caught.value.detail) == (
        503,
        "The notes service could not be reached",
    )
