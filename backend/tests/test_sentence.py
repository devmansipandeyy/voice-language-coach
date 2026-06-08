"""Sentence-boundary detection used to stream TTS one sentence at a time."""
from backend.session import _SENTENCE_END


def _first_sentence(text: str) -> str:
    m = _SENTENCE_END.search(text)
    return text[: m.end()].strip() if m else ""


def test_splits_on_period():
    assert _first_sentence("Hola amigo. Como estas?") == "Hola amigo."


def test_handles_question_mark():
    assert _first_sentence("Como estas? Bien.") == "Como estas?"


def test_handles_cjk_terminator():
    assert _first_sentence("你好。再见") == "你好。"


def test_no_boundary_returns_empty():
    assert _first_sentence("an unfinished clause") == ""
