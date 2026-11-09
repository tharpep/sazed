import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


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
async def test_synthesize_speech_kokoro_mock(client, monkeypatch):
    """Proves Kokoro synthesis streams bytes from the upstream endpoint."""
    async def mock_stream(*args, **kwargs):
        yield b"fake_mp3_chunk_1"
        yield b"fake_mp3_chunk_2"

    monkeypatch.setattr("app.routers.audio._stream_kokoro", mock_stream)
    resp = client.post("/audio/speech", json={"text": "Hello world", "engine": "kokoro"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"fake_mp3_chunk_1fake_mp3_chunk_2"


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

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)
    resp = client.post(
        "/audio/transcribe", content=b"dummy_audio_bytes", headers={"content-type": "audio/wav"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"text": "Hello Sazed, check calendar."}
