"""Handoff with full context, and photos for WhatsApp.

Handoff tests use local Postgres with listings loaded; test rows are removed.
"""

from __future__ import annotations

import asyncio
import io
import os

import psycopg
import pytest
from PIL import Image
from psycopg.types.json import Jsonb

from agent import handoff
from agent.media import MAX_IMAGE_BYTES, for_whatsapp

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
TEST_PHONE = "929911110001"
AGENT_PHONES = ("929922220001", "929922220002")


# --- photos -------------------------------------------------------------------

def _image(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 100, 50)).save(buf, fmt)
    return buf.getvalue()


def test_webp_is_converted_to_jpeg_for_whatsapp():
    data, mime, name = for_whatsapp(_image("WEBP"), "303462770-800x1200.webp")
    assert mime == "image/jpeg" and name == "303462770-800x1200.jpg"
    assert Image.open(io.BytesIO(data)).format == "JPEG"


@pytest.mark.parametrize("fmt, mime", [("JPEG", "image/jpeg"), ("PNG", "image/png")])
def test_accepted_formats_pass_through_untouched(fmt, mime):
    original = _image(fmt)
    assert for_whatsapp(original, f"x.{fmt.lower()}") == (original, mime, f"x.{fmt.lower()}")


def test_format_is_read_from_the_file_not_the_name():
    # A WebP file named .jpg is still converted.
    data, mime, _ = for_whatsapp(_image("WEBP"), "misnamed.jpg")
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_a_photo_over_whatsapps_5_mb_limit_is_made_to_fit():
    # Random pixels do not compress: a 3000x3000 PNG of them is ~27 MB.
    big = io.BytesIO()
    Image.frombytes("RGB", (3000, 3000), os.urandom(3000 * 3000 * 3)).save(big, "PNG")
    assert big.tell() > MAX_IMAGE_BYTES
    data, mime, name = for_whatsapp(big.getvalue(), "huge.png")
    assert len(data) <= MAX_IMAGE_BYTES and mime == "image/jpeg" and name == "huge.jpg"
    assert Image.open(io.BytesIO(data)).format == "JPEG"


# --- the agent's alert on WhatsApp ----------------------------------------------

ALERT = ("🔥 HOT: Ahmed (+923001234567). Reason: ready to pay (also: negotiation).\n"
         "Wants: buy · house · Askari 6 · up to PKR 8.5 Cr\n- inquired: Brigadier House [52735413], PKR 8 Cr\n"
         "Reply #take to take over.")


def _captured(monkeypatch, **settings):
    """Run send_alert with these settings; -> the message it would post to Meta."""
    from app import sender
    from app.config import Settings
    sent = []

    async def fake_send(_settings, to, message):
        sent.append(message)
        return sender.SendResult("sent", wa_message_id="wamid.x")
    monkeypatch.setattr(sender, "_send", fake_send)
    base = dict(database_url="", redis_url="", whatsapp_app_secret="", whatsapp_verify_token="",
                whatsapp_access_token="t", whatsapp_phone_number_id="1", whatsapp_api_version="v23.0",
                whatsapp_alert_template="", whatsapp_alert_template_language="en", debounce_seconds=7,
                supabase_url="", supabase_service_role_key="")
    asyncio.run(sender.send_alert(Settings(**{**base, **settings}), "929922220001", ALERT))
    return sent[0]


def test_with_an_approved_template_the_alert_is_a_template_message(monkeypatch):
    # Outside the agent's 24-hour window Meta delivers only templates.
    msg = _captured(monkeypatch, whatsapp_alert_template="trez_lead_alert")
    assert msg["type"] == "template" and msg["template"]["name"] == "trez_lead_alert"
    assert msg["template"]["language"] == {"code": "en"}
    param = msg["template"]["components"][0]["parameters"][0]["text"]
    # Meta rejects newlines, tabs and 4+ spaces in a parameter: the alert is one line.
    assert "\n" not in param and "\t" not in param and "    " not in param
    assert param.startswith("🔥 HOT: Ahmed") and "ready to pay" in param and "52735413" in param


def test_without_a_template_the_alert_is_plain_text(monkeypatch):
    msg = _captured(monkeypatch)
    assert msg["type"] == "text" and msg["text"]["body"] == ALERT


def test_a_long_alert_is_cut_to_fit_a_template():
    from app.sender import TEMPLATE_PARAM_MAX, template_param
    param = template_param("line\n" * 400)
    assert len(param) == TEMPLATE_PARAM_MAX and param.endswith("…")


# --- handoff ------------------------------------------------------------------

def _loaded() -> bool:
    try:
        with psycopg.connect(DB_URL, connect_timeout=2) as conn:
            return conn.execute("SELECT count(*) FROM listings").fetchone()[0] > 0
    except Exception:
        return False


db = pytest.mark.skipif(not _loaded(), reason="local Supabase with listings not available")


def _cleanup(conn) -> None:
    lead = conn.execute("SELECT id FROM leads WHERE phone = %s", (TEST_PHONE,)).fetchone()
    if lead:
        for table in ("handoffs", "lead_slots", "lead_listings", "open_questions", "messages"):
            conn.execute(f"DELETE FROM {table} WHERE lead_id = %s", lead)
        conn.execute("DELETE FROM leads WHERE id = %s", lead)
    conn.execute("DELETE FROM agents WHERE phone = ANY(%s)", (list(AGENT_PHONES),))


@pytest.fixture
def lead():
    """A buyer who asked about 54467403, said budget 1 Cr, and was asked their timeline."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        _cleanup(conn)
        agents = [conn.execute("INSERT INTO agents (name, phone) VALUES (%s, %s) RETURNING id",
                               (name, phone)).fetchone()[0]
                  for name, phone in zip(("Fahad", "Sana"), AGENT_PHONES, strict=True)]
        lead_id = conn.execute(
            "INSERT INTO leads (phone, name, priority) VALUES (%s, 'Ahmed', 'hot') RETURNING id",
            (TEST_PHONE,)).fetchone()[0]
        conn.execute("""INSERT INTO lead_slots (lead_id, slot, value, confidence, source)
                        VALUES (%s, 'budget_max', %s, 1, 'stated')""", (lead_id, Jsonb(10_000_000)))
        listing = conn.execute("SELECT id, price_pkr FROM listings WHERE zameen_id = 54467403").fetchone()
        conn.execute("""INSERT INTO lead_listings (lead_id, listing_id, relation, status_shown, price_shown)
                        VALUES (%s, %s, 'inquired', 'available', %s)""", (lead_id, listing[0], 8_000_000))
        conn.execute("INSERT INTO open_questions (lead_id, slot) VALUES (%s, 'timeline')", (lead_id,))
        conn.execute("""INSERT INTO messages (lead_id, direction, wa_message_id, type, text, at)
                        VALUES (%s, 'in', 'wamid.handoff.test', 'text', 'last price kya hai?', now())""",
                     (lead_id,))
        yield lead_id, agents
        _cleanup(conn)


def run(fn, *args, **kwargs):
    async def go():
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            return await fn(conn, *args, **kwargs)
    return asyncio.run(go(), loop_factory=asyncio.SelectorEventLoop)


def _state(lead_id):
    with psycopg.connect(DB_URL) as conn:
        return conn.execute("SELECT handoff_state, agent_id FROM leads WHERE id = %s", (lead_id,)).fetchone()


@db
def test_request_alerts_an_agent_with_full_context(lead):
    lead_id, (fahad, _) = lead
    req = run(handoff.request_handoff, lead_id, "negotiation", ["Can the price come down?"])
    assert req.new and req.agent["id"] == fahad
    assert _state(lead_id) == ("requested", fahad)
    alert = req.alert
    assert "Ahmed" in alert and "negotiation" in alert
    assert "Wants: up to PKR 1 Cr" in alert                       # what the buyer said, in words
    assert "budget_max" not in alert                              # no internal names
    assert "54467403" in alert and "was PKR 80 lakh when shown" in alert   # price changed since shown
    assert "Can the price come down?" in alert                    # what the bot could not answer
    assert "no answer yet: timeline" in alert                     # still waiting to hear
    assert "last price kya hai?" in alert                         # the latest message


@db
def test_a_second_request_adds_to_the_open_handoff(lead):
    lead_id, _ = lead
    first = run(handoff.request_handoff, lead_id, "negotiation", ["Price?"])
    second = run(handoff.request_handoff, lead_id, "not_in_data", ["Transfer fee?"])
    assert not second.new and second.handoff_id == first.handoff_id
    assert "Price?" in second.alert and "Transfer fee?" in second.alert


@db
def test_take_then_release(lead):
    lead_id, (fahad, _) = lead
    run(handoff.request_handoff, lead_id, "asked_for_human")
    run(handoff.take, lead_id, fahad)
    assert _state(lead_id) == ("taken", fahad)
    run(handoff.release, lead_id)
    assert _state(lead_id)[0] == "none"


@db
def test_agent_to_agent_keeps_the_chain_and_open_items(lead):
    lead_id, (fahad, sana) = lead
    first = run(handoff.request_handoff, lead_id, "negotiation", ["Price?"])
    run(handoff.take, lead_id, fahad)
    moved = run(handoff.reassign, lead_id, sana, "He wants a call after 6 pm")
    assert moved.agent["id"] == sana and _state(lead_id) == ("requested", sana)
    assert "Price?" in moved.alert and "after 6 pm" in moved.alert
    with psycopg.connect(DB_URL) as conn:
        rows = conn.execute("SELECT id, replaces_id, released_at IS NOT NULL FROM handoffs "
                            "WHERE lead_id = %s ORDER BY id", (lead_id,)).fetchall()
    assert rows == [(first.handoff_id, None, True), (moved.handoff_id, first.handoff_id, False)]


@db
def test_no_agent_set_up_is_reported_not_hidden(lead):
    lead_id, _ = lead
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("UPDATE agents SET active = false WHERE phone = ANY(%s)", (list(AGENT_PHONES),))
        others = conn.execute("SELECT count(*) FROM agents WHERE active").fetchone()[0]
    req = run(handoff.request_handoff, lead_id, "asked_for_human")
    if others == 0:
        assert req.agent is None
