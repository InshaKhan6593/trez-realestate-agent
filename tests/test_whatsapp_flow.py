"""Webhook -> store -> debounce -> turn, against local Postgres and Redis.

Skipped when `npx supabase start` / `docker compose up -d` are not running.
Uses Redis DB 15 and test-only phone numbers, removed afterwards.
"""

from __future__ import annotations

import asyncio
import os
import random

import psycopg
import pytest
import redis as redis_sync

from tests.meta_payloads import APP_SECRET, signed, status_update, text_message

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
REDIS_URL = "redis://127.0.0.1:6379/15"
VERIFY_TOKEN = "test-verify-token"
# No real Pakistani number starts with 9299. This test's own block of it, so its
# cleanup never deletes scripts/chat.py's test buyers (92995555...).
TEST_PREFIX = "929977"


def _reachable() -> bool:
    try:
        psycopg.connect(DB_URL, connect_timeout=2).close()
        redis_sync.Redis.from_url(REDIS_URL, socket_connect_timeout=2).ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="local Supabase/Redis not running")


def _cleanup() -> None:
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM leads WHERE phone LIKE %s", (TEST_PREFIX + "%",)).fetchall()]
        if ids:
            # Everything that hangs off a lead, children before parents.
            for table in ("handoffs", "lead_slots", "lead_listings", "open_questions", "episodes",
                          "messages", "turns"):
                conn.execute(f"DELETE FROM {table} WHERE lead_id = ANY(%s)", (ids,))
            conn.execute("DELETE FROM leads WHERE id = ANY(%s)", (ids,))
    redis_sync.Redis.from_url(REDIS_URL).flushdb()


@pytest.fixture(scope="module")
def client():
    os.environ.update({
        "DATABASE_URL": DB_URL, "REDIS_URL": REDIS_URL,
        "WHATSAPP_APP_SECRET": APP_SECRET, "WHATSAPP_VERIFY_TOKEN": VERIFY_TOKEN,
        "WHATSAPP_ACCESS_TOKEN": "", "DEBOUNCE_SECONDS": "7",
    })
    from app.config import get_settings
    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from app.webhook import app

    _cleanup()
    # psycopg async needs a selector loop (Windows defaults to Proactor).
    with TestClient(app, backend_options={"loop_factory": asyncio.SelectorEventLoop}) as c:
        yield c
    _cleanup()


def _phone() -> str:
    return TEST_PREFIX + "".join(random.choices("0123456789", k=6))


def _post(client, payload: dict):
    body, headers = signed(payload)
    return client.post("/webhook", content=body, headers=headers)


def _q(sql: str, *args):
    with psycopg.connect(DB_URL) as conn:
        return conn.execute(sql, args).fetchall()


def _lead(phone: str) -> int:
    return _q("SELECT id FROM leads WHERE phone = %s", phone)[0][0]


def _seq(lead_id: int) -> int:
    return int(redis_sync.Redis.from_url(REDIS_URL).get(f"inbox:{lead_id}:seq") or 0)


class _Reply:
    def __init__(self, text):
        self.text, self.media_listing_id, self.alert = text, None, None

    async def commit(self, conn, delivered=True):
        pass


def counting_reply(messages: list[dict]) -> str:
    n = len(messages)
    return f"received {n} message{'s' if n > 1 else ''}"


def _turn(lead_id: int, seq: int, reply_fn=None) -> str:
    """Run one turn the way the arq worker would, with the real reply/sender."""
    from psycopg_pool import AsyncConnectionPool
    from redis.asyncio import Redis

    from app.config import get_settings
    from app.sender import send_text
    from app.turn import run_turn

    text_fn = reply_fn or counting_reply

    async def fake_agent(conn, lead_id, turn_id, messages):
        # The plumbing is tested without a model; agent.graph has its own tests.
        return _Reply(text_fn(messages))

    async def go():
        r = Redis.from_url(REDIS_URL)
        async with AsyncConnectionPool(DB_URL, kwargs={"autocommit": True}, open=False) as pool:
            out = await run_turn(r, pool, get_settings(), lead_id, seq,
                                 reply_fn=fake_agent, send_fn=send_text)
        await r.aclose()
        return out

    return asyncio.run(go(), loop_factory=asyncio.SelectorEventLoop)


# --------------------------------------------------------------------------

def test_verification_handshake(client):
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                                        "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                         "hub.challenge": "12345"})
    assert bad.status_code == 403


def test_bad_signature_is_rejected_and_nothing_stored(client):
    phone = _phone()
    body, headers = signed(text_message(phone, f"wamid.{phone}.1", "hi"), secret="attacker")
    assert client.post("/webhook", content=body, headers=headers).status_code == 401
    assert _q("SELECT 1 FROM leads WHERE phone = %s", phone) == []


def test_burst_of_three_becomes_one_turn_and_one_reply(client):
    phone = _phone()
    for i, text in enumerate(["hi", "available hai?", "https://www.zameen.com/Property/x-54550053-9582-1.html"]):
        assert _post(client, text_message(phone, f"wamid.{phone}.{i}", text)).status_code == 200
    lead = _lead(phone)
    assert _seq(lead) == 3

    # The first two jobs wake up after the debounce, see newer messages, and step aside.
    assert _turn(lead, 1) == "stale"
    assert _turn(lead, 2) == "stale"
    assert _turn(lead, 3) == "not_sent"           # dry run: no WhatsApp token in tests

    [(turn_id, status, reply, latency)] = _q(
        "SELECT id, status, reply, latency_ms FROM turns WHERE lead_id = %s", lead)
    assert status == "not_sent" and "3 messages" in reply and latency >= 0
    assert _q("SELECT count(*) FROM messages WHERE lead_id = %s AND direction = 'in' "
              "AND turn_id = %s", lead, turn_id) == [(3,)]
    assert _q("SELECT delivery_status FROM messages WHERE lead_id = %s AND direction = 'out'",
              lead) == [("not_sent",)]


def test_duplicate_delivery_is_stored_once(client):
    phone = _phone()
    payload = text_message(phone, f"wamid.{phone}.dup", "salam")
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200       # Meta retry
    lead = _lead(phone)
    assert _q("SELECT count(*) FROM messages WHERE lead_id = %s", lead) == [(1,)]

    # Once answered, a late retry neither stores nor queues anything.
    assert _turn(lead, _seq(lead)) == "not_sent"
    before = _seq(lead)
    assert _post(client, payload).status_code == 200
    assert _seq(lead) == before
    assert _q("SELECT count(*) FROM messages WHERE lead_id = %s AND direction = 'in'", lead) == [(1,)]


def test_takeover_means_no_bot_reply(client):
    phone = _phone()
    _post(client, text_message(phone, f"wamid.{phone}.1", "price kam karo"))
    lead = _lead(phone)
    _q("UPDATE leads SET handoff_state = 'taken' WHERE id = %s RETURNING id", lead)
    assert _turn(lead, _seq(lead)) == "takeover"
    assert _q("SELECT count(*) FROM messages WHERE lead_id = %s AND direction = 'out'", lead) == [(0,)]


def test_message_arriving_mid_turn_supersedes_the_draft(client):
    phone = _phone()
    _post(client, text_message(phone, f"wamid.{phone}.1", "DHA wala?"))
    lead = _lead(phone)
    seq = _seq(lead)

    def slow_reply(messages):
        # While the draft is being written, the buyer sends another message.
        _post(client, text_message(phone, f"wamid.{phone}.2", "aur installments?"))
        return "draft"

    assert _turn(lead, seq, reply_fn=slow_reply) == "superseded"
    # The draft is discarded and its message handed back...
    assert _q("SELECT count(*) FROM messages WHERE lead_id = %s AND turn_id IS NULL", lead) == [(2,)]
    # ...so the newer job answers both in one reply.
    assert _turn(lead, _seq(lead)) == "not_sent"
    assert "2 messages" in _q("SELECT reply FROM turns WHERE lead_id = %s AND status = 'not_sent'",
                              lead)[0][0]


def test_locked_lead_waits(client):
    phone = _phone()
    _post(client, text_message(phone, f"wamid.{phone}.1", "hello"))
    lead = _lead(phone)
    r = redis_sync.Redis.from_url(REDIS_URL)
    r.set(f"inbox:{lead}:lock", "another-worker", px=5000)
    assert _turn(lead, _seq(lead)) == "locked"
    r.delete(f"inbox:{lead}:lock")
    assert _turn(lead, _seq(lead)) == "not_sent"


def test_delivery_statuses_only_move_forward(client):
    phone = _phone()
    _post(client, text_message(phone, f"wamid.{phone}.1", "hi"))
    lead = _lead(phone)
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("""INSERT INTO messages (lead_id, direction, wa_message_id, type, text,
                                              delivery_status, at)
                        VALUES (%s, 'out', %s, 'text', 'x', 'sent', now())""",
                     (lead, f"wamid.{phone}.out"))
    out = f"wamid.{phone}.out"

    def status():
        return _q("SELECT delivery_status, error FROM messages WHERE wa_message_id = %s", out)[0]

    _post(client, status_update(out, "read"))
    _post(client, status_update(out, "delivered"))          # arrives late: must not downgrade
    assert status() == ("read", None)
    _post(client, status_update(out, "failed", {"code": 131047, "title": "Re-engagement message"}))
    assert status() == ("failed", "131047: Re-engagement message")


def test_agent_taking_over_mid_turn_stops_the_bot_reply(client):
    phone = _phone()
    _post(client, text_message(phone, f"wamid.{phone}.1", "price kam ho sakti hai?"))
    lead = _lead(phone)

    def slow_reply(messages):
        # While the draft is written, the agent takes the chat.
        _q("UPDATE leads SET handoff_state = 'taken' WHERE id = %s RETURNING id", lead)
        return "draft"

    assert _turn(lead, _seq(lead), reply_fn=slow_reply) == "takeover"
    assert _q("SELECT count(*) FROM messages WHERE lead_id = %s AND direction = 'out'", lead) == [(0,)]
    assert _q("SELECT status, reply FROM turns WHERE lead_id = %s", lead) == [("takeover", "draft")]
