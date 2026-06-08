# 🎙️ Voice Language Coach

A real-time, voice-to-voice AI language tutor. Hold a spoken conversation in
Spanish, French, German, or English; the coach adapts to your CEFR level, gently
corrects you mid-conversation, and produces a structured feedback report at the end.

Built **from scratch** — the streaming `STT → LLM → TTS` pipeline is orchestrated
directly over a WebSocket rather than using an off-the-shelf voice framework, to
demonstrate the real-time-systems engineering underneath (interruptions,
sentence-level audio streaming, latency instrumentation).

## Demo

> _[link your 60–90s screen recording here]_

## Architecture

```
 Browser (AudioWorklet, Web Audio)                 FastAPI backend (asyncio)
 ┌────────────────────────────┐                  ┌───────────────────────────────┐
 │ mic ─16kHz PCM16─▶ WebSocket│ ───audio────────▶│ Deepgram  (streaming STT)     │
 │                            │                  │   │ interim + final transcript │
 │ playback ◀─PCM chunks──────│ ◀──audio─────────│   ▼                            │
 │   (gapless scheduler)      │                  │ Gemini Flash (streaming LLM)   │
 └────────────────────────────┘                  │   │ sentence-chunked           │
        ▲  barge-in (SpeechStarted)               │   ▼                            │
        └──────────────────────────────"clear"────│ Cartesia  (streaming TTS)     │
                                                  └───────────────────────────────┘
```

Per turn the backend measures **time-to-first-word** (first LLM token) and
**time-to-first-audio** (first TTS byte) and surfaces them live in the UI.

### Why these choices

| Decision | Rationale |
|---|---|
| Raw WebSocket to Deepgram & Cartesia (no SDK) | The wire protocol is explicit and version-stable; shows understanding of streaming audio. |
| Sentence-level TTS streaming | TTS starts on the first complete sentence instead of waiting for the full reply → much lower latency. |
| Server-driven barge-in via Deepgram `SpeechStarted` | With mic echo-cancellation on, the agent can be interrupted naturally; the in-flight LLM+TTS tasks are cancelled and the client flushes its queue. |
| Provider interface for the LLM (`llm.py`) | Default is Gemini Flash (free tier); switch to Claude with `LLM_PROVIDER=claude`. |
| Structured-output feedback report | The session transcript is scored into a typed JSON schema and rendered as a report card. |

## Setup

```bash
cd voice-language-coach
python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env        # then add your keys
uvicorn backend.main:app --reload
# open http://localhost:8000
```

Keys (all have free tiers):
- **Deepgram** — STT ($200 free credit) · https://console.deepgram.com
- **Gemini** — LLM (free tier) · https://aistudio.google.com/apikey
- **Cartesia** — TTS (optional; free tier) · https://play.cartesia.ai
  Leave `CARTESIA_API_KEY` blank to use the browser's built-in speech synthesis instead.

## Project layout

```
backend/
  main.py      FastAPI app + /ws endpoint + static hosting
  session.py   per-connection orchestration: turn pipeline, barge-in, latency
  stt.py       Deepgram streaming client (raw WS)
  llm.py       provider interface + Gemini / Claude implementations
  tts.py       Cartesia streaming client (raw WS)
  coach.py     languages, CEFR levels, scenarios, prompts, report schema
  config.py    env / model configuration
frontend/
  index.html   UI (Tailwind via CDN)
  app.js       mic capture, WS protocol, gapless PCM playback, report card
```

## Notable engineering details

- **Gapless playback**: incoming PCM chunks are scheduled on a running cursor in a
  dedicated 24 kHz `AudioContext`, so audio plays without gaps or overlap.
- **16 kHz capture in the browser**: an `AudioWorklet` buffers ~100 ms frames and
  converts Float32 → Int16 on the audio thread before sending.
- **Cancellation safety**: a turn is one `asyncio.Task`; barge-in cancels it and the
  partial spoken text is still committed to history so context stays coherent.
