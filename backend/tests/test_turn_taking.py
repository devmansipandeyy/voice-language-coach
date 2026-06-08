"""Server-authoritative turn-taking in CoachSession._on_final.

The bug this guards against: while the coach is mid-reply, its own voice leaking
back through the mic was transcribed as a 'final' and cancelled + restarted the
turn, so the agent never finished ("just hello"). Now an echo-like final is
ignored; a genuinely different utterance is treated as a real barge-in."""
import pytest

from backend import session as S


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    async def send_bytes(self, b):
        pass


@pytest.fixture
async def sess(monkeypatch):
    # Avoid constructing real providers (no API keys needed for these tests).
    monkeypatch.setattr(S, "get_provider", lambda: object())
    monkeypatch.setattr(S.config, "use_cartesia", lambda: False)
    return S.CoachSession(FakeWS())


@pytest.mark.asyncio
async def test_ignores_echo_final_while_speaking(sess):
    sess._speaking = True
    sess._current_tts = "como estas hoy amigo"
    await sess._on_final("como estas hoy")  # high overlap → the agent's own echo
    assert sess._agent_task is None
    assert not any(m.get("type") == "transcript" for m in sess._ws.sent)


@pytest.mark.asyncio
async def test_real_barge_in_replaces_reply(sess, monkeypatch):
    started = []

    async def fake_turn(utterance, greeting=False):
        started.append(utterance)

    monkeypatch.setattr(sess, "_agent_turn", fake_turn)
    sess._speaking = True
    sess._current_tts = "como estas hoy amigo"
    await sess._on_final("wait stop please now")  # low overlap → real barge-in
    assert sess._agent_task is not None
    await sess._agent_task
    assert started == ["wait stop please now"]


@pytest.mark.asyncio
async def test_normal_final_when_idle_starts_turn(sess, monkeypatch):
    async def fake_turn(utterance, greeting=False):
        pass

    monkeypatch.setattr(sess, "_agent_turn", fake_turn)
    sess._speaking = False
    await sess._on_final("hola buenos dias")
    assert any(m.get("type") == "transcript" and m.get("final") for m in sess._ws.sent)
    await sess._agent_task
