"""Error reporting: off without a DSN; phone numbers never leave in an event."""

from app.errors import _masked, init_sentry


def test_sentry_is_off_without_a_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry("worker") is False


def test_phone_numbers_are_masked_in_error_events():
    event = {
        "exception": {"values": [{"type": "LLMError", "value": "turn failed for +923001234567"}]},
        "logentry": {"message": "reply failed for lead 03331234567"},
        "extra": {"to": "923001234567", "price": 85_000_000},
    }
    out = _masked(event, {})
    assert out["exception"]["values"][0]["value"] == "turn failed for +92300•••4567"
    assert out["logentry"]["message"] == "reply failed for lead 0333•••4567"
    assert out["extra"] == {"to": "92300•••4567", "price": 85_000_000}
