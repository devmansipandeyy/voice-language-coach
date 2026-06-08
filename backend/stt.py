"""Streaming speech-to-text via Deepgram's realtime WebSocket API.

We talk to Deepgram over the raw WS protocol (no SDK) so the wire format is
explicit: we stream linear16 PCM up, and receive interim + finalized transcripts
down. Deepgram's `speech_final` flag (driven by endpointing) marks the end of an
utterance — that's our cue to hand the full turn to the LLM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlencode

import websockets

from . import config

logger = logging.getLogger("coach.stt")


class DeepgramSTT:
    def __init__(
        self,
        language: str,
        on_interim: Callable[[str], Awaitable[None]],
        on_final: Callable[[str], Awaitable[None]],
        on_speech_started: Callable[[], Awaitable[None]] | None = None,
        on_reconnect: Callable[[bool], Awaitable[None]] | None = None,
        on_failed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._language = language
        self._on_interim = on_interim
        self._on_final = on_final
        self._on_speech_started = on_speech_started
        # on_reconnect(had_partial): a drop recovered; had_partial=True means an
        # utterance was mid-capture and its audio was lost.
        # on_failed(): reconnection exhausted; the session is no longer hearing.
        self._on_reconnect = on_reconnect
        self._on_failed = on_failed
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._final_segments: list[str] = []
        self._closing = False     # set by close() so we don't reconnect on purpose
        self._connected = False   # gate send_audio so frames don't hit a dead socket
        self._last_voice_ts: float | None = None  # for end-of-turn timing evidence

    async def connect(self) -> None:
        params = {
            "model": "nova-2",
            "language": self._language,
            "encoding": "linear16",
            "sample_rate": config.STT_SAMPLE_RATE,
            "channels": 1,
            "interim_results": "true",
            "smart_format": "true",
            "vad_events": "true",
            # See config: 200ms was too aggressive and fragmented utterances into
            # multiple finals, churning the turn pipeline. ~550ms + utterance_end
            # >=1000ms is the researched sweet spot.
            "endpointing": config.STT_ENDPOINTING_MS,
            "utterance_end_ms": config.STT_UTTERANCE_END_MS,
        }
        url = f"{config.DEEPGRAM_WS_URL}?{urlencode(params)}"
        self._ws = await websockets.connect(
            url, additional_headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"}
        )
        self._connected = True

    async def send_audio(self, pcm: bytes) -> None:
        # Drop frames while disconnected/reconnecting — sending into a dead
        # socket raises and the frames would vanish anyway.
        if self._ws is None or not self._connected:
            return
        try:
            await self._ws.send(pcm)
        except websockets.ConnectionClosed:
            self._connected = False

    async def receive_loop(self) -> None:
        """Dispatch transcript callbacks, reconnecting on unexpected drops.

        On a drop we clear the in-flight utterance state and ask the session to
        tell the learner to repeat (rather than feeding a truncated utterance to
        the LLM). After STT_RECONNECT_MAX failed attempts we surface a hard error.
        """
        while not self._closing:
            try:
                await self._consume()
            except websockets.ConnectionClosed:
                pass
            if self._closing:
                break
            # Either the socket closed mid-stream or _consume returned because
            # the server ended it. Treat both as an unexpected drop and reconnect.
            self._connected = False
            if not await self._reconnect():
                logger.error("STT reconnection exhausted")
                if self._on_failed:
                    await self._on_failed()
                break

    async def _consume(self) -> None:
        """Read one connection to exhaustion, dispatching transcript callbacks."""
        assert self._ws is not None
        async for raw in self._ws:
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "SpeechStarted" and self._on_speech_started:
                await self._on_speech_started()
                continue

            # Reliable end-of-turn: fires after a word-timing gap even if the
            # silence-based `speech_final` didn't (e.g. background noise).
            if mtype == "UtteranceEnd":
                utterance = " ".join(self._final_segments).strip()
                self._final_segments = []
                if utterance:
                    logger.info("END-OF-TURN via UtteranceEnd: %r", utterance)
                    await self._on_final(utterance)
                continue

            if mtype != "Results":
                continue

            alts = msg.get("channel", {}).get("alternatives", [])
            transcript = alts[0].get("transcript", "").strip() if alts else ""

            if transcript:
                # Track the last time we heard words, to measure the silence gap
                # that triggers end-of-turn (evidence for endpointing tuning).
                self._last_voice_ts = time.monotonic()

            if msg.get("is_final") and transcript:
                self._final_segments.append(transcript)

            # interim view = finalized-so-far + current partial
            interim = " ".join(self._final_segments + ([transcript] if transcript else []))
            if interim:
                await self._on_interim(interim.strip())

            if msg.get("speech_final"):
                utterance = " ".join(self._final_segments).strip()
                self._final_segments = []
                if utterance:
                    gap_ms = (
                        int((time.monotonic() - self._last_voice_ts) * 1000)
                        if self._last_voice_ts else -1
                    )
                    logger.info(
                        "END-OF-TURN via speech_final after ~%dms silence "
                        "(endpointing=%dms): %r",
                        gap_ms, config.STT_ENDPOINTING_MS, utterance,
                    )
                    await self._on_final(utterance)

    async def _reconnect(self) -> bool:
        """Reconnect with exponential backoff. Returns True on success.

        Drops the in-flight utterance (its audio is gone) and reports whether a
        capture was interrupted so the session can prompt a repeat.
        """
        had_partial = bool(self._final_segments)
        self._final_segments = []
        for attempt in range(1, config.STT_RECONNECT_MAX + 1):
            delay = (config.STT_RECONNECT_BASE_MS / 1000) * (2 ** (attempt - 1))
            await asyncio.sleep(delay)
            try:
                await self.connect()
            except Exception:
                logger.warning(
                    "STT reconnect attempt %d/%d failed", attempt, config.STT_RECONNECT_MAX
                )
                continue
            logger.info("STT reconnected on attempt %d", attempt)
            if self._on_reconnect:
                await self._on_reconnect(had_partial)
            return True
        return False

    async def close(self) -> None:
        self._closing = True
        self._connected = False
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except websockets.ConnectionClosed:
                pass
            self._ws = None
