"""FastAPI entrypoint: serves the frontend and the /ws voice endpoint.

Run from the project root:
    uvicorn backend.main:app --reload
then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import config
from .session import CoachSession

logger = logging.getLogger("coach.main")

# Emit our structured logs (turn latency, errors, reconnects). Honors LOG_LEVEL.
logging.basicConfig(
    level=config.LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = FastAPI(title="Voice Language Coach")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "llm_provider": config.LLM_PROVIDER,
        "tts": "cartesia" if config.use_cartesia() else "browser",
        "deepgram_key": bool(config.DEEPGRAM_API_KEY),
    }


# --- /ws anti-abuse state ----------------------------------------------------
# In-process counters. SINGLE-WORKER ASSUMPTION: with `uvicorn --workers N` or
# multiple containers each worker keeps its own counters, so the real caps are
# N x the configured values. Move to a shared store (Redis) to scale out
# (see TODOS.md). Documented in PLAN.md (CQ-1).
_active_sessions = 0
_ip_hits: dict[str, list[float]] = {}


def _client_ip(ws: WebSocket) -> str:
    # Behind a proxy/LB, the real client is in X-Forwarded-For; only trust this
    # if your proxy sets it. Without a proxy this header is absent and we fall
    # back to the socket peer.
    xff = ws.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return ws.client.host if ws.client else "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.monotonic()
    hits = [t for t in _ip_hits.get(ip, []) if now - t < 60]
    hits.append(now)
    _ip_hits[ip] = hits
    return len(hits) > config.WS_RATE_PER_MIN_IP


async def _reject(ws: WebSocket, message: str) -> None:
    await ws.accept()
    try:
        await ws.send_json({"type": "error", "message": message})
    finally:
        await ws.close(code=1013)  # 1013 = Try Again Later


async def _session_watchdog(ws: WebSocket) -> None:
    """Warn before, then enforce, the max session duration with a grace window."""
    warn_at = max(0, config.WS_MAX_SESSION_S - 60)
    try:
        await asyncio.sleep(warn_at)
        try:
            await ws.send_json({"type": "info", "message": "Session ends in 1 minute."})
        except Exception:
            return
        await asyncio.sleep(config.WS_MAX_SESSION_S - warn_at)
        try:
            await ws.send_json({"type": "info", "message": "Session time limit reached — ending now."})
        except Exception:
            pass
        await ws.close(code=1000)
    except asyncio.CancelledError:
        pass


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    global _active_sessions
    ip = _client_ip(ws)
    if _rate_limited(ip):
        logger.warning("ws rejected: rate limit ip=%s", ip)
        await _reject(ws, "Too many connections from your network — slow down a moment.")
        return
    if _active_sessions >= config.WS_MAX_CONCURRENT:
        logger.warning("ws rejected: at capacity (%d)", _active_sessions)
        await _reject(ws, "The coach is at capacity right now — please try again shortly.")
        return

    await ws.accept()
    _active_sessions += 1
    session = CoachSession(ws)
    watchdog = asyncio.create_task(_session_watchdog(ws))
    try:
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=config.WS_IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "error", "message": "Session idle — closing."})
                except Exception:
                    pass
                break
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await session.on_audio(msg["bytes"])
            elif msg.get("text") is not None:
                await session.on_control(json.loads(msg["text"]))
    except WebSocketDisconnect:
        pass
    finally:
        watchdog.cancel()
        await session.close()
        _active_sessions -= 1


# Static frontend (mounted last so it doesn't shadow /ws or /health).
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
