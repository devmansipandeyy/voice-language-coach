"""Central configuration loaded from environment / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level up from backend/).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# --- API keys ---
DEEPGRAM_API_KEY: str = os.getenv("DEEPGRAM_API_KEY", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
CARTESIA_API_KEY: str = os.getenv("CARTESIA_API_KEY", "")

# --- Providers / models ---
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini").lower()
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# --- Audio ---
STT_SAMPLE_RATE: int = int(os.getenv("STT_SAMPLE_RATE", "16000"))
TTS_SAMPLE_RATE: int = int(os.getenv("TTS_SAMPLE_RATE", "24000"))

# --- Conversation ---
# Cap the messages sent to the LLM each turn so latency/token cost stays flat
# in long sessions (see PLAN.md F5/T6). Counts individual history entries
# (user + model), not exchanges.
HISTORY_MAX_TURNS: int = int(os.getenv("HISTORY_MAX_TURNS", "12"))

# Stall guards: without these a hung provider freezes the whole turn forever
# (the "stuck mid-response" symptom). On timeout the turn aborts and the learner
# gets a clear message instead of silence.
# Max wait for the NEXT LLM token (covers TTFT and mid-stream stalls).
LLM_TOKEN_TIMEOUT_S: float = float(os.getenv("LLM_TOKEN_TIMEOUT_S", "20"))
# Max wait for one sentence's TTS synthesis (Cartesia opens a WS per sentence).
TTS_TIMEOUT_S: float = float(os.getenv("TTS_TIMEOUT_S", "20"))

# --- Full-duplex barge-in (PLAN.md F1/T1, behind a flag) ---
# When True the mic stays live during playback and barge-in fires on a real
# learner partial. When False the client gates the mic during playback
# (today's half-duplex) and only the Interrupt button cuts the coach off.
FULL_DUPLEX: bool = os.getenv("FULL_DUPLEX", "false").lower() in ("1", "true", "yes")
# A partial transcript must have at least this many recognized words to count
# as a barge-in (rejects stray single-word noise).
BARGEIN_MIN_WORDS: int = int(os.getenv("BARGEIN_MIN_WORDS", "2"))
# Reject a partial as echo if its token-overlap with the agent's in-flight TTS
# text is >= this fraction. This is what actually rejects the agent hearing
# itself, since leaked TTS transcribes as real words. Lower = more eager to
# treat speech as a real barge-in.
BARGEIN_ECHO_OVERLAP: float = float(os.getenv("BARGEIN_ECHO_OVERLAP", "0.5"))

# --- STT reconnection (PLAN.md F2/T2) ---
STT_RECONNECT_MAX: int = int(os.getenv("STT_RECONNECT_MAX", "3"))
STT_RECONNECT_BASE_MS: int = int(os.getenv("STT_RECONNECT_BASE_MS", "500"))

# --- /ws anti-abuse (PLAN.md F3/T3) ---
# NOTE: these counters are in-process. Deploy as a SINGLE worker/instance.
# With `uvicorn --workers N` each worker keeps its own counter, so the real
# cap becomes N x the configured value. Move to a shared store (Redis) only
# when scaling out (see TODOS.md).
WS_MAX_CONCURRENT: int = int(os.getenv("WS_MAX_CONCURRENT", "50"))
WS_RATE_PER_MIN_IP: int = int(os.getenv("WS_RATE_PER_MIN_IP", "5"))
WS_IDLE_TIMEOUT_S: int = int(os.getenv("WS_IDLE_TIMEOUT_S", "60"))
WS_MAX_SESSION_S: int = int(os.getenv("WS_MAX_SESSION_S", "900"))

# --- Pronunciation clarity (PLAN.md E5/T10, behind a flag) ---
PRONUNCIATION: bool = os.getenv("PRONUNCIATION", "false").lower() in ("1", "true", "yes")

# --- Cartesia ---
CARTESIA_MODEL: str = os.getenv("CARTESIA_MODEL", "sonic-2")
CARTESIA_WS_URL: str = "wss://api.cartesia.ai/tts/websocket"
CARTESIA_VERSION: str = "2024-11-13"

# --- Deepgram ---
DEEPGRAM_WS_URL: str = "wss://api.deepgram.com/v1/listen"

# --- Deepgram turn detection ---
# Silence (ms) that ends the learner's turn. PROVEN by a probe against live
# Deepgram (synthesized "I went to the store <900ms pause> yesterday..."):
#   endpointing=550  -> the sentence was SPLIT at the pause (learner cut off)
#   endpointing=1500 -> the whole sentence was captured as one turn
# Language learners pause mid-sentence to think, so the threshold must be
# forgiving. 1500ms costs ~1.5s of response latency but stops the cut-offs.
# endpointing (silence) and utterance_end_ms (word-gap fallback) are kept equal
# so neither fires early. Tunable: raise toward 2000 for slower speakers, lower
# toward 1000 for snappier turns.
STT_ENDPOINTING_MS: int = int(os.getenv("STT_ENDPOINTING_MS", "1500"))
STT_UTTERANCE_END_MS: int = int(os.getenv("STT_UTTERANCE_END_MS", "1500"))

# --- Logging ---
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()


def use_cartesia() -> bool:
    """TTS goes through Cartesia only when a key is present; else browser TTS."""
    return bool(CARTESIA_API_KEY)
