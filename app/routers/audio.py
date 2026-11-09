from collections.abc import AsyncIterator
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings
from app.http_client import get_client

router = APIRouter()


class SpeechRequest(BaseModel):
    text: str
    voice: str | None = None
    engine: Literal["kokoro", "fish"] | None = None
    format: str = "mp3"


class TranscribeResponse(BaseModel):
    text: str


async def _stream_tts_response(
    url: str,
    payload: dict,
    headers: dict | None,
    media_type: str,
    service_name: str,
) -> StreamingResponse:
    client = get_client()
    req = client.build_request("POST", url, json=payload, headers=headers)
    try:
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail=f"{service_name} service timed out")
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502, detail=f"{service_name} service unreachable: {exc}"
        )

    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        raise HTTPException(status_code=resp.status_code, detail=body.decode(errors="ignore"))

    async def _body_stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes(chunk_size=4096):
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(_body_stream(), media_type=media_type)


@router.post("/speech")
async def synthesize_speech(body: SpeechRequest):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    engine = body.engine or settings.default_tts_engine
    voice = body.voice or settings.default_tts_voice
    media_type = "audio/mpeg" if body.format == "mp3" else "audio/wav"

    if engine == "kokoro":
        url = f"{settings.kokoro_tts_url}/audio/speech"
        payload = {
            "model": "kokoro",
            "voice": voice,
            "input": body.text,
            "response_format": body.format,
        }
        return await _stream_tts_response(url, payload, None, media_type, "Kokoro TTS")

    if engine == "fish":
        if not settings.fish_audio_api_key:
            raise HTTPException(status_code=503, detail="Fish Audio API key not configured")
        url = "https://api.fish.audio/v1/tts"
        headers = {
            "Authorization": f"Bearer {settings.fish_audio_api_key}",
            "Content-Type": "application/json",
            "model": "drama-3",
        }
        payload = {
            "text": body.text,
            "reference_id": body.voice if body.voice else None,
            "format": body.format,
            "streaming": True,
        }
        return await _stream_tts_response(url, payload, headers, media_type, "Fish Audio")

    raise HTTPException(status_code=400, detail=f"Unsupported TTS engine: {engine}")


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(request: Request):
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio content")

    content_type = request.headers.get("content-type", "audio/wav")
    client = get_client()
    url = f"{settings.whisper_stt_url}/audio/transcriptions"
    files = {"file": ("audio.wav", content, content_type)}
    data = {
        "model": "systran/faster-whisper-base.en",
        "prompt": settings.whisper_stt_prompt,
    }
    try:
        resp = await client.post(url, files=files, data=data)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Whisper transcription timed out")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Whisper service unreachable: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    result = resp.json()
    return TranscribeResponse(text=result.get("text", "").strip())


@router.get("/voices")
async def list_voices():
    return {
        "engines": ["kokoro", "fish"],
        "default_engine": settings.default_tts_engine,
        "default_voice": settings.default_tts_voice,
        "kokoro_voices": [
            {"id": "am_adam", "name": "Adam (Calm American)", "gender": "male"},
            {"id": "am_michael", "name": "Michael (American)", "gender": "male"},
            {"id": "af_bella", "name": "Bella (Warm American)", "gender": "female"},
            {"id": "af_sarah", "name": "Sarah (American)", "gender": "female"},
            {"id": "af_nicole", "name": "Nicole (Whispering/Soft)", "gender": "female"},
            {"id": "bm_george", "name": "George (British Butler)", "gender": "male"},
            {"id": "bf_emma", "name": "Emma (British)", "gender": "female"},
        ],
    }
