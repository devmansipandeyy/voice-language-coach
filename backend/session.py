"""Per-connection orchestration: wires STT -> LLM -> TTS for one learner.

Responsibilities:
  - Drive a full spoken turn: stream LLM tokens, chunk them into sentences, and
    stream each sentence through TTS so audio starts as early as possible.
  - Barge-in: if the learner starts speaking while the agent is talking, cancel
    the in-flight turn and tell the client to flush its audio queue.
  - Latency metrics: per-turn time-to-first-token and time-to-first-audio.

Time is read via loop.time() (a monotonic clock) so metrics are drift-free.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from fastapi import WebSocket

from . import coach, config
from .llm import get_provider
from .stt import DeepgramSTT
from .tts import CartesiaTTS

logger = logging.getLogger("coach.session")

# Sentence boundary for streaming TTS (handles Latin + CJK terminal punctuation).
_SENTENCE_END = re.compile(r"[.!?。！？]+[\s\"')\]]*")

# Word tokens for the full-duplex echo guard. Unicode \w keeps accented and
# CJK characters so the overlap check works across the supported languages.
_WORD = re.compile(r"\w+", re.UNICODE)


def _tokenize(s: str) -> list[str]:
    return _WORD.findall(s.lower())


def echo_is_real_barge_in(
    partial: str, current_tts: str, min_words: int, echo_overlap: float
) -> bool:
    """Pure decision for the full-duplex echo guard (extracted for testing).

    True when `partial` looks like the learner interrupting, False when it looks
    like the agent's own speech leaking back through the mic. Requires enough
    words AND low token-overlap with the agent's in-flight speech `current_tts`.
    """
    words = _tokenize(partial)
    if len(words) < min_words:
        return False
    tts_tokens = set(_tokenize(current_tts))
    if not tts_tokens:
        return True  # agent isn't speaking words yet → treat as real
    overlap = sum(1 for w in words if w in tts_tokens) / len(words)
    return overlap < echo_overlap


class CoachSession:
    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        # We're constructed inside the running /ws handler, so a loop is live.
        self._loop = asyncio.get_running_loop()

        # conversation config (set on "start")
        self.language = "es"
        self.level = "A2"
        self.scenario = "free"
        self.corrections = True
        self._system = ""

        self._llm = get_provider()
        self._tts = CartesiaTTS() if config.use_cartesia() else None
        self._stt: DeepgramSTT | None = None
        self._stt_task: asyncio.Task | None = None

        self.history: list[dict] = []        # [{role, text}] for the LLM
        self.transcript: list[str] = []       # ["LEARNER: ..", "COACH: .."] for the report

        self._agent_task: asyncio.Task | None = None
        self._speaking = False
        # The agent's currently-spoken text. The full-duplex echo guard reads
        # this from the STT callback to tell a real learner barge-in apart from
        # the agent hearing its own voice. Lock-free: single event loop, whole-
        # string assignment, no await between the guard's read and use (ARCH-1).
        self._current_tts = ""

    # --- client I/O helpers --------------------------------------------------
    async def _send(self, **payload) -> None:
        await self._ws.send_json(payload)

    # --- lifecycle -----------------------------------------------------------
    async def start(self, msg: dict) -> None:
        self.language = msg.get("language", "es")
        self.level = msg.get("level", "A2")
        self.scenario = msg.get("scenario", "free")
        self.corrections = bool(msg.get("corrections", True))
        self._system = coach.system_prompt(
            self.language, self.level, self.scenario, self.corrections
        )

        lang = coach.LANGUAGES.get(self.language, coach.LANGUAGES["es"])
        self._stt = DeepgramSTT(
            language=lang.stt_code,
            on_interim=self._on_interim,
            on_final=self._on_final,
            on_speech_started=self._on_speech_started,
            on_reconnect=self._on_stt_reconnect,
            on_failed=self._on_stt_failed,
        )
        try:
            await self._stt.connect()
        except Exception:
            logger.exception("STT initial connect failed")
            await self._send(
                type="error",
                message="Couldn't reach the speech service. Check your connection and restart.",
            )
            return
        self._stt_task = asyncio.create_task(self._stt.receive_loop())

        await self._send(
            type="ready",
            tts="cartesia" if self._tts else "browser",
            full_duplex=config.FULL_DUPLEX,
        )
        # Proactively greet the learner.
        self._agent_task = asyncio.create_task(self._agent_turn("Begin the conversation now.", greeting=True))

    async def on_audio(self, pcm: bytes) -> None:
        if self._stt is not None:
            await self._stt.send_audio(pcm)

    async def on_control(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "start":
            await self.start(msg)
        elif mtype == "interrupt":
            await self._barge_in()
        elif mtype == "end_session":
            await self._end_session()

    async def close(self) -> None:
        await self._cancel_agent()
        if self._stt_task:
            self._stt_task.cancel()
        if self._stt:
            await self._stt.close()

    # --- STT callbacks -------------------------------------------------------
    async def _on_interim(self, text: str) -> None:
        await self._send(type="transcript", text=text, final=False)
        # Full-duplex barge-in: the mic stays live while the coach speaks, so we
        # decide barge-in from the partial transcript (not bare SpeechStarted,
        # which the agent's own audio would trigger).
        if config.FULL_DUPLEX and self._speaking and self._is_real_barge_in(text):
            await self._barge_in()

    async def _on_speech_started(self) -> None:
        # Half-duplex only: the client gates the mic during playback, so a
        # SpeechStarted here means the learner really spoke. In full-duplex we
        # ignore this and rely on the partial-transcript guard in _on_interim.
        if not config.FULL_DUPLEX and self._speaking:
            await self._barge_in()

    def _is_real_barge_in(self, partial: str) -> bool:
        return echo_is_real_barge_in(
            partial, self._current_tts,
            config.BARGEIN_MIN_WORDS, config.BARGEIN_ECHO_OVERLAP,
        )

    async def _on_stt_reconnect(self, had_partial: bool) -> None:
        # The speech socket dropped and recovered. If we were mid-utterance the
        # audio is gone, so ask the learner to repeat rather than respond to a
        # truncated sentence.
        if had_partial:
            await self._send(
                type="info",
                message="Lost you for a second — please say that again.",
            )

    async def _on_stt_failed(self) -> None:
        await self._send(
            type="error",
            message="Lost the speech connection. Please restart the session.",
        )

    async def _on_final(self, utterance: str) -> None:
        await self._send(type="transcript", text=utterance, final=True)
        self.transcript.append(f"LEARNER: {utterance}")
        # Replace any in-flight agent turn with a response to the new utterance.
        await self._cancel_agent()
        self._agent_task = asyncio.create_task(self._agent_turn(utterance))

    # --- barge-in ------------------------------------------------------------
    async def _barge_in(self) -> None:
        await self._cancel_agent()
        await self._send(type="clear")  # tell client to flush its playback queue

    async def _cancel_agent(self) -> None:
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass
        self._speaking = False

    # --- the core turn -------------------------------------------------------
    async def _agent_turn(self, user_text: str, greeting: bool = False) -> None:
        t0 = self._loop.time()
        ttft_ms: int | None = None
        ttfa_ms: int | None = None
        spoken = ""        # text actually emitted (for history on cancel)
        pending = ""       # buffer awaiting a sentence boundary

        try:
            self._speaking = True
            # Window the history sent to the LLM so latency and token cost stay
            # flat in long sessions. The full transcript is kept separately
            # (self.transcript) for the end-of-session report.
            window = self.history[-config.HISTORY_MAX_TURNS:]
            # Manual iteration so we can bound the wait for each token. A hung
            # LLM stream would otherwise freeze the turn forever ("stuck").
            agen = self._llm.stream_reply(self._system, window, user_text).__aiter__()
            while True:
                try:
                    token = await asyncio.wait_for(
                        agen.__anext__(), config.LLM_TOKEN_TIMEOUT_S
                    )
                except StopAsyncIteration:
                    break
                if ttft_ms is None:
                    ttft_ms = int((self._loop.time() - t0) * 1000)
                spoken += token
                pending += token
                # Expose the in-flight speech to the echo guard (ARCH-1).
                self._current_tts = spoken
                await self._send(type="agent_text", text=spoken, done=False)

                # Flush complete sentences to TTS as soon as they form.
                while (m := _SENTENCE_END.search(pending)):
                    sentence = pending[: m.end()].strip()
                    pending = pending[m.end():]
                    if sentence:
                        ttfa_ms = await self._speak(sentence, ttfa_ms, t0)

            # Flush any trailing partial sentence.
            if pending.strip():
                ttfa_ms = await self._speak(pending.strip(), ttfa_ms, t0)

            await self._send(type="agent_text", text=spoken, done=True)
            await self._send(
                type="latency",
                ttft_ms=ttft_ms or 0,
                ttfa_ms=ttfa_ms or 0,
            )
            # Persist latency server-side (the UI badge is ephemeral). Structured
            # key=value fields so a log aggregator can chart TTFT/TTFA percentiles.
            logger.info(
                "turn latency ttft_ms=%d ttfa_ms=%d lang=%s level=%s scenario=%s greeting=%s",
                ttft_ms or 0, ttfa_ms or 0,
                self.language, self.level, self.scenario, greeting,
            )
            self._commit_turn(user_text, spoken, greeting)
        except asyncio.CancelledError:
            # Keep what was actually spoken so the conversation stays coherent.
            if spoken.strip():
                self._commit_turn(user_text, spoken, greeting)
            raise
        except (asyncio.TimeoutError, TimeoutError):
            # A provider (LLM token wait or TTS synth) stalled past its timeout.
            logger.warning("turn timed out (user_text=%r)", user_text)
            if spoken.strip():
                self._commit_turn(user_text, spoken, greeting)
            await self._send(
                type="error",
                message="The coach took too long to respond. Please try again.",
            )
        except Exception:
            # Two provider SDKs (Gemini, Claude) plus TTS/websocket raise an
            # open-ended set of exception types, so a final catch-all is the
            # honest degrade path. The rule we DO keep: never swallow silently —
            # log full context (traceback + the utterance we were answering) and
            # surface a clear message to the learner.
            logger.exception("agent turn failed (user_text=%r)", user_text)
            await self._send(
                type="error",
                message="The coach hit a problem responding. Try saying that again.",
            )
        finally:
            self._speaking = False
            self._current_tts = ""

    async def _speak(self, sentence: str, ttfa_ms: int | None, t0: float) -> int | None:
        """Render one sentence to audio (Cartesia) or hand it to the browser TTS."""
        if self._tts is None:
            # Browser SpeechSynthesis fallback.
            if ttfa_ms is None:
                ttfa_ms = int((self._loop.time() - t0) * 1000)
            await self._send(type="speak", text=sentence)
            return ttfa_ms

        await self._send(type="audio_start", sample_rate=config.TTS_SAMPLE_RATE)
        chunks = 0
        async for chunk in self._tts.synthesize(sentence, self.language):
            chunks += 1
            if ttfa_ms is None:
                ttfa_ms = int((self._loop.time() - t0) * 1000)
            await self._ws.send_bytes(chunk)
        await self._send(type="audio_end")
        if chunks == 0:
            # Text was shown but no audio came back — the "text, no speech"
            # symptom. Log it so a recurrence is diagnosable.
            logger.warning("TTS produced no audio for sentence=%r", sentence)
        return ttfa_ms

    def _commit_turn(self, user_text: str, agent_text: str, greeting: bool) -> None:
        if not greeting:
            self.history.append({"role": "user", "text": user_text})
        self.history.append({"role": "model", "text": agent_text})
        self.transcript.append(f"COACH: {agent_text}")

    # --- report --------------------------------------------------------------
    async def _end_session(self) -> None:
        await self._cancel_agent()
        await self._send(type="info", message="Generating your feedback report…")
        transcript = "\n".join(self.transcript) or "(no conversation took place)"
        prompt = coach.report_prompt(self.language, self.level, transcript)
        try:
            data = await self._build_report(prompt)
            await self._send(type="report", data=data)
        except Exception:
            logger.exception("report generation failed after retry")
            await self._send(
                type="error",
                message="Could not build your feedback report. Your conversation still happened — try ending again.",
            )

    async def _build_report(self, prompt: str) -> dict:
        """Generate the report, retrying once on malformed JSON.

        LLMs occasionally emit invalid JSON. One retry recovers the common
        transient case; a second failure propagates to the caller, which
        surfaces a clear message rather than failing silently.
        """
        try:
            return await self._llm.generate_report(prompt, coach.REPORT_SCHEMA)
        except json.JSONDecodeError:
            logger.warning("report JSON malformed, retrying once")
            return await self._llm.generate_report(prompt, coach.REPORT_SCHEMA)
