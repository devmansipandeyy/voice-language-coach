"""Streaming text-to-speech via Cartesia's realtime WebSocket API.

`synthesize()` is an async generator that yields raw PCM (s16le) chunks as the
audio is produced, so the orchestrator can forward audio to the browser the
moment the first chunk lands — minimizing time-to-first-audio.

If no CARTESIA_API_KEY is set, the backend skips this entirely and the frontend
speaks the text with the browser's built-in SpeechSynthesis (zero-cost fallback).
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import AsyncIterator

import websockets

from . import config
from .coach import LANGUAGES


class CartesiaTTS:
    async def synthesize(self, text: str, language: str) -> AsyncIterator[bytes]:
        lang = LANGUAGES.get(language, LANGUAGES["es"])
        url = (
            f"{config.CARTESIA_WS_URL}"
            f"?api_key={config.CARTESIA_API_KEY}"
            f"&cartesia_version={config.CARTESIA_VERSION}"
        )
        payload = {
            "model_id": config.CARTESIA_MODEL,
            "transcript": text,
            "voice": {"mode": "id", "id": lang.cartesia_voice},
            "language": lang.tts_language,
            "context_id": uuid.uuid4().hex,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": config.TTS_SAMPLE_RATE,
            },
            "add_timestamps": False,
        }
        # Bound the whole synth (connect + stream) so a slow or wedged Cartesia
        # socket can't hang the turn forever. On timeout this raises
        # TimeoutError, which the session surfaces as a recoverable error.
        async with asyncio.timeout(config.TTS_TIMEOUT_S):
            async with websockets.connect(url, max_size=None) as ws:
                await ws.send(json.dumps(payload))
                async for raw in ws:
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "chunk" and msg.get("data"):
                        yield base64.b64decode(msg["data"])
                    elif mtype in ("done", "error"):
                        if mtype == "error":
                            raise RuntimeError(f"Cartesia error: {msg.get('error')}")
                        break
