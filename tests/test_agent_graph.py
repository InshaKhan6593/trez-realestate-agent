"""The whole agent turn (graph) against the real local listings, with a
scripted stand-in for the two models. Checks what the code does with model
output: tools, validation, retry, template fallback, handoff, memory.

Skipped when local Supabase with listings is not available.
"""

from __future__ import annotations

import asyncio
import os

import psycopg
import pytest

from agent.graph import agent_reply
from agent.respond import ReplyDraft
from agent.schemas import Extraction

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")
PHONE = "929933330001"
AGENT_PHONE = "929944440001"


def _loaded() -> bool:
    try:
        with psycopg.connect(DB_URL, connect_timeout=2) as conn:
            return conn.execute("SELECT count(*) FROM listings WHERE zameen_id = 54467403").fetchone()[0] == 1
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _loaded(), reason="local Supabase with listings not available")


class ScriptedLLM:
    """Returns queued answers per role and records every prompt it was given."""

    def __init__(self, extractor: list, responder: list):
        self.queue = {"extractor": list(extractor), "responder": list(responder)}
        self.prompts: dict[str, list] = {"extractor": [], "responder": []}

    async def structured(self, role, messages, schema, usage):
        self.prompts[role].append(messages)
        usage.calls.append({"role": role, "model": "scripted"})
        item = self.queue[role].pop(0)
        return item if isinstance(item, schema) else schema.model_validate(item)


def _cleanup(conn):
    lead = conn.execute("SELECT id FROM leads WHERE phone = %s", (PHONE,)).fetchone()
    if lead:
        for t in ("handoffs", "lead_slots", "lead_listings", "open_questions", "messages", "turns"):
            conn.execute(f"DELETE FROM {t} WHERE lead_id = %s", lead)
        conn.execute("DELETE FROM leads WHERE id = %s", lead)
    conn.execute("DELETE FROM agents WHERE phone = %s", (AGENT_PHONE,))


@pytest.fixture
def lead():
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        _cleanup(conn)
        conn.execute("INSERT INTO agents (name, phone) VALUES ('Fahad', %s)", (AGENT_PHONE,))
        lead_id = conn.execute("INSERT INTO leads (phone, name) VALUES (%s, 'Ahmed') RETURNING id",
                               (PHONE,)).fetchone()[0]
        yield lead_id
        _cleanup(conn)


def turn(lead_id: int, text: str, llm: ScriptedLLM):
    """Store one inbound message, open a turn, run the agent, commit memory."""
    async def go():
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            mid = (await (await conn.execute(
                """INSERT INTO messages (lead_id, direction, wa_message_id, type, text, at)
                   VALUES (%s, 'in', 'wamid.graph.' || gen_random_uuid(), 'text', %s, now())
                   RETURNING id""", (lead_id, text))).fetchone())[0]
            turn_id = (await (await conn.execute(
                "INSERT INTO turns (lead_id) VALUES (%s) RETURNING id", (lead_id,))).fetchone())[0]
            await conn.execute("UPDATE messages SET turn_id = %s WHERE id = %s", (turn_id, mid))
            reply = await agent_reply(conn, llm, lead_id, turn_id, [mid])
            await reply.commit(conn)
            return reply, turn_id
    return asyncio.run(go(), loop_factory=asyncio.SelectorEventLoop)


def q(sql, *args):
    with psycopg.connect(DB_URL) as conn:
        return conn.execute(sql, args).fetchall()


def listing_id(zameen_id):
    return q("SELECT id FROM listings WHERE zameen_id = %s", zameen_id)[0][0]


PLOT = 54467403
ASKS_ABOUT_PLOT = Extraction.model_validate({
    "language": "roman_urdu",
    "intents": [{"type": "listing_question", "listing": {"zameen_id": PLOT}, "topic": "installments",
                 "question": "installment plan kya hai?"},
                {"type": "photos", "listing": {"zameen_id": PLOT}}],
})


def test_link_question_gets_facts_media_and_memory(lead):
    lid = listing_id(PLOT)
    llm = ScriptedLLM([ASKS_ABOUT_PLOT], [ReplyDraft(
        reply="Ji, ye plot PKR 85 Lakh ka hai. Advance PKR 17 Lakh, phir 36 mahine PKR 60,000. Photos bhej raha hoon.",
        listing_ids_mentioned=[lid], says_available=[])])
    reply, turn_id = turn(lead, f"https://www.zameen.com/Property/x-{PLOT}-1345-1.html installments? photos?", llm)

    assert reply.media_listing_id == lid and reply.photos
    # The responder was shown the real facts, formatted.
    facts_json = llm.prompts["responder"][0][1]["content"]
    assert "PKR 85 Lakh" in facts_json and "PKR 60,000" in facts_json
    # Validation passed first time; the turn is fully audited.
    validation, extracted, tools = q("SELECT validation, extracted, tool_calls FROM turns WHERE id = %s", turn_id)[0]
    assert validation == {"attempts": 1, "problems": [], "used_template": False}
    assert extracted["intents"][0]["listing"]["zameen_id"] == PLOT
    assert {t["tool"] for t in tools} >= {"resolve_listing", "get_listing", "media_for_listing"}
    # Memory: they inquired about this listing at this price.
    assert q("SELECT relation, price_shown FROM lead_listings WHERE lead_id = %s", lead) == [("inquired", 8_500_000)]


def test_a_wrong_price_is_caught_and_rewritten(lead):
    lid = listing_id(PLOT)
    llm = ScriptedLLM([ASKS_ABOUT_PLOT], [
        ReplyDraft(reply="Ye plot sirf PKR 80 Lakh ka hai!", listing_ids_mentioned=[lid]),
        ReplyDraft(reply="Ye plot PKR 85 Lakh ka hai.", listing_ids_mentioned=[lid]),
    ])
    reply, turn_id = turn(lead, f"{PLOT} ki price?", llm)
    assert reply.text == "Ye plot PKR 85 Lakh ka hai."
    # The retry was told exactly what was wrong.
    assert "PKR 80 Lakh, which is not in the facts" in llm.prompts["responder"][1][-1]["content"]
    assert q("SELECT validation->>'attempts' FROM turns WHERE id = %s", turn_id) == [("2",)]


def test_wrong_twice_falls_back_to_a_template_from_facts(lead):
    lid = listing_id(PLOT)
    llm = ScriptedLLM([ASKS_ABOUT_PLOT], [
        ReplyDraft(reply="PKR 1 Crore only!", listing_ids_mentioned=[lid]),
        ReplyDraft(reply="PKR 2 Crore, available!", listing_ids_mentioned=[lid], says_available=[lid]),
    ])
    reply, turn_id = turn(lead, f"{PLOT}?", llm)
    assert "PKR 85 Lakh" in reply.text and "Crore" not in reply.text
    assert q("SELECT validation->>'used_template' FROM turns WHERE id = %s", turn_id) == [("true",)]


def test_negotiation_alerts_the_agent_with_the_question(lead):
    lid = listing_id(PLOT)
    ext = Extraction.model_validate({
        "language": "roman_urdu",
        "intents": [{"type": "negotiation", "listing": {"zameen_id": PLOT}, "question": "last kitna lagaoge?"}],
    })
    llm = ScriptedLLM([ext], [ReplyDraft(
        reply="Is plot ki price PKR 85 Lakh hai. Price ki baat hamare agent khud karenge, woh jald rabta karenge.",
        listing_ids_mentioned=[lid])])
    reply, _ = turn(lead, "last kitna lagaoge?", llm)
    assert reply.alert and reply.alert["phone"] == AGENT_PHONE
    assert "last kitna lagaoge?" in reply.alert["text"] and str(PLOT) in reply.alert["text"]
    assert q("SELECT handoff_state FROM leads WHERE id = %s", lead) == [("requested",)]


def test_a_question_the_data_cannot_answer_goes_to_the_agent(lead):
    lid = listing_id(PLOT)
    ext = Extraction.model_validate({
        "language": "roman_urdu",
        "intents": [{"type": "listing_question", "listing": {"zameen_id": PLOT}, "topic": "gas",
                     "question": "gas connection hai?"}],
    })
    llm = ScriptedLLM([ext], [ReplyDraft(
        reply="Listing mein gas ka zikr nahi hai; hamare agent confirm kar ke batayenge.",
        listing_ids_mentioned=[lid], unanswered=["gas connection hai?"])])
    reply, _ = turn(lead, "gas connection hai?", llm)
    assert reply.alert and "gas connection hai?" in reply.alert["text"]
    assert q("SELECT reason FROM handoffs WHERE lead_id = %s", lead) == [("not_in_data",)]


def test_search_with_nothing_in_the_sector_suggests_nearby_and_asks_one_thing(lead):
    ext = Extraction.model_validate({
        "language": "roman_urdu",
        "intents": [{"type": "search"}],
        "slot_updates": [
            {"slot": "purpose", "value": "sale", "source": "stated", "confidence": 1},
            {"slot": "property_types", "value": ["flat"], "source": "stated", "confidence": 1},
            {"slot": "location_id", "value": 12242, "source": "stated", "confidence": 1},
            {"slot": "budget_max", "value": 50000000, "source": "stated", "confidence": 1},
        ],
    })
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Sector G mein abhi flat nahi, lekin qareeb options hain. Kitne bedrooms chahiye?")])
    reply, turn_id = turn(lead, "Askari 5 sector G mein flat chahiye 5 crore tak", llm)
    facts_json = llm.prompts["responder"][0][1]["content"]
    assert '"nothing_in_asked_location": true' in facts_json and "Askari 5 - Sector" in facts_json
    plan = q("SELECT plan FROM turns WHERE id = %s", turn_id)[0][0]
    # For a flat, bedrooms come before timeline in the question order.
    assert plan["search"]["location_id"] == 12242 and plan["ask"] == "bedrooms_min"
    # What they want is remembered; the question asked is open.
    slots = dict(q("SELECT slot, value FROM lead_slots WHERE lead_id = %s", lead))
    assert slots["budget_max"] == 50000000 and slots["purpose"] == "sale"
    assert q("SELECT slot FROM open_questions WHERE lead_id = %s AND status = 'open'", lead) == [("bedrooms_min",)]


def test_an_agent_promise_without_a_handoff_is_rejected(lead):
    lid = listing_id(PLOT)
    llm = ScriptedLLM([ASKS_ABOUT_PLOT], [
        ReplyDraft(reply="Ye plot PKR 85 Lakh ka hai. Agent aap se rabta karega.",
                   listing_ids_mentioned=[lid], promises_agent_contact=True),
        ReplyDraft(reply="Ye plot PKR 85 Lakh ka hai.", listing_ids_mentioned=[lid]),
    ])
    reply, turn_id = turn(lead, f"{PLOT} price?", llm)
    assert reply.text == "Ye plot PKR 85 Lakh ka hai."
    assert "no handoff was made" in llm.prompts["responder"][1][-1]["content"]


def test_a_place_we_do_not_know_may_be_called_empty(lead):
    ext = Extraction.model_validate({
        "language": "roman_urdu", "intents": [{"type": "search"}],
        "slot_updates": [
            {"slot": "purpose", "value": "sale", "source": "stated", "confidence": 1},
            {"slot": "location_text", "value": "Bahria Town", "source": "stated", "confidence": 1},
            {"slot": "property_types", "value": ["house"], "source": "stated", "confidence": 1},
        ],
    })
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Bahria Town mein abhi hamari listings nahi hain.",
                                         claims_no_listings=True)])
    _, turn_id = turn(lead, "Bahria Town mein house?", llm)
    assert q("SELECT validation->>'attempts' FROM turns WHERE id = %s", turn_id) == [("1",)]
