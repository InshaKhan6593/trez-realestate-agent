"""The extractor: the model reads the buyer's messages and reports, as JSON,
what they asked and what they told us. It decides nothing.

It is grounded in our data: it picks places from the real place list (by id)
and listings from the ones this buyer already discussed (by Zameen id), so it
never has to guess a spelling or invent an id.
"""

from __future__ import annotations

import json

from .context import TurnContext, for_model
from .llm import LLM, Usage
from .schemas import Extraction

SYSTEM = """You read WhatsApp messages from people asking Trez Enterprises (a Karachi real estate
agency) about property. Buyers write in Roman Urdu, Urdu or English, often mixed.
Report ONLY what the messages say, as JSON matching the schema. Decide nothing, answer nothing.
Describe meanings, not wording: people say the same thing in many ways.

Rules:
- language: of the buyer's own words in BUYER_MESSAGES_NOW, not counting names, numbers,
  links or property words. If that leaves too little to tell, the language of the buyer's
  earlier messages. Never judge by our replies.
  roman_urdu = Urdu written in English letters, even when it borrows English nouns;
  urdu = Urdu script; english = English sentences; mixed = clearly both in one message.
- BUYER_MESSAGES_NOW items that are objects are not text (a voice note, a picture, a location):
  report only what their "words" say; when we cannot see or hear the content, do not guess it.
- intents: one per request, in order; at least one for every message; a message can hold
  several. Types: greeting; thanks; not_interested = no longer looking, or only browsing;
  search = looking for property, or asking about a kind of property in a place (prices, what
  is there) when no particular listing has been discussed; listing_question = about one
  particular listing's details; availability = whether a listing is still available;
  photos / video; more_options = wants to see other or more listings; negotiation = asking
  for a lower price or the last price; ask_human = wants a person or a call;
  legal_or_documents = registry, NOC, transfer, title, ownership; seller = wants to sell or
  rent out their own property; other = anything else.
- A Zameen link or listing number: put it in listing.zameen_id.
- OUR_LAST_LIST is the numbered list of listings in our most recent message that named
  listings, in the order we wrote them. A reference by position (first, second, third, last)
  or by comparison (the cheapest, the biggest) means that listing there: use its zameen_id.
- A reference like "this one" or by area: if you can tell which of OUR_LAST_LIST or
  LISTINGS_DISCUSSED it is, use its zameen_id; only if you cannot tell, describe it in
  listing.from_history.
- A question about the price, a discount or the deal is about the listing being discussed:
  give that listing too when you can tell which it is.
- wants: one field per kind of detail below. Go through every field: each is null unless
  these messages say it, else {value, said, source, confidence}. What THEY want to buy or rent,
  only from what they say about their own needs.
  When they ask about one particular listing, that listing's own details (its area, type,
  size, price) are not their wants. Their own words for what they are looking for always
  are, even when listings we showed have the same type or area. A seller's details of their own property are
  not wants either. source "stated" if they said it, "inferred" if implied
  (confidence below 0.7 for inferred).
  * purpose: "sale" if they say they want to buy, "rent" if they say they want to rent. Leave
    purpose null when they do not say which; that leaves only purpose null, never the other fields.
  * location_id: the id of the matching place in PLACES (the most specific that fits; spelling,
    short forms and Roman numerals do not matter). A place not in PLACES: put their words in
    location_text and do NOT choose a broader place that merely contains it.
  * said: for every field you fill, copy the buyer's own words it comes from, exactly as written
    (for a place: just their name for it). We check places against these words.
  * property_types: list from house, flat, plot, commercial, portion (a flat may be called an
    apartment).
  * bedrooms_min: the number of bedrooms (or rooms) they ask for.
  * budget_min/budget_max: whole PKR. 1 crore = 10,000,000; 1 lakh = 100,000; a bare number in
    a house-buying chat is crore (inferred).
  * size in square yards: 1 marla = 25 sq yd, 1 kanal = 500 sq yd, sq ft / 9 = sq yd; gaz = sq yd.
  * timeline: under_1_month | 1_3_months | 3_6_months | browsing.
  * payment_mode: cash | installments | bank_loan | selling_first.
  * decision_maker: self | with_family | for_someone_else.  use: live | invest.
  Use only these values; something that fits none is not a slot (a dealer is the signal
  "dealer", not a slot).
- A short answer answers the matching OPEN_QUESTIONS item. If location_id is open, a place
  name is the answer: choose its id from PLACES (it is a place, not a listing).
- signals: only if clearly present. visit_request: wants to come and see a property;
  token_or_bayana: offers to pay a token, advance or bayana, even with conditions;
  cash_ready; urgent; dealer: an agent or dealer fishing for stock; frustrated;
  repeat_question.
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
        "BUYER_MESSAGES_NOW": [for_model(m) for m in ctx.burst],
    }
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]


async def extract(llm: LLM, usage: Usage, messages: list[dict]) -> Extraction:
    return await llm.structured("extractor", messages, Extraction, usage)
