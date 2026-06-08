"""Report JSON fence-stripping (llm.py). Models sometimes wrap JSON in ```json
fences; the report path must tolerate that before json.loads."""
from backend.llm import _strip_json


def test_plain_json_unchanged():
    assert _strip_json('{"a": 1}') == '{"a": 1}'


def test_strips_json_fence():
    assert _strip_json('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strips_bare_fence():
    assert _strip_json('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strips_surrounding_whitespace():
    assert _strip_json('   {"a": 1}   ') == '{"a": 1}'
