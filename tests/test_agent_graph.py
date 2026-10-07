"""The whole agent turn (graph) against the real local listings, with a
scripted stand-in for the two models. Checks what the code does with model
output: tools, validation, retry, template fallback, handoff, memory.

Skipped when local Supabase with listings is not available.
"""

from __future__ import annotations

import asyncio
import json
import os

import psycopg
import pytest

from agent.graph import agent_reply
from agent.locations import load_tree, place_choices, places_named
from agent.money import pkr
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
        if isinstance(item, Exception):
            raise item
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
            {"slot": "purpose", "value": "sale", "said": "khareedna", "source": "stated", "confidence": 1},
            {"slot": "property_types", "value": ["flat"], "said": "flat", "source": "stated", "confidence": 1},
            {"slot": "location_id", "value": 12242, "said": "sector G", "source": "stated", "confidence": 1},
            {"slot": "budget_max", "value": 50000000, "said": "5 crore tak", "source": "stated", "confidence": 1},
        ],
    })
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Sector G mein abhi flat nahi, lekin qareeb options hain. Kitne bedrooms chahiye?")])
    reply, turn_id = turn(lead, "Askari 5 sector G mein flat khareedna hai 5 crore tak", llm)
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


def _three_available():
    return q("SELECT id, zameen_id, price_pkr FROM listings WHERE status = 'available' ORDER BY id LIMIT 3")


SEARCH = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}], "slot_updates": [
    {"slot": "purpose", "value": "sale", "said": "khareedna", "source": "stated", "confidence": 1}]})


def test_the_first_one_means_the_first_in_our_last_reply(lead):
    # Live run, turn 4: "pehle wale" could not be matched, because the listings
    # were handed to the extractor by time, and all three were shown at once.
    rows = _three_available()
    order = [rows[2][0], rows[0][0], rows[1][0]]           # the reply's order, not the database's
    first_zameen = rows[2][1]
    asks_first = Extraction.model_validate({"language": "roman_urdu", "intents": [
        {"type": "listing_question", "listing": {"zameen_id": first_zameen}}]})
    llm = ScriptedLLM([SEARCH, asks_first], [
        ReplyDraft(reply="Ye teen options hain: ...", listing_ids_mentioned=order),
        ReplyDraft(reply="Ji, is ki tafseel ye hai.", listing_ids_mentioned=[rows[2][0]]),
    ])
    turn(lead, "khareedna hai", llm)
    turn(lead, "pehle wale ka size?", llm)
    payload = json.loads(llm.prompts["extractor"][1][1]["content"])
    assert [i["zameen_id"] for i in payload["OUR_LAST_LIST"]] == [rows[2][1], rows[0][1], rows[1][1]]
    assert payload["OUR_LAST_LIST"][0]["n"] == 1


def test_an_unclear_listing_is_asked_about_not_guessed(lead):
    rows = _three_available()
    llm = ScriptedLLM([SEARCH, Extraction.model_validate({"language": "roman_urdu", "intents": [
        {"type": "listing_question", "listing": {"from_history": "wo wala"}}]})], [
        ReplyDraft(reply="Ye options hain.", listing_ids_mentioned=[r[0] for r in rows]),
        # Quotes the candidates' real prices to ask which one: allowed.
        ReplyDraft(reply=f"{pkr(rows[0][2])} wala ya {pkr(rows[1][2])} wala?",
                   listing_ids_mentioned=[rows[0][0], rows[1][0]]),
    ])
    turn(lead, "khareedna hai", llm)
    with psycopg.connect(DB_URL, autocommit=True) as conn:      # listings this buyer has seen
        for r in rows:
            conn.execute("""INSERT INTO lead_listings (lead_id, listing_id, relation, status_shown, price_shown)
                            VALUES (%s, %s, 'shown', 'available', %s) ON CONFLICT DO NOTHING""", (lead, r[0], r[2]))
    reply, turn_id = turn(lead, "wo wala kitne ka hai?", llm)
    assert "ya" in reply.text
    plan, validation = q("SELECT plan, validation FROM turns WHERE id = %s", turn_id)[0]
    assert plan["ask"] == "which_listing" and validation["attempts"] == 1
    # "which one?" is not a fact about the buyer: no open question is stored for it.
    assert ("which_listing",) not in q("SELECT slot FROM open_questions WHERE lead_id = %s", lead)


def test_with_nothing_safe_to_say_the_agent_gets_the_message(lead):
    # Live run, turn 4: the template had no facts and sent an empty message.
    ext = Extraction.model_validate({"language": "roman_urdu", "intents": [
        {"type": "listing_question", "listing": {"from_history": "jo kal dekha tha"}}]})
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Wo PKR 3 Crore ka hai."),
                              ReplyDraft(reply="Wo PKR 3.2 Crore ka hai.")])
    reply, turn_id = turn(lead, "jo kal dekha tha wo kitne ka hai?", llm)
    assert reply.text == "Hamare agent jald aap se rabta karenge."
    assert reply.alert and "jo kal dekha tha wo kitne ka hai?" in reply.alert["text"]
    assert q("SELECT reason FROM handoffs WHERE lead_id = %s", lead) == [("not_in_data",)]


def test_a_token_offer_on_the_listing_being_discussed(lead):
    # Live run, turn 7: the listing was not loaded, so its true price was
    # rejected twice; the lead stayed warm; the reason read "asked for human".
    lid = listing_id(PLOT)
    offer = Extraction.model_validate({"language": "roman_urdu", "intents": [
        {"type": "negotiation", "question": "80 lakh final karein?"}, {"type": "ask_human"}],
        "signals": ["token_or_bayana"]})
    llm = ScriptedLLM([ASKS_ABOUT_PLOT, offer], [
        ReplyDraft(reply="Ye plot PKR 85 Lakh ka hai.", listing_ids_mentioned=[lid]),
        ReplyDraft(reply="Listed price PKR 85 Lakh hai; aap ka PKR 80 Lakh ka offer agent tak pohanch jayega, "
                         "woh jald rabta karenge.", listing_ids_mentioned=[lid], promises_agent_contact=True),
    ])
    turn(lead, f"{PLOT} installments? photos?", llm)
    reply, turn_id = turn(lead, "80 lakh final karein to aaj token de dun", llm)
    tools, validation = q("SELECT tool_calls, validation FROM turns WHERE id = %s", turn_id)[0]
    assert "get_listing" in [t["tool"] for t in tools] and validation["attempts"] == 1
    assert q("SELECT priority FROM leads WHERE id = %s", lead) == [("hot",)]
    assert reply.alert["text"].startswith("🔥 HOT") and "Reason: ready to pay (also: negotiation" in reply.alert["text"]
    assert "ready to pay a token" in reply.alert["text"]


def test_a_link_is_found_in_code_even_when_the_model_misses_it(lead):
    # Live run: the extractor returned zameen_id null for a message with a Zameen link,
    # and the bot said "I couldn't find the listing you shared".
    lid = listing_id(PLOT)
    missed = Extraction.model_validate({"language": "roman_urdu", "intents": [
        {"type": "availability", "listing": {"from_history": "the listing in the link"}}]})
    llm = ScriptedLLM([missed], [ReplyDraft(reply="Ji, ye plot PKR 85 Lakh ka hai aur available hai.",
                                            listing_ids_mentioned=[lid], says_available=[lid])])
    reply, turn_id = turn(lead, f"https://www.zameen.com/Property/x-{PLOT}-1345-1.html ye available hai? "
                                "call 03001234567", llm)
    tools, validation = q("SELECT tool_calls, validation FROM turns WHERE id = %s", turn_id)[0]
    resolved = [t["result"] for t in tools if t["tool"] == "resolve_listing"]
    assert resolved == [{"match": lid, "how": "zameen_id", "candidates": [PLOT]}]   # the phone number did not count
    assert validation["attempts"] == 1


def _ambiguous_place_name():
    """A word that fits several of today's places (e.g. "Emaar" for Emaar Panorama
    and Emaar The Views), found from the live places, so the test follows the
    stock instead of assuming it. -> (word, {place_id: name}) or None."""
    async def go():
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            tree, offered = await load_tree(conn), {c["id"] for c in await place_choices(conn)}
            for word in sorted({tree.places[i].name.split()[0] for i in offered}):
                named = places_named(tree, word, offered)
                if len(named) > 1:
                    return word, {i: tree.places[i].name for i in named}
        return None
    return asyncio.run(go(), loop_factory=asyncio.SelectorEventLoop)


def _place_search(word, **slots):
    return Extraction.model_validate({"language": "english", "intents": [{"type": "search"}],
                                      "slot_updates": [{"slot": "purpose", "value": "sale", "said": "khareedna",
                                                        "source": "stated", "confidence": 1}] + [
                                          {"slot": k, "value": v, "said": word, "source": "stated", "confidence": 1}
                                          for k, v in slots.items()]})


def test_words_naming_one_place_overrule_a_different_pick(lead):
    # Live run: "askari mein ghar" was picked as DHA Defence. If the buyer's words name
    # exactly one of our places, a pick outside it is replaced by that place.
    rows = q("""SELECT l.location_id, p.name FROM listings l JOIN locations p ON p.id = l.location_id
                WHERE l.status = 'available' AND l.purpose = 'sale' GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 2""")
    (right, name), (wrong, _) = rows
    llm = ScriptedLLM([_place_search(name, location_id=wrong)], [ReplyDraft(reply="Ye options hain.")])
    _, turn_id = turn(lead, f"{name} mein khareedna hai", llm)
    plan, tools = q("SELECT plan, tool_calls FROM turns WHERE id = %s", turn_id)[0]
    assert plan["search"]["location_id"] == right
    assert tools[0]["tool"] == "check_place" and tools[0]["result"]["verdict"] == "use_named"


@pytest.mark.parametrize("model_reads", ["as text", "as one place's id"])
def test_a_name_that_fits_several_places_is_asked_not_searched_everywhere(lead, model_reads):
    # Live runs: "Emaar" fits Emaar Panorama and Emaar The Views. Read as text, the search
    # ran across all of Karachi; read as an id, the model had quietly picked one tower.
    found = _ambiguous_place_name()
    if found is None:
        pytest.skip("no place name in today's stock fits several places")
    word, options = found
    slot = {"location_text": word} if model_reads == "as text" else {"location_id": next(iter(options))}
    llm = ScriptedLLM([_place_search(word, **slot)], [ReplyDraft(reply="Which one do you mean?")])
    reply, turn_id = turn(lead, f"{word} mein khareedna hai", llm)
    plan, tools = q("SELECT plan, tool_calls FROM turns WHERE id = %s", turn_id)[0]
    assert plan["ask"] == "which_place" and plan["search"] is None
    assert [t["tool"] for t in tools] in (["find_location"], ["check_place"])
    facts = json.loads(llm.prompts["responder"][0][1]["content"])["FACTS"]
    assert {o["place_id"] for o in facts["place"]["options"]} == set(options)
    assert "location_id" not in dict(q("SELECT slot, value FROM lead_slots WHERE lead_id = %s", lead))
    # The answer ("Emaar Panorama") fills the place: that is the open question next turn
    # (live run: without it, the answer was read as a listing and went unanswered).
    assert q("SELECT slot FROM open_questions WHERE lead_id = %s AND status = 'open'", lead) == [("location_id",)]


def _one_way_only():
    """A property type today's stock has only for sale (or only for rent). -> (type, purpose) or None."""
    rows = q("""SELECT property_type, array_agg(DISTINCT purpose) FROM listings WHERE status = 'available'
                GROUP BY property_type HAVING count(DISTINCT purpose) = 1""")
    return (rows[0][0], rows[0][1][0]) if rows else None


def test_buy_or_rent_unknown_but_only_one_exists_shows_the_listings(lead):
    # Live run: "120 gaz plot, 1 crore tak": counts only, and the reply guessed "no listing".
    found = _one_way_only()
    if found is None:
        pytest.skip("every property type in today's stock is listed both for sale and for rent")
    kind, purpose = found
    ext = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}], "slot_updates": [
        {"slot": "property_types", "value": [kind], "said": kind, "source": "stated", "confidence": 1}]})
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Ye options hain. Buy karna hai ya rent?")])
    turn(lead, f"{kind} chahiye", llm)
    facts = json.loads(llm.prompts["responder"][0][1]["content"])["FACTS"]
    assert facts["search"]["buyer_has_not_said_buy_or_rent"] and facts["search"]["results"]
    assert {r["purpose"] for r in facts["search"]["results"]} == {purpose}
    assert facts["stock_preview"]["counted"] == {"property_types": [kind]}


def test_asked_again_without_saying_buy_or_rent_shows_both_ways(lead):
    both = q("""SELECT property_type FROM listings WHERE status = 'available'
                GROUP BY property_type HAVING count(DISTINCT purpose) = 2 LIMIT 1""")
    if not both:
        pytest.skip("no property type is listed both for sale and for rent today")
    kind = both[0][0]
    ext = lambda intent: Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": intent}],  # noqa: E731
                                                    "slot_updates": [{"slot": "property_types", "value": [kind], "said": kind,
                                                                      "source": "stated", "confidence": 1}]})
    llm = ScriptedLLM([ext("search"), ext("more_options")],
                      [ReplyDraft(reply="Buy karna hai ya rent?"), ReplyDraft(reply="Ye options hain.")])
    turn(lead, f"{kind} chahiye", llm)                          # 1st: counts, asks buy or rent
    first = json.loads(llm.prompts["responder"][0][1]["content"])["FACTS"]
    assert first["search"] is None
    turn(lead, "jo hai woh dikha dein", llm)                    # asked again: both ways, labelled
    facts = json.loads(llm.prompts["responder"][1][1]["content"])["FACTS"]
    assert {r["purpose"] for r in facts["search"]["results"]} == {"sale", "rent"}


def test_a_voice_note_goes_to_the_agent_and_the_model_knows_it_cannot_hear_it(lead):
    async def go():
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            mid = (await (await conn.execute(
                """INSERT INTO messages (lead_id, direction, wa_message_id, type, text, at)
                   VALUES (%s, 'in', 'wamid.graph.' || gen_random_uuid(), 'audio', NULL, now()) RETURNING id""",
                (lead,))).fetchone())[0]
            turn_id = (await (await conn.execute("INSERT INTO turns (lead_id) VALUES (%s) RETURNING id",
                                                 (lead,))).fetchone())[0]
            await conn.execute("UPDATE messages SET turn_id = %s WHERE id = %s", (turn_id, mid))
            reply = await agent_reply(conn, llm, lead, turn_id, [mid])
            await reply.commit(conn)
            return reply
    llm = ScriptedLLM([Extraction.model_validate({"language": "mixed", "intents": [{"type": "other"}]})],
                      [ReplyDraft(reply="Main abhi voice note nahi sun sakta, likh kar bata dein; agent sunega.",
                                  promises_agent_contact=True)])
    reply = asyncio.run(go(), loop_factory=asyncio.SelectorEventLoop)
    sent = json.loads(llm.prompts["responder"][0][1]["content"])["BUYER_MESSAGES_NOW"]
    assert sent == [{"kind": "voice_note", "words": None, "we_can_see_or_hear_its_content": False}]
    assert reply.alert and "voice note the bot cannot read" in reply.alert["text"]


def test_urdu_script_is_answered_in_urdu_script(lead):
    # Live run: an Urdu-script message was labelled roman_urdu, and answered in Roman Urdu.
    ext = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "greeting"}]})
    llm = ScriptedLLM([ext], [ReplyDraft(reply="وعلیکم السلام")])
    turn(lead, "السلام علیکم", llm)
    assert json.loads(llm.prompts["responder"][0][1]["content"])["REPLY_IN"] == "Urdu script"
    assert q("SELECT language FROM leads WHERE id = %s", lead) == [("urdu",)]


def test_urdu_in_english_letters_is_not_answered_in_urdu_script(lead):
    # Live run: "Assalam o alaikum" was labelled urdu and answered in Urdu script.
    ext = Extraction.model_validate({"language": "urdu", "intents": [{"type": "greeting"}]})
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Walaikum assalam")])
    turn(lead, "Assalam o alaikum", llm)
    assert json.loads(llm.prompts["responder"][0][1]["content"])["REPLY_IN"].startswith("Roman Urdu")


def test_a_place_the_buyer_named_is_used_when_the_model_leaves_it_out(lead):
    # Live run: "Askari 6 villa rate?" -> the model recorded no place in half the runs.
    rows = q("""SELECT p.id, p.name FROM locations p JOIN listings l ON l.location_id = p.id
                WHERE l.status = 'available' GROUP BY p.id, p.name ORDER BY count(*) DESC LIMIT 1""")
    place_id, name = rows[0]
    ext = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}]})   # no place
    llm = ScriptedLLM([ext, ext], [ReplyDraft(reply="Buy karna hai ya rent?")])   # 2nd: the second look
    _, turn_id = turn(lead, f"{name} ka rate?", llm)
    plan, tools = q("SELECT plan, tool_calls FROM turns WHERE id = %s", turn_id)[0]
    assert tools[0]["tool"] == "place_in_message" and tools[0]["result"]["place_id"] == place_id
    assert q("SELECT value FROM lead_slots WHERE lead_id = %s AND slot = 'location_id'", lead) == [(place_id,)]


def test_a_place_that_does_not_resemble_the_buyers_words_is_not_taken(lead):
    # Live run: "Clifton" (not one of our places) was mapped to Zamzama, a place the model
    # knows to be nearby. A pick must resemble the buyer's words; otherwise it is a place we lack.
    some_place = q("SELECT p.id FROM locations p JOIN listings l ON l.location_id = p.id GROUP BY p.id LIMIT 1")[0][0]
    ext = _place_search("Qwertabad", location_id=some_place)        # a made-up place name
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Qwertabad mein hamari listings nahi hain.", claims_no_listings=True)])
    _, turn_id = turn(lead, "Qwertabad mein khareedna hai", llm)
    tools = q("SELECT tool_calls FROM turns WHERE id = %s", turn_id)[0][0]
    assert tools[0]["tool"] == "check_place" and tools[0]["result"]["verdict"] == "not_our_place"
    assert tools[0]["result"]["resemblance"] < 0.3
    assert tools[1]["tool"] == "find_location" and tools[1]["result"]["status"] == "none"
    slots = dict(q("SELECT slot, value FROM lead_slots WHERE lead_id = %s", lead))
    assert "location_id" not in slots and slots["location_text"] == "Qwertabad"


def test_a_value_the_buyer_did_not_say_is_not_stored(lead):
    # Live run: "jo hai woh dikha dein" came back with purpose "sale", which they never said.
    ext = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "more_options"}], "slot_updates": [
        {"slot": "purpose", "value": "sale", "said": "khareedna hai", "source": "stated", "confidence": 1}]})
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Buy karna hai ya rent?")])
    _, turn_id = turn(lead, "acha phir jo hai woh dikha dein", llm)
    assert q("SELECT slot FROM lead_slots WHERE lead_id = %s", lead) == []
    plan = q("SELECT plan FROM turns WHERE id = %s", turn_id)[0][0]
    assert plan["search"] is None                                             # buy or rent still unknown


def test_a_search_with_no_details_gets_a_second_look(lead):
    # Live run: "10 marla ka ghar chahiye Bahria Town mein, 5 crore tak" came back with nothing.
    empty = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}]})
    full = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}], "slot_updates": [
        {"slot": "budget_max", "value": 50000000, "said": "5 crore tak", "source": "stated", "confidence": 1}]})
    llm = ScriptedLLM([empty, full], [ReplyDraft(reply="Buy karna hai ya rent?")])
    turn(lead, "ghar chahiye 5 crore tak", llm)
    assert len(llm.prompts["extractor"]) == 2
    assert "came back empty" in llm.prompts["extractor"][1][-1]["content"]
    assert q("SELECT value FROM lead_slots WHERE lead_id = %s AND slot = 'budget_max'", lead) == [(50000000,)]


def test_the_place_named_exactly_is_searched_not_a_part_of_it(lead):
    # Live run: "Askari 5" searched only Sector F / Sector G, which the model had picked.
    rows = q("""SELECT parent.id, parent.name, child.id FROM locations parent
                JOIN locations child ON child.parent_id = parent.id
                JOIN listings l ON l.location_id = child.id WHERE l.status = 'available' LIMIT 1""")
    if not rows:
        pytest.skip("no place with listed sub-places today")
    parent_id, parent_name, child_id = rows[0]
    llm = ScriptedLLM([_place_search(parent_name, location_id=child_id)], [ReplyDraft(reply="Ye options hain.")])
    _, turn_id = turn(lead, f"{parent_name} mein khareedna hai", llm)
    plan, tools = q("SELECT plan, tool_calls FROM turns WHERE id = %s", turn_id)[0]
    assert tools[0]["result"]["verdict"] == "use_named" and plan["search"]["location_id"] == parent_id


def test_asking_again_leaves_one_open_question(lead):
    greet = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}]})
    llm = ScriptedLLM([greet, greet, greet, greet], [ReplyDraft(reply="Buy karna hai ya rent?"),
                                       ReplyDraft(reply="Buy ya rent?")])
    turn(lead, "ghar chahiye", llm)
    turn(lead, "house dikhayein", llm)
    assert q("SELECT slot, status FROM open_questions WHERE lead_id = %s ORDER BY id", lead) == [
        ("purpose", "dropped"), ("purpose", "open")]


def test_a_place_we_do_not_know_may_be_called_empty(lead):
    ext = Extraction.model_validate({
        "language": "roman_urdu", "intents": [{"type": "search"}],
        "slot_updates": [
            {"slot": "purpose", "value": "sale", "said": "khareedna", "source": "stated", "confidence": 1},
            {"slot": "location_text", "value": "Bahria Town", "said": "Bahria Town", "source": "stated", "confidence": 1},
            {"slot": "property_types", "value": ["house"], "said": "house", "source": "stated", "confidence": 1},
        ],
    })
    llm = ScriptedLLM([ext], [ReplyDraft(reply="Bahria Town mein abhi hamari listings nahi hain.",
                                         claims_no_listings=True)])
    _, turn_id = turn(lead, "Bahria Town mein house khareedna hai?", llm)
    assert q("SELECT validation->>'attempts' FROM turns WHERE id = %s", turn_id) == [("1",)]


def test_a_second_reading_fills_what_the_first_dropped_but_adds_nothing_unsaid():
    # Replaying a live turn: one reading dropped "plot" 2 times in 10.
    from agent.graph import _combine
    said = lambda slot, value, words: {"slot": slot, "value": value, "said": words,  # noqa: E731
                                       "source": "stated", "confidence": 1.0}
    text = "120 gaz ka plot chahiye installments pe"
    first = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}], "signals": [],
        "slot_updates": [said("size_min_sqyd", 120, "120 gaz"), said("payment_mode", "installments", "installments pe")]})
    second = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}],
        "signals": ["urgent"], "slot_updates": [said("property_types", ["plot"], "plot"),
                                                said("budget_max", 10_000_000, "1 crore tak")]})
    ext, kept, unsaid = _combine([first, second], [text])
    assert {u.slot: u.value for u in kept} == {"size_min_sqyd": 120, "payment_mode": "installments",
                                               "property_types": ["plot"]}
    assert ext is first and ext.signals == []        # the base reading's signals, none added


def test_a_reaction_to_our_last_list_is_answered_from_those_listings(lead):
    # Live run: "bohat mehnge hain" named no listing; the reply repeated the prices from the
    # chat, was rejected twice, and the template went out. Now those listings are read again.
    rows = _three_available()
    reaction = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "other"}]})
    llm = ScriptedLLM([SEARCH, reaction], [
        ReplyDraft(reply="Ye options hain.", listing_ids_mentioned=[r[0] for r in rows]),
        ReplyDraft(reply=f"Ji, pehla {pkr(rows[0][2])} ka hai.", listing_ids_mentioned=[rows[0][0]]),
    ])
    turn(lead, "khareedna hai", llm)
    reply, _ = turn(lead, "bohat mehnge hain", llm)
    facts = json.loads(llm.prompts["responder"][1][1]["content"])["FACTS"]
    assert [f["listing_id"] for f in facts["listings_in_our_last_reply"]] == [r[0] for r in rows]
    assert reply.text == f"Ji, pehla {pkr(rows[0][2])} ka hai."              # passed the check
    # Read again, not asked about: none is recorded as one they inquired about.
    assert not q("SELECT 1 FROM lead_listings WHERE lead_id = %s AND relation = 'inquired'", lead)


def test_no_answer_from_the_model_still_gets_the_buyer_a_reply(lead):
    # Live run: a model call hung for 300 s and the turn failed: the buyer got nothing.
    from agent.llm import LLMError
    rows = _three_available()
    llm = ScriptedLLM([SEARCH], [LLMError("gave no answer within 25 s")])
    reply, _ = turn(lead, "khareedna hai", llm)
    assert reply.text and reply.audit["validation"]["used_template"]
    assert len(llm.prompts["responder"]) == 1                         # not asked again
