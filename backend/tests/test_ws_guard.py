"""/ws anti-abuse helpers (main.py): per-IP rate limit and client-IP resolution."""
from backend import main


class _FakeClient:
    host = "9.9.9.9"


class _FakeWS:
    def __init__(self, xff=None):
        self.headers = {"x-forwarded-for": xff} if xff else {}
        self.client = _FakeClient()


def test_rate_limit_trips_after_threshold(monkeypatch):
    main._ip_hits.clear()
    monkeypatch.setattr(main.config, "WS_RATE_PER_MIN_IP", 2)
    assert main._rate_limited("1.2.3.4") is False  # 1st
    assert main._rate_limited("1.2.3.4") is False  # 2nd
    assert main._rate_limited("1.2.3.4") is True   # 3rd exceeds 2/min


def test_rate_limit_is_per_ip(monkeypatch):
    main._ip_hits.clear()
    monkeypatch.setattr(main.config, "WS_RATE_PER_MIN_IP", 1)
    assert main._rate_limited("1.1.1.1") is False
    assert main._rate_limited("2.2.2.2") is False  # different IP, own budget


def test_client_ip_prefers_forwarded_for():
    assert main._client_ip(_FakeWS("5.5.5.5, 6.6.6.6")) == "5.5.5.5"


def test_client_ip_falls_back_to_peer():
    assert main._client_ip(_FakeWS()) == "9.9.9.9"
