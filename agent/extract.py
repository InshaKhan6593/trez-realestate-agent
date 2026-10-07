"""The extractor: the model reads the buyer's messages and reports, as JSON,
what they asked and what they told us. It decides nothing.

It is grounded in our data: it picks places from the real place list (by id)
and listings from the ones this buyer already discussed (by Zameen id), so it
never has to guess a spelling or invent an id.
"""

from __future__ import annotations

import json

from .context import TurnContext
from .llm import LLM, Usage
from .schemas import Extraction

SYSTEM = """You read WhatsApp messages from people asking Trez Enterprises (a Karachi real estate
agency) about property. Buyers write in Roman Urdu, Urdu or English, often mixed.
Report ONLY what the messages say, as JSON matching the schema. Decide nothing, answer nothing.

Rules:
- language: roman_urdu = Urdu written in English letters ("flat chahiye", "kitne ka hai");
  urdu = Urdu script; english = English sentences; mixed = clearly both in one message.
- intents: one per request, in order; at least one for every message. A message can hold
  several: "installment plan kya hai? photos bhi bhejo" = listing_question AND photos. Types:
  greeting = salam, hi, hello; thanks = shukriya, thank you, ok theek hai;
  not_interested = no longer looking or just browsing ("bas dekh raha tha", "abhi nahi chahiye",
  "nahi chahiye"); other = anything else;
  search = looking for property ("Askari mein flat chahiye", giving budget/area/size);
  listing_question = asking about a specific listing's details; availability = "available hai?";
  photos / video = wants pictures / a video; more_options = wants other options;
  negotiation = price reduction, "last price"; ask_human = wants a person/call;
  legal_or_documents = registry, NOC, transfer, ownership; seller = wants to sell/rent out theirs.
- A Zameen link or listing number: put it in listing.zameen_id.
- OUR_LAST_LIST is the numbered list of listings in our most recent message that named
  listings, in the order we wrote them. "pehla / pehle wala / first / upar wala" = n 1,
  "doosra / second" = n 2, "teesra" = n 3, "last / aakhri / neeche wala" = the last one,
  "sasta wala / the cheaper one" = the lowest price there. Use that listing's zameen_id.
- "this one / is ki / the DHA one": if you can tell which of OUR_LAST_LIST or
  LISTINGS_DISCUSSED it is, use its zameen_id; only if you cannot tell, describe it in
  listing.from_history.
- A question about price, a discount or the deal ("price kam ho sakti hai?") is about the
  listing being discussed: give that listing too when you can tell which it is.
- slot_updates: facts about what THEY want, only from what they say about their own needs.
  Asking about a specific listing does NOT change what they want: never copy a listing's
  area, type, size or price into slot_updates. source "stated" if they said it, "inferred" if implied
  (confidence below 0.7 for inferred).
  * purpose: "sale" if they say they want to buy, "rent" if they say they want to rent.
    Only when they say it (khareedna, buy, kiraye pe, rent); "chahiye" alone does not say which.
  * location_id: the id of the matching place in PLACES (choose the most specific that fits;
    "Askari V" = "Askari 5", "Malir Cantt" = "Malir Cantonment"). If the place they named is
    not in PLACES (e.g. "Bahria Town", "Clifton"), put their words in location_text and do NOT
    choose a broader place (a city or area that merely contains it) instead.
  * said: for every slot update, copy the buyer's own words it comes from, exactly as written
    (for a place: just their name for it, e.g. "askari 5"). We check places against these words.
  * budget_min/budget_max: whole PKR. "4 cr tak" -> budget_max 40000000; "80 lakh" -> 8000000;
    a bare "4.5" in a house-buying chat means crore (inferred).
  * size in square yards: 1 marla = 25 sq yd, 1 kanal = 500 sq yd, sq ft / 9 = sq yd.
  * property_types: list from house, flat, plot, commercial, portion.
  * timeline: under_1_month | 1_3_months | 3_6_months | browsing.
  * payment_mode: cash | installments | bank_loan | selling_first.
  * decision_maker: self | with_family | for_someone_else.  use: live | invest.
  Use only these values; something that fits none (e.g. "I am a dealer") is not a slot
  (a dealer is the signal "dealer").
- A short answer like "3 tak" or "haan" answers the matching OPEN_QUESTIONS item. If
  location_id is open, a place name is the answer: choose its id from PLACES (it is a place,
  not a listing).
- signals: only if clearly present. visit_request: wants to see it ("dekhne aa sakta hoon?");
  token_or_bayana: offers to pay a token/advance/bayana, even with conditions ("aaj token de dun",
  "8 crore final karein to token de deta hoon", "bayana de doon?", "advance bhej doon"); cash_ready;
  urgent; dealer: an agent/dealer fishing for stock; frustrated; repeat_question.
- rejected: listings they said no to, with rejected_reason in their words."""


def build_prompt(ctx: TurnContext, places: list[dict], discussed: list[dict],
                 last_list: list[dict] | None = None) -> list[dict]:
    payload = {
        "PLACES": places,
        "OUR_LAST_LIST": last_list or [],
        "LISTINGS_DISCUSSED": discussed,
        "OPEN_QUESTIONS": ctx.state.open_questions,
        "WHAT_WE_KNOW": {k: v["value"] for k, v in ctx.state.slots.items()},
        "EARLIER_CONVERSATION": ctx.recent[-8:],
        "BUYER_MESSAGES_NOW": [m["text"] or f"[{m['type']}]" for m in ctx.burst],
    }
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]


async def extract(llm: LLM, usage: Usage, messages: list[dict]) -> Extraction:
    return await llm.structured("extractor", messages, Extraction, usage)
