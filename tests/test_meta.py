"""Webhook signature and payload parsing. Pure: no network."""

from app.meta import parse_webhook, signature_ok

from tests.meta_payloads import APP_SECRET, envelope, signed, status_update, text_message


def test_valid_signature():
    body, headers = signed(text_message("923001234567", "wamid.1", "hi"))
    assert signature_ok(body, headers["X-Hub-Signature-256"], APP_SECRET)


def test_tampered_body_fails():
    body, headers = signed(text_message("923001234567", "wamid.1", "hi"))
    assert not signature_ok(body.replace(b"hi", b"yo"), headers["X-Hub-Signature-256"], APP_SECRET)


def test_wrong_secret_missing_header_and_unset_secret_fail():
    body, headers = signed(text_message("923001234567", "wamid.1", "hi"), secret="other")
    assert not signature_ok(body, headers["X-Hub-Signature-256"], APP_SECRET)
    assert not signature_ok(body, None, APP_SECRET)
    # An unconfigured secret must fail closed, never accept everything.
    assert not signature_ok(body, headers["X-Hub-Signature-256"], "")


def test_text_with_quote_and_profile_name():
    [msg], statuses = parse_webhook(
        text_message("923001234567", "wamid.2", "3 tak", name="Ahmed", ts=1759750000,
                     context_id="wamid.question")
    )
    assert statuses == []
    assert (msg.phone, msg.name, msg.text, msg.context_id) == (
        "923001234567", "Ahmed", "3 tak", "wamid.question")
    assert msg.at.timestamp() == 1759750000


def _one(message: dict):
    payload = envelope({"contacts": [{"wa_id": "923001234567", "profile": {"name": "A"}}],
                        "messages": [{"from": "923001234567", "id": "wamid.x",
                                      "timestamp": "1759750000", **message}]})
    [msg], _ = parse_webhook(payload)
    return msg


def test_voice_note_keeps_media_id():
    msg = _one({"type": "audio", "audio": {"id": "MEDIA1", "voice": True}})
    assert (msg.type, msg.text, msg.media_id) == ("audio", None, "MEDIA1")


def test_image_caption_location_buttons_lists():
    assert _one({"type": "image", "image": {"id": "IMG", "caption": "ye wala?"}}).text == "ye wala?"
    loc = _one({"type": "location", "location": {"latitude": 24.9, "longitude": 67.1,
                                                 "name": "Askari 5"}})
    assert loc.text.startswith("[location 24.9,67.1]") and "Askari 5" in loc.text
    btn = _one({"type": "interactive", "interactive": {
        "type": "button_reply", "button_reply": {"id": "b1", "title": "Visit book karein"}}})
    assert btn.text == "Visit book karein"
    row = _one({"type": "interactive", "interactive": {
        "type": "list_reply", "list_reply": {"id": "l1", "title": "4-5 Crore"}}})
    assert row.text == "4-5 Crore"


def test_statuses_and_errors():
    _, [st] = parse_webhook(status_update("wamid.out", "failed",
                                          {"code": 131047, "title": "Re-engagement message"}))
    assert (st.status, st.error) == ("failed", "131047: Re-engagement message")


def test_other_fields_are_ignored():
    payload = envelope({})
    payload["entry"][0]["changes"][0]["field"] = "account_update"
    assert parse_webhook(payload) == ([], [])
