"""Deepgram STT message parsing (_consume) and reconnection (_reconnect)."""
import json

import pytest

from backend.stt import DeepgramSTT


class FakeWS:
    """Minimal async-iterable stand-in for the Deepgram websocket."""

    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


async def _noop(*_args):
    pass


@pytest.mark.asyncio
async def test_consume_dispatches_speech_started_and_final():
    finals, interims, started = [], [], []

    async def on_interim(t):
        interims.append(t)

    async def on_final(t):
        finals.append(t)

    async def on_started():
        started.append(True)

    stt = DeepgramSTT("es", on_interim, on_final, on_started)
    stt._ws = FakeWS([
        json.dumps({"type": "SpeechStarted"}),
        json.dumps({"type": "Results", "is_final": True,
                    "channel": {"alternatives": [{"transcript": "hola"}]}}),
        json.dumps({"type": "Results", "speech_final": True,
                    "channel": {"alternatives": [{"transcript": ""}]}}),
    ])

    await stt._consume()

    assert started == [True]
    assert finals == ["hola"]
    assert interims[-1] == "hola"


@pytest.mark.asyncio
async def test_consume_utterance_end_flushes_segments():
    finals = []

    async def on_final(t):
        finals.append(t)

    stt = DeepgramSTT("es", _noop, on_final)
    stt._ws = FakeWS([
        json.dumps({"type": "Results", "is_final": True,
                    "channel": {"alternatives": [{"transcript": "buenos dias"}]}}),
        json.dumps({"type": "UtteranceEnd"}),
    ])

    await stt._consume()

    assert finals == ["buenos dias"]
    assert stt._final_segments == []


@pytest.mark.asyncio
async def test_reconnect_clears_partial_and_signals(monkeypatch):
    events = []

    async def on_reconnect(had_partial):
        events.append(had_partial)

    stt = DeepgramSTT("es", _noop, _noop, on_reconnect=on_reconnect)
    stt._final_segments = ["half an utter"]  # mid-capture when the socket dropped

    async def instant(_seconds):
        return None

    async def fake_connect():
        stt._connected = True

    monkeypatch.setattr("backend.stt.asyncio.sleep", instant)
    monkeypatch.setattr(stt, "connect", fake_connect)

    assert await stt._reconnect() is True
    assert stt._final_segments == []      # truncated utterance dropped (ARCH-2)
    assert events == [True]               # session told to prompt a repeat


@pytest.mark.asyncio
async def test_reconnect_exhausts_and_returns_false(monkeypatch):
    stt = DeepgramSTT("es", _noop, _noop)

    async def instant(_seconds):
        return None

    async def always_fail():
        raise RuntimeError("still down")

    monkeypatch.setattr("backend.stt.asyncio.sleep", instant)
    monkeypatch.setattr(stt, "connect", always_fail)

    assert await stt._reconnect() is False
