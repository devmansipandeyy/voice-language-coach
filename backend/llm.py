"""Provider-agnostic LLM layer.

`LLMProvider` defines the contract the session orchestrator depends on:
  - stream_reply(): stream the spoken reply token-by-token (low latency).
  - generate_report(): one-shot structured JSON for the feedback report.

Default impl = Gemini Flash (free tier, fast). A Claude impl is included so the
provider is a one-line swap via LLM_PROVIDER=claude. Selected by `get_provider()`.
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Protocol

from . import config


# --- message helpers ---------------------------------------------------------
# History is stored provider-neutrally as: {"role": "user"|"model", "text": str}
def _strip_json(text: str) -> str:
    """Tolerate models that wrap JSON in ```json fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1]
        if t.endswith("```"):
            t = t[: -3]
    return t.strip()


class LLMProvider(Protocol):
    async def stream_reply(
        self, system: str, history: list[dict], user_text: str
    ) -> AsyncIterator[str]: ...

    async def generate_report(self, prompt: str, schema: dict) -> dict: ...


# --- Gemini ------------------------------------------------------------------
class GeminiProvider:
    def __init__(self) -> None:
        from google import genai  # imported lazily so Claude-only setups don't need it

        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set")
        self._genai = genai
        self._client = genai.Client(api_key=config.GEMINI_API_KEY)
        self._model = config.GEMINI_MODEL

    def _contents(self, history: list[dict], user_text: str) -> list[dict]:
        contents = [
            {"role": h["role"], "parts": [{"text": h["text"]}]} for h in history
        ]
        contents.append({"role": "user", "parts": [{"text": user_text}]})
        return contents

    async def stream_reply(
        self, system: str, history: list[dict], user_text: str
    ) -> AsyncIterator[str]:
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.7,
            max_output_tokens=300,
            # 2.5 models "think" before answering, adding seconds of first-token
            # latency. A voice turn needs to start talking fast, so disable it.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        stream = await self._client.aio.models.generate_content_stream(
            model=self._model,
            contents=self._contents(history, user_text),
            config=cfg,
        )
        async for chunk in stream:
            if chunk.text:
                yield chunk.text

    async def generate_report(self, prompt: str, schema: dict) -> dict:
        from google.genai import types

        # Enforce the report shape server-side: response_schema makes Gemini
        # emit JSON matching REPORT_SCHEMA instead of relying on the prompt
        # alone. response_mime_type must accompany it.
        cfg = types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
            response_schema=schema,
        )
        resp = await self._client.aio.models.generate_content(
            model=self._model, contents=prompt, config=cfg
        )
        return json.loads(_strip_json(resp.text))


# --- Claude (optional swap) --------------------------------------------------
class ClaudeProvider:
    def __init__(self) -> None:
        import anthropic  # requires `pip install anthropic` + ANTHROPIC_API_KEY

        self._client = anthropic.AsyncAnthropic()
        self._model = config.CLAUDE_MODEL

    def _messages(self, history: list[dict], user_text: str) -> list[dict]:
        role_map = {"model": "assistant", "user": "user"}
        msgs = [{"role": role_map[h["role"]], "content": h["text"]} for h in history]
        msgs.append({"role": "user", "content": user_text})
        return msgs

    async def stream_reply(
        self, system: str, history: list[dict], user_text: str
    ) -> AsyncIterator[str]:
        async with self._client.messages.stream(
            model=self._model,
            system=system,
            max_tokens=300,
            temperature=0.7,
            messages=self._messages(history, user_text),
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def generate_report(self, prompt: str, schema: dict) -> dict:
        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt + "\n\nRespond with ONLY valid JSON."}],
        )
        return json.loads(_strip_json(msg.content[0].text))


_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    """Singleton provider chosen by config.LLM_PROVIDER."""
    global _provider
    if _provider is None:
        _provider = ClaudeProvider() if config.LLM_PROVIDER == "claude" else GeminiProvider()
    return _provider
