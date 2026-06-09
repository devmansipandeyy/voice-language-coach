# 🎙️ Voice Language Coach

A real-time, voice-to-voice AI language tutor. Hold a spoken conversation in
Spanish, French, German, or English; the coach adapts to your CEFR level, gently
corrects you mid-conversation, and produces a structured feedback report at the end.

Built **from scratch** — the streaming `STT → LLM → TTS` pipeline is orchestrated
directly over a single WebSocket rather than using an off-the-shelf voice
framework, to make the real-time-systems engineering explicit: turn detection,
sentence-level audio streaming, barge-in, reconnection, and latency
instrumentation.

## Demo

> _[link your 60–90s screen recording here]_

---

## Architecture

A **cascading (turn-based) pipeline**: speech in → text → reply → speech out.
Everything between the browser and the providers runs in one `asyncio` process,
and the browser talks to it over one WebSocket carrying both JSON control
messages and binary audio.

```
 Browser (Web Audio / AudioWorklet)              FastAPI backend (asyncio)
 ┌────────────────────────────────┐            ┌────────────────────────────────────┐
 │ mic ─16 kHz PCM16─▶ WebSocket   │ ──audio──▶ │ Deepgram nova-2  (streaming STT)     │
 │                                │            │   │ interim + final + end-of-turn    │
 │ playback ◀─PCM chunks── WS ◀────│ ◀─audio─── │   ▼                                  │
 │   (gapless 24 kHz scheduler)    │            │ CoachSession  (turn orchestrator)    │
 │                                │            │   ├─ sentence-chunked streaming      │
 │ barge-in / Interrupt ──control──│──────────▶ │   ├─ latency timers (TTFT / TTFA)    │
 └────────────────────────────────┘            │   ├─ history window                  │
                                                │   ▼                                  │
                                                │ Gemini Flash / Claude (streaming LLM)│
                                                │   │ token stream                     │
                                                │   ▼                                  │
                                                │ Cartesia sonic-2  (streaming TTS)    │
                                                │   (or browser SpeechSynthesis)       │
                                                └────────────────────────────────────┘
```

### Why this architecture

| Decision | Rationale |
|---|---|
| **Cascading STT→LLM→TTS** (not a single speech-to-speech model) | Each stage is swappable and independently tunable, you can read/score the transcript (needed for the feedback report and inline corrections), and you control turn-taking and barge-in directly. Speech-to-speech models hide all of that. |
| **One WebSocket, JSON + binary frames** | The browser streams raw PCM up and receives PCM down on the same connection that carries `start`/`interrupt`/`transcript`/`report` control messages. One connection, one lifecycle, no polling. |
| **Raw provider WebSockets, no SDK** (Deepgram & Cartesia) | The wire protocol is explicit and version-stable, and there's nothing between the code and the bytes — which matters when you're chasing latency and debugging streaming. |
| **Provider-agnostic LLM layer** (`llm.py`) | Default is Gemini Flash (free tier, fast); switch to Claude with `LLM_PROVIDER=claude`. The orchestrator depends on a 2-method `LLMProvider` contract, nothing provider-specific. |
| **Built from scratch** | The point of the project is the real-time plumbing — turn detection, sentence streaming, barge-in, gapless playback — so it's implemented directly rather than delegated to Pipecat/LiveKit. |
| **Structured-output feedback report** | The transcript is scored into a typed JSON schema (Gemini `response_schema`) and rendered as a report card. |

---

## How STT, LLM and TTS are bound together

The whole conversation is driven by `backend/session.py` (`CoachSession`), one
instance per WebSocket connection. The binding is the **turn**.

**1. A turn begins when Deepgram says the learner stopped talking.**
`stt.py` streams the learner's PCM to Deepgram and listens for end-of-turn,
combining Deepgram's two independent signals ([their recommended approach](https://developers.deepgram.com/docs/understanding-end-of-speech-detection)):
`speech_final` (silence-based endpointing) and `UtteranceEnd` (word-gap based,
robust to background noise). Either one fires `on_final(utterance)`.

**2. `on_final` hands the utterance to the LLM and streams the reply into the TTS.**
This is the core loop in `_agent_turn`:

```
LLM token stream ──▶ accumulate into a sentence buffer
                        │  on each complete sentence (. ! ? 。 ！ ？)
                        ▼
                     TTS synthesize(sentence) ──▶ PCM chunks ──▶ WebSocket ──▶ browser
```

The LLM is consumed **token by token**; as soon as a full sentence forms it is
flushed to Cartesia, whose audio chunks are forwarded to the browser the instant
they arrive. The agent starts *speaking the first sentence while still
generating the rest of the reply*. Text also streams to the UI in parallel
(`agent_text` messages) so the user sees the words as they're spoken.

**3. The conversation context is provider-neutral.**
History is stored as `{role, text}` and translated to each provider's format in
`llm.py`. The full transcript is kept separately for the end-of-session report,
while only a recent **window** of turns is sent to the LLM each turn (see latency,
below).

**4. Barge-in cancels the turn.**
A turn is a single `asyncio.Task`. Interrupting it (`_cancel_agent`) cancels the
in-flight LLM + TTS work and tells the client to flush its playback queue. The
partial reply that was actually spoken is still committed to history, so context
stays coherent.

### The WebSocket protocol

Client → server: `start` (language/level/scenario), `interrupt`, `end_session`,
plus raw PCM16 binary frames.
Server → client: `ready`, `transcript` (interim/final), `agent_text`,
`audio_start` / binary PCM / `audio_end` (or `speak` for the browser-TTS
fallback), `mic` (half-duplex gate), `clear` (flush playback on barge-in),
`latency`, `info`, `error`, `report`.

---

## How we keep latency low

Latency in a cascading pipeline is dominated by *waiting for each stage to
finish before starting the next*. The whole design is about **overlapping the
stages** so perceived latency is closer to `max(stage)` than `sum(stages)`.

- **Stream everything.** STT emits interim results live; the LLM is consumed as a
  token stream; TTS audio is forwarded chunk-by-chunk. No stage waits for the
  previous one to fully complete.
- **Sentence-level TTS.** TTS starts on the *first complete sentence* instead of
  the full reply, so audio begins seconds earlier on multi-sentence answers.
- **Thinking disabled on the LLM.** Gemini 2.x "thinks" before answering, adding
  seconds of first-token latency. For a voice turn we set `thinking_budget=0` and
  cap `max_output_tokens` so replies stay short and start fast.
- **Gapless playback.** Incoming 24 kHz PCM chunks are scheduled on a running
  cursor in a dedicated `AudioContext`, so audio plays continuously with no gaps
  or overlap even as chunks arrive piecemeal.
- **16 kHz capture on the audio thread.** An `AudioWorklet` buffers ~100 ms frames
  and converts Float32 → Int16 off the main thread before sending.
- **History windowing.** Only the last `HISTORY_MAX_TURNS` messages go to the LLM,
  so prompt size (and therefore time-to-first-token) stays flat no matter how
  long the conversation runs.
- **Measured, not assumed.** Every turn records **time-to-first-word** (first LLM
  token) and **time-to-first-audio** (first TTS byte) using a monotonic clock;
  both are shown live in the UI and logged server-side for percentile tracking.

---

## Turn detection & barge-in

Getting *when the learner is done talking* right is the hardest part of a voice
agent, and the most common failure is cutting the user off mid-sentence.

- **Endpointing is tuned for language learners.** A probe against live Deepgram
  showed a ~900 ms mid-sentence "thinking" pause was enough to split one sentence
  into two turns at an aggressive threshold. Learners pause to compose sentences,
  so the silence threshold (`STT_ENDPOINTING_MS`, default **1500 ms**) is
  deliberately forgiving. It's a single env var — lower it for snappier turns,
  raise it for slower speakers.
- **Half-duplex by default (reliable).** While the coach is speaking, the server
  authoritatively mutes the mic (`mic` control message). This guarantees the
  agent never transcribes its own voice. Interrupt with the **✋ button**.
- **Full-duplex barge-in (optional).** Set `FULL_DUPLEX=true` to keep the mic live
  during playback so you can interrupt by voice. An **echo guard** distinguishes a
  real interruption from the coach hearing itself: a partial transcript only
  counts as barge-in if it has enough words *and* low token-overlap with what the
  coach is currently saying. Works best with headphones (open speakers can leak
  the coach's voice into the mic).

---

## Reliability & safety

- **STT reconnection.** If the Deepgram socket drops mid-session it reconnects
  with exponential backoff; if it was mid-utterance, the truncated audio is
  dropped and the learner is asked to repeat rather than fed a half sentence. No
  silent failures.
- **Stall guards.** Per-token LLM and per-sentence TTS timeouts mean a wedged
  provider aborts the turn with a clear message instead of hanging forever.
- **Abuse protection on `/ws`.** Per-IP rate limiting, a concurrency cap, an idle
  timeout, and a max-session limit (with a warning + grace window) protect API
  credits. _Counters are in-process — deploy as a single worker, or move to a
  shared store to scale out._
- **XSS-safe report.** LLM output rendered into the report card is HTML-escaped;
  the report schema is enforced and generation retries once on malformed JSON.

---

## Project layout

```
backend/
  main.py      FastAPI app, /ws endpoint, /ws abuse guards, static hosting
  session.py   per-connection orchestration: turn pipeline, barge-in, latency, echo guard
  stt.py       Deepgram streaming client (raw WS) + reconnection
  llm.py       provider interface + Gemini / Claude implementations
  tts.py       Cartesia streaming client (raw WS) with timeout
  coach.py     languages, CEFR levels, scenarios, prompts, report schema
  config.py    env / model / tuning configuration
  tests/       pytest suite (turn-taking, echo guard, STT parsing, reconnect, report)
frontend/
  index.html   UI (custom CSS, Fraunces + Hanken Grotesk, no framework)
  app.js       mic capture, WS protocol, gapless PCM playback, report card
```

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
  Leave `CARTESIA_API_KEY` blank to use the browser's built-in speech synthesis.

## Configuration

All knobs are environment variables (see `backend/config.py` for the full list):

| Variable | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `claude` |
| `STT_ENDPOINTING_MS` | `1500` | silence (ms) that ends the learner's turn — lower = snappier, higher = fewer cut-offs |
| `STT_UTTERANCE_END_MS` | `1500` | word-gap fallback end-of-turn (min 1000) |
| `FULL_DUPLEX` | `false` | `true` keeps the mic live so you can interrupt by voice (use headphones) |
| `BARGEIN_ECHO_OVERLAP` | `0.5` | full-duplex echo guard sensitivity |
| `HISTORY_MAX_TURNS` | `12` | messages of context sent to the LLM each turn |
| `LLM_TOKEN_TIMEOUT_S` / `TTS_TIMEOUT_S` | `20` | stall guards |
| `WS_MAX_CONCURRENT` / `WS_RATE_PER_MIN_IP` | `50` / `5` | `/ws` abuse limits |
| `WS_IDLE_TIMEOUT_S` / `WS_MAX_SESSION_S` | `60` / `900` | session limits |

## Tests

```bash
pytest        # 25 tests: turn-taking, echo guard, STT parsing, reconnect, report parsing
```

---

## Notable engineering details

- **Gapless playback:** PCM chunks are scheduled on a running cursor in a 24 kHz
  `AudioContext`, so audio plays without gaps or overlap.
- **16 kHz capture in the browser:** an `AudioWorklet` buffers ~100 ms frames and
  converts Float32 → Int16 on the audio thread before sending.
- **Cancellation safety:** a turn is one `asyncio.Task`; barge-in cancels it and the
  partial spoken text is still committed to history so context stays coherent.
- **Drift-free timing:** latency is read via `loop.time()` (a monotonic clock).
