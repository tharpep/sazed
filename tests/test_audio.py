import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_list_voices_returns_engines_and_defaults(client):
    """Proves the voices endpoint exposes the supported engines and default voice."""
    resp = client.get("/audio/voices")
    assert resp.status_code == 200
    data = resp.json()
    assert "kokoro" in data["engines"]
    assert "fish" in data["engines"]
    assert data["default_engine"] == "kokoro"
    assert len(data["kokoro_voices"]) > 0


def test_synthesize_speech_rejects_empty_text(client):
    """Proves synthesis rejects whitespace or empty input."""
    resp = client.post("/audio/speech", json={"text": "   "})
    assert resp.status_code == 400


def test_synthesize_speech_fish_audio_missing_key_returns_503(client, monkeypatch):
    """Proves Fish Audio requests fail cleanly when no API key is configured."""
    monkeypatch.setattr(settings, "fish_audio_api_key", "")
    resp = client.post("/audio/speech", json={"text": "Hello", "engine": "fish"})
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_synthesize_speech_kokoro_streaming_success(client, monkeypatch):
    """Proves Kokoro synthesis streams bytes from the upstream endpoint."""
    class MockStreamResponse:
        status_code = 200

        async def aiter_bytes(self, chunk_size=4096):
            yield b"fake_mp3_chunk_1"
            yield b"fake_mp3_chunk_2"

        async def aclose(self):
            pass

    async def mock_send(self, req, stream=False):
        return MockStreamResponse()

    monkeypatch.setattr("app.routers.audio.get_client", lambda: type("C", (), {
        "build_request": lambda self, *args, **kwargs: None,
        "send": mock_send,
    })())
    resp = client.post("/audio/speech", json={"text": "Hello world", "engine": "kokoro"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"fake_mp3_chunk_1fake_mp3_chunk_2"


def test_synthesize_speech_upstream_error_raises_before_stream(client, monkeypatch):
    """Proves non-200 responses from upstream raise before streaming response starts."""
    class MockErrorResponse:
        status_code = 502

        async def aread(self):
            return b"Upstream gateway error"

        async def aclose(self):
            pass

    async def mock_send(self, req, stream=False):
        return MockErrorResponse()

    monkeypatch.setattr("app.routers.audio.get_client", lambda: type("C", (), {
        "build_request": lambda self, *args, **kwargs: None,
        "send": mock_send,
    })())
    resp = client.post("/audio/speech", json={"text": "Hello world", "engine": "kokoro"})
    assert resp.status_code == 502
    assert "Upstream gateway error" in resp.text


def test_transcribe_rejects_empty_file(client):
    """Proves transcription rejects empty audio uploads."""
    resp = client.post("/audio/transcribe", content=b"", headers={"content-type": "audio/wav"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_transcribe_audio_success(client, monkeypatch):
    """Proves transcription returns extracted text from Whisper response."""
    class MockResponse:
        status_code = 200

        def json(self):
            return {"text": "Hello Sazed, check calendar."}

    async def mock_post(*args, **kwargs):
        return MockResponse()

    monkeypatch.setattr("app.routers.audio.get_client", lambda: type("C", (), {
        "post": mock_post,
    })())
    resp = client.post(
        "/audio/transcribe", content=b"dummy_audio_bytes", headers={"content-type": "audio/wav"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"text": "Hello Sazed, check calendar."}
