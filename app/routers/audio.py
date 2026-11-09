from collections.abc import AsyncIterator
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings

router = APIRouter()


class SpeechRequest(BaseModel):
    text: str
    voice: str | None = None
    engine: Literal["kokoro", "fish"] | None = None
    format: str = "mp3"


class TranscribeResponse(BaseModel):
    text: str


async def _stream_kokoro(text: str, voice: str, response_format: str) -> AsyncIterator[bytes]:
    payload = {
        "model": "kokoro",
        "voice": voice,
        "input": text,
        "response_format": response_format,
    }
    client = httpx.AsyncClient(timeout=30.0)
    try:
        url = f"{settings.kokoro_tts_url}/audio/speech"
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                detail = body.decode(errors="ignore")
                raise HTTPException(status_code=resp.status_code, detail=detail)
            async for chunk in resp.aiter_bytes(chunk_size=4096):
                yield chunk
    finally:
        await client.aclose()


async def _stream_fish(
    text: str, voice: str | None, response_format: str
) -> AsyncIterator[bytes]:
    headers = {
        "Authorization": f"Bearer {settings.fish_audio_api_key}",
        "Content-Type": "application/json",
        "model": "drama-3",
    }
    payload = {
        "text": text,
        "reference_id": voice if voice else None,
        "format": response_format,
        "streaming": True,
    }
    client = httpx.AsyncClient(timeout=30.0)
    try:
        url = "https://api.fish.audio/v1/tts"
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                detail = body.decode(errors="ignore")
                raise HTTPException(status_code=resp.status_code, detail=detail)
            async for chunk in resp.aiter_bytes(chunk_size=4096):
                yield chunk
    finally:
        await client.aclose()


@router.post("/speech")
async def synthesize_speech(body: SpeechRequest):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    engine = body.engine or settings.default_tts_engine
    voice = body.voice or settings.default_tts_voice
    media_type = "audio/mpeg" if body.format == "mp3" else "audio/wav"

    if engine == "kokoro":
        stream = _stream_kokoro(body.text, voice, body.format)
        return StreamingResponse(stream, media_type=media_type)
    if engine == "fish":
        if not settings.fish_audio_api_key:
            raise HTTPException(status_code=503, detail="Fish Audio API key not configured")
        stream = _stream_fish(body.text, body.voice, body.format)
        return StreamingResponse(stream, media_type=media_type)

    raise HTTPException(status_code=400, detail=f"Unsupported TTS engine: {engine}")


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_audio(request: Request):
    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio content")

    content_type = request.headers.get("content-type", "audio/wav")
    client = httpx.AsyncClient(timeout=30.0)
    try:
        url = f"{settings.whisper_stt_url}/audio/transcriptions"
        files = {"file": ("audio.wav", content, content_type)}
        data = {
            "model": "systran/faster-whisper-base.en",
            "prompt": settings.whisper_stt_prompt,
        }
        resp = await client.post(url, files=files, data=data)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        result = resp.json()
        return TranscribeResponse(text=result.get("text", "").strip())
    finally:
        await client.aclose()


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
