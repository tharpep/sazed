import anthropic
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.notes.filing import FilerError, FilerUsage, Filing
from app.routers import notes


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(notes.router)
    return TestClient(app)


def _filing(**kw) -> Filing:
    base = {
        "topic": "Cooking",
        "new_topic": False,
        "kind": "idea",
        "reason": "It is a recipe.",
        "by_tag": False,
    }
    return Filing(**{**base, **kw})


def test_model_filing_returns_fields_and_usage(client, monkeypatch):
    async def fake(text, topics):
        return _filing(), FilerUsage(120, 30, 0.0004)

    monkeypatch.setattr(notes, "file_text", fake)
    resp = client.post("/filer/try", json={"text": "try miso soup"})
    assert resp.status_code == 200
    assert resp.json() == {
        "topic": "Cooking",
        "new_topic": False,
        "kind": "idea",
        "reason": "It is a recipe.",
        "by_tag": False,
        "filer": "1",
        "usage": {"input_tokens": 120, "output_tokens": 30, "cost_usd": 0.0004},
    }


def test_tag_filing_has_null_usage(client, monkeypatch):
    async def fake(text, topics):
        return _filing(topic="Gym", kind=None, by_tag=True, new_topic=True), None

    monkeypatch.setattr(notes, "file_text", fake)
    body = client.post("/filer/try", json={"text": "squats #gym"}).json()
    assert body["usage"] is None
    assert body["by_tag"] is True
    assert body["kind"] is None


def test_topics_are_passed_through(client, monkeypatch):
    seen = {}

    async def fake(text, topics):
        seen["args"] = (text, topics)
        return _filing(), None

    monkeypatch.setattr(notes, "file_text", fake)
    client.post("/filer/try", json={"text": "hello", "topics": {"Cooking": 3, "Gym": 1}})
    assert seen["args"] == ("hello", {"Cooking": 3, "Gym": 1})


def test_topics_default_to_empty(client, monkeypatch):
    seen = {}

    async def fake(text, topics):
        seen["topics"] = topics
        return _filing(), None

    monkeypatch.setattr(notes, "file_text", fake)
    client.post("/filer/try", json={"text": "hello"})
    assert seen["topics"] == {}


def test_whitespace_text_is_400(client, monkeypatch):
    async def fake(text, topics):
        raise AssertionError("file_text must not run")

    monkeypatch.setattr(notes, "file_text", fake)
    assert client.post("/filer/try", json={"text": "  \n\t "}).status_code == 400


def test_unreadable_answer_is_502(client, monkeypatch):
    async def fake(text, topics):
        raise FilerError("answer is not JSON")

    monkeypatch.setattr(notes, "file_text", fake)
    resp = client.post("/filer/try", json={"text": "hello"})
    assert resp.status_code == 502
    assert resp.json()["detail"].startswith("The filer's answer could not be read")


def test_model_failure_is_502(client, monkeypatch):
    async def fake(text, topics):
        raise anthropic.APIError(
            "boom", httpx.Request("POST", "https://api.anthropic.com"), body=None
        )

    monkeypatch.setattr(notes, "file_text", fake)
    resp = client.post("/filer/try", json={"text": "hello"})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "The model call failed"


def test_empty_text_is_422(client):
    assert client.post("/filer/try", json={"text": ""}).status_code == 422


def test_too_many_topics_is_422(client):
    topics = {f"t{i}": 1 for i in range(201)}
    assert client.post("/filer/try", json={"text": "hi", "topics": topics}).status_code == 422


def test_route_is_mounted_in_main():
    from app.main import app

    assert "/notes/filer/try" in {route.path for route in app.routes}
